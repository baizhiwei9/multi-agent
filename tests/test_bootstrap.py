"""bootstrap 测试：STUN 探测 / 门票 / 打洞 / 直连执行。

这一层的核心价值是「**省 token**」：
① 自己用 STUN 问出公网地址（不需要人知道 IP）
② 只交换一次短门票（~40 字符）
③ 之后所有通信走 UDP 直连，不再经过大模型

测试重点：
- 门票格式必须严格（错了就打洞到错误地址）
- 打洞在同一 NAT 后的两个 socket 之间必须真通
- 直连后 send/recv 要有 ack（丢包能重传）
- 连不上时**明确失败**，不静默假装成功
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

import pytest

from multi_agent.bootstrap import (
    DirectLink, discover_public, make_ticket, parse_ticket, run_local,
)


# ───────────────────────────────────────────── 门票
def test_ticket_roundtrip_long():
    t = make_ticket(("1.2.3.4", 50659), 8792, "agent-a")
    assert t == "A1|1.2.3.4|50659|8792|agent-a"
    d = parse_ticket(t)
    assert d == {"ip": "1.2.3.4", "public_port": 50659,
                 "local_port": 8792, "agent_id": "agent-a"}


def test_ticket_is_compact():
    """门票要够短 —— 它就是全流程唯一的 token 成本（人/大模型转发一次）。"""
    t = make_ticket(("120.245.114.140", 50659), 8792, "openclaw")
    assert len(t) < 60, f"门票太长（{len(t)} 字符）：{t}"
    assert parse_ticket(t)["agent_id"] == "openclaw"


def test_ticket_rejects_garbage():
    for bad in ("", "hello", "A1|1.2.3.4", "B2|1.2.3.4|1|2|x",
                "A1|1.2.3.4|notaport|8792|x"):
        with pytest.raises(ValueError):
            parse_ticket(bad)


def test_ticket_error_message_is_actionable():
    with pytest.raises(ValueError) as e:
        parse_ticket("A1|1.2.3.4")
    assert "A1|<公网IP>" in str(e.value)


# ───────────────────────────────────────────── STUN
def test_stun_returns_something_or_none():
    """STUN 是外部依赖：这里只保证「要么拿到地址，要么明确返回 None」，
    不能抛异常也不能返回半个结果。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 0))
    try:
        info = discover_public(s, servers=(("stun.cloudflare.com", 3478),))
        assert "mapped" in info and "attempts" in info
        if info["mapped"]:
            ip, port = info["mapped"]
            assert isinstance(ip, str) and isinstance(port, int) and port > 0
    finally:
        s.close()


def test_stun_all_unreachable_is_graceful():
    """全部 STUN 不可达 → mapped=None（不是异常），调用方能据此降级。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 0))
    try:
        info = discover_public(s, servers=(("192.0.2.1", 9),))   # TEST-NET，必然不可达
        assert info["mapped"] is None
        assert info["attempts"][0]["mapped"] is None
    finally:
        s.close()


# ───────────────────────────────────────────── 打洞（本机两个 socket 模拟两台机器）
def _pair():
    """两个 DirectLink，绑定随机端口，直接互打（绕过 STUN）。"""
    a = DirectLink("A", 0, punch_timeout=8.0, verbose=False)
    b = DirectLink("B", 0, punch_timeout=8.0, verbose=False)
    return a, b


def test_punch_between_sockets_succeeds():
    a, b = _pair()
    try:
        # 手工造票：公网地址就用 127.0.0.1 + 各自本地端口（同机直连等价）
        ta = make_ticket(("127.0.0.1", a.local_port), a.local_port, "A")
        tb = make_ticket(("127.0.0.1", b.local_port), b.local_port, "B")
        out = {}

        def _a():
            out["a"] = a.punch(tb)

        th = threading.Thread(target=_a, daemon=True)
        th.start()
        time.sleep(0.2)
        out["b"] = b.punch(ta)
        th.join(timeout=10)

        assert out["b"]["ok"] is True, out
        assert out["a"]["ok"] is True, out
        assert a.state == "direct" and b.state == "direct"
    finally:
        a.close()
        b.close()


def test_punch_unreachable_fails_explicitly():
    """打不通必须明确返回 ok=False + 原因，不能假装成功。"""
    a = DirectLink("A", 0, punch_timeout=1.5, verbose=False)
    try:
        bogus = make_ticket(("192.0.2.1", 9), 9, "ghost")   # TEST-NET
        res = a.punch(bogus)
        assert res["ok"] is False and res["state"] == "failed"
        assert "中继" in res["reason"]
    finally:
        a.close()


def test_send_after_direct_delivers():
    a, b = _pair()
    got = []
    try:
        ta = make_ticket(("127.0.0.1", a.local_port), a.local_port, "A")
        tb = make_ticket(("127.0.0.1", b.local_port), b.local_port, "B")
        t = threading.Thread(target=lambda: a.punch(tb), daemon=True)
        t.start()
        time.sleep(0.2)
        b.punch(ta)
        t.join(timeout=10)
        b.on_message = lambda m: got.append(m)
        ok = a.send({"hello": "world", "n": 42})
        deadline = time.time() + 5
        while time.time() < deadline and not got:
            time.sleep(0.05)
        assert ok is True
        assert got and got[0]["n"] == 42
    finally:
        a.close()
        b.close()


def test_send_without_link_raises():
    a = DirectLink("A", 0, verbose=False)
    try:
        with pytest.raises(RuntimeError) as e:
            a.send({"x": 1})
        assert "未建立" in str(e.value)
    finally:
        a.close()


def test_send_reports_failure_when_peer_gone():
    """对端不在了：send 必须返回 False（重传耗尽），不能永远 True。"""
    a, b = _pair()
    try:
        ta = make_ticket(("127.0.0.1", a.local_port), a.local_port, "A")
        tb = make_ticket(("127.0.0.1", b.local_port), b.local_port, "B")
        t = threading.Thread(target=lambda: a.punch(tb), daemon=True)
        t.start()
        time.sleep(0.2)
        b.punch(ta)
        t.join(timeout=10)
        b.close()                     # 对端消失
        ok = a.send({"x": 1}, retries=2, ack_timeout=0.2)
        assert ok is False
    finally:
        a.close()


# ───────────────────────────────────────────── 本地执行
def test_run_local_success():
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write("import json,sys\nprint(json.dumps({'argv': sys.argv[1:]}))\n")
        path = fh.name
    try:
        res = run_local(path, ["--x", "1"])
        assert res["ok"] and res["returncode"] == 0
        assert json.loads(res["stdout"])["argv"] == ["--x", "1"]
        assert "duration_ms" in res and res["host"]
    finally:
        os.unlink(path)


def test_run_local_failure_reported():
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write("import sys\nprint('boom', file=sys.stderr)\nsys.exit(7)\n")
        path = fh.name
    try:
        res = run_local(path)
        assert res["ok"] is False and res["returncode"] == 7
        assert "boom" in res["stderr"]
    finally:
        os.unlink(path)


def test_run_local_timeout_killed():
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write("import time\ntime.sleep(30)\n")
        path = fh.name
    try:
        t0 = time.time()
        res = run_local(path, timeout_s=0.5)
        assert res["timed_out"] is True and res["ok"] is False
        assert time.time() - t0 < 8
    finally:
        os.unlink(path)


# ───────────────────────────────────────────── 端到端：打洞 + 远程执行程序
def test_end_to_end_punch_and_execute_program():
    """完整链路：打洞 → 发 run 指令 → 对端跑真实程序 → 结果回来。

    这就是大佬要的形态：**换一次门票，之后零 token**。
    """
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write("import json\nprint(json.dumps({'ok': True, 'role': 'A'}))\n")
        script = fh.name
    a, b = _pair()
    try:
        ta = make_ticket(("127.0.0.1", a.local_port), a.local_port, "A")
        tb = make_ticket(("127.0.0.1", b.local_port), b.local_port, "B")

        # B = 执行端
        results = []

        def _b():
            b.punch(ta)

        tb_thread = threading.Thread(target=_b, daemon=True)
        tb_thread.start()
        time.sleep(0.2)
        a.punch(tb)
        tb_thread.join(timeout=10)
        assert a.state == "direct" and b.state == "direct"

        def _on_msg(msg):
            if msg.get("action") == "run":
                res = run_local(msg["script"], msg.get("argv"))
                b.send({"action": "result", "id": msg.get("id"), "result": res})

        b.on_message = _on_msg
        a.on_message = lambda m: results.append(m)

        assert a.send({"action": "run", "id": "t1", "script": script}) is True

        deadline = time.time() + 10
        while time.time() < deadline and not results:
            time.sleep(0.05)

        res = next((m for m in results if m.get("action") == "result"), None)
        assert res is not None, results
        assert res["result"]["ok"] is True
        assert res["result"]["returncode"] == 0
        assert json.loads(res["result"]["stdout"])["role"] == "A"
    finally:
        a.close()
        b.close()
        os.unlink(script)


# ───────────────────────────────────────────── 可信 UDP：去重
def test_duplicate_packet_delivered_once():
    """UDP 会重复投递（对端重传）。同一 seq 只能交给业务层一次，
    否则"跑一次脚本"会被执行多遍。

    做法：手工把同一帧发两次，看 on_message 是否只被调一次。
    """
    a, b = _pair()
    got = []
    try:
        ta = make_ticket(("127.0.0.1", a.local_port), a.local_port, "A")
        tb = make_ticket(("127.0.0.1", b.local_port), b.local_port, "B")
        t = threading.Thread(target=lambda: a.punch(tb), daemon=True)
        t.start()
        time.sleep(0.2)
        b.punch(ta)
        t.join(timeout=10)
        a.on_message = lambda m: got.append(m)      # 接收方是 a

        frame = b"A|999|" + json.dumps({"k": "v"}).encode()
        target = ("127.0.0.1", a.local_port)
        # 连发 5 次同一个 seq
        for _ in range(5):
            b.sock.sendto(frame, target)
            time.sleep(0.05)
        deadline = time.time() + 5
        while time.time() < deadline and len(got) < 2:
            time.sleep(0.05)
        assert len(got) == 1, f"重复包被投递了 {len(got)} 次"
        assert a.stats["dup_dropped"] >= 4
    finally:
        a.close()
        b.close()


def test_stats_track_send_and_ack():
    a, b = _pair()
    try:
        ta = make_ticket(("127.0.0.1", a.local_port), a.local_port, "A")
        tb = make_ticket(("127.0.0.1", b.local_port), b.local_port, "B")
        t = threading.Thread(target=lambda: a.punch(tb), daemon=True)
        t.start()
        time.sleep(0.2)
        b.punch(ta)
        t.join(timeout=10)
        b.on_message = lambda m: None
        assert a.send({"x": 1}) is True
        assert a.stats["sent"] >= 1
        assert a.stats["ack_miss"] == 0
    finally:
        a.close()
        b.close()


# ───────────────────────────────────────────── 被动打洞（我方 IP 固定）
def test_wait_punch_passive_mode():
    """大佬 18:17：对方主动打给我，我只被动等。

    验证：B 用 wait_punch 只等不打，A 主动 punch → B 也能建立 direct。
    """
    a = DirectLink("A", 0, punch_timeout=8.0, verbose=False)
    b = DirectLink("B", 0, punch_timeout=8.0, verbose=False)
    try:
        ta = make_ticket(("127.0.0.1", a.local_port), a.local_port, "A")
        tb = make_ticket(("127.0.0.1", b.local_port), b.local_port, "B")
        out = {}

        def _b_waits():
            # 传 A 的票 = "我期待 A 打过来"（传自己的票会被当成串台拦掉）
            out["b"] = b.wait_punch(ta)

        th = threading.Thread(target=_b_waits, daemon=True)
        th.start()
        time.sleep(0.3)
        out["a"] = a.punch(tb)          # A 主动打
        th.join(timeout=15)

        assert out["a"]["ok"] is True, out
        assert out["b"]["ok"] is True, out
        assert out["b"]["mode"] == "passive"
        assert a.state == "direct" and b.state == "direct"
        # B 必须知道 A 的地址（从收到的 PUNCH 源地址学到的）
        assert b.peer and b.peer["ip"] == "127.0.0.1"
    finally:
        a.close()
        b.close()


def test_wait_punch_times_out_explicitly():
    """没人打过来 → 明确失败，不静默挂着。"""
    b = DirectLink("B", 0, punch_timeout=1.0, verbose=False)
    try:
        res = b.wait_punch()
        assert res["ok"] is False and res["state"] == "failed"
        assert "没等到" in res["reason"]
    finally:
        b.close()


def test_wait_punch_can_send_after_direct():
    """被动建立后，收发和主动模式完全一样。"""
    a = DirectLink("A", 0, punch_timeout=8.0, verbose=False)
    b = DirectLink("B", 0, punch_timeout=8.0, verbose=False)
    got = []
    try:
        ta = make_ticket(("127.0.0.1", a.local_port), a.local_port, "A")
        tb = make_ticket(("127.0.0.1", b.local_port), b.local_port, "B")
        th = threading.Thread(target=lambda: b.wait_punch(ta), daemon=True)
        th.start()
        time.sleep(0.3)
        a.punch(tb)
        th.join(timeout=15)
        a.on_message = lambda m: got.append(m)
        assert b.send({"hi": "from B"}) is True
        deadline = time.time() + 5
        while time.time() < deadline and not got:
            time.sleep(0.05)
        assert got and got[0]["hi"] == "from B"
    finally:
        a.close()
        b.close()


# ───────────────────────────────────────────── 执行端 fail-closed
def test_serve_commands_denies_by_default():
    """没配白名单时必须拒绝一切 —— 门票就是凭证，默认不能放开。"""
    from multi_agent.bootstrap import serve_commands
    a = DirectLink("A", 0, punch_timeout=8.0, verbose=False)
    b = DirectLink("B", 0, punch_timeout=8.0, verbose=False)
    got = []
    try:
        ta = make_ticket(("127.0.0.1", a.local_port), a.local_port, "A")
        tb = make_ticket(("127.0.0.1", b.local_port), b.local_port, "B")
        th = threading.Thread(target=lambda: b.punch(ta), daemon=True)
        th.start()
        time.sleep(0.2)
        a.punch(tb)
        th.join(timeout=10)

        stop = threading.Event()
        threading.Thread(target=serve_commands, args=(b, stop), daemon=True).start()
        time.sleep(0.2)
        a.on_message = lambda m: got.append(m)
        a.send({"action": "run", "script": "/tmp/anything.py"})
        deadline = time.time() + 5
        while time.time() < deadline and not got:
            time.sleep(0.05)
        stop.set()
        assert got, "没有任何回包（应该回一条 denied 结果）"
        res = got[0].get("result") or {}
        assert res.get("denied") is True
        assert "fail-closed" in res.get("reason", "")
    finally:
        a.close()
        b.close()


def test_serve_commands_allows_listed_dir():
    from multi_agent.bootstrap import serve_commands
    with tempfile.NamedTemporaryFile("w", suffix=".py", dir="/tmp",
                                     delete=False) as fh:
        fh.write("import json\nprint(json.dumps({'ran': True}))\n")
        script = fh.name
    a = DirectLink("A", 0, punch_timeout=8.0, verbose=False)
    b = DirectLink("B", 0, punch_timeout=8.0, verbose=False)
    got = []
    try:
        ta = make_ticket(("127.0.0.1", a.local_port), a.local_port, "A")
        tb = make_ticket(("127.0.0.1", b.local_port), b.local_port, "B")
        th = threading.Thread(target=lambda: b.punch(ta), daemon=True)
        th.start()
        time.sleep(0.2)
        a.punch(tb)
        th.join(timeout=10)

        stop = threading.Event()
        threading.Thread(target=serve_commands,
                         args=(b, stop, ["/tmp/"]), daemon=True).start()
        time.sleep(0.2)
        a.on_message = lambda m: got.append(m)
        a.send({"action": "run", "script": script})
        deadline = time.time() + 10
        while time.time() < deadline and not got:
            time.sleep(0.05)
        stop.set()
        assert got, "白名单内脚本应该被执行并回结果"
        res = got[0].get("result") or {}
        assert res.get("ok") is True and res.get("returncode") == 0
    finally:
        a.close()
        b.close()
        os.unlink(script)


def test_serve_commands_wildcard_allows_all():
    from multi_agent.bootstrap import serve_commands
    a = DirectLink("A", 0, punch_timeout=8.0, verbose=False)
    b = DirectLink("B", 0, punch_timeout=8.0, verbose=False)
    got = []
    script = None
    try:
        ta = make_ticket(("127.0.0.1", a.local_port), a.local_port, "A")
        tb = make_ticket(("127.0.0.1", b.local_port), b.local_port, "B")
        th = threading.Thread(target=lambda: b.punch(ta), daemon=True)
        th.start()
        time.sleep(0.2)
        a.punch(tb)
        th.join(timeout=10)
        stop = threading.Event()
        threading.Thread(target=serve_commands,
                         args=(b, stop, ["*"]), daemon=True).start()
        time.sleep(0.2)
        a.on_message = lambda m: got.append(m)
        with tempfile.NamedTemporaryFile("w", suffix=".py", dir="/tmp",
                                         delete=False) as fh:
            fh.write("print('ok')\n")
            script = fh.name
        a.send({"action": "run", "script": script})
        deadline = time.time() + 10
        while time.time() < deadline and not got:
            time.sleep(0.05)
        stop.set()
        assert got and (got[0].get("result") or {}).get("ok") is True
    finally:
        a.close()
        b.close()
        if script:
            try: os.unlink(script)
            except OSError: pass
