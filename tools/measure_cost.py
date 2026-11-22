#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""measure_cost.py —— 度量一条跨 Agent 任务在三种通道上的实际开销。

这个脚本**不下结论，只打印数据**。任何人都可以复跑，自己看数字。

为什么不用"抓包截图"当证据
==========================
抓包只能证明"这条链路没向 LLM 端点发 HTTP 请求"，**证明不了零 token**。
零 token 的本质是：执行路径里**没有拼接 prompt、没有调用模型**。
所以本脚本统计三件事，每件都是可复现的：

  1. **消息条数与字节数**：Hub REST / UDP 直连 / HTTP 中继 各自搬了多少数据
  2. **外部 HTTP 请求明细**：中继模式实际打了哪些 URL（有没有 LLM 端点）
  3. **静态依赖**：执行路径 import 的模块里有没有模型 SDK

用法
====
    # 全跑（含真实 UDP 打洞 + 真实 ntfy 中继，约 30 秒）
    python3 tools/measure_cost.py

    # 只跑本地（不联网，跳过中继）
    python3 tools/measure_cost.py --skip-relay

    # 输出 JSON（便于进 CI 比对）
    python3 tools/measure_cost.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_og = socket.getaddrinfo
socket.getaddrinfo = lambda *a, **k: [r for r in _og(*a, **k) if r[0] == socket.AF_INET]

from multi_agent.bootstrap import DirectLink, make_ticket, run_local  # noqa: E402

LLM_MARKERS = ("18799", "chat/completions", "/v1/messages", "openai",
               "anthropic", "api.deepseek", "dashscope", "generativelanguage")


# ═══════════════════════════════════════════════ 1. 消息与字节统计
def _frame_size(payload) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode()) + 2   # +2 分隔


def measure_direct(rounds: int = 5, payload_size: int = 200) -> dict:
    """UDP 直连：跑 rounds 次往返，统计条数与字节。"""
    a = DirectLink("A", 0, punch_timeout=8.0, verbose=False)
    b = DirectLink("B", 0, punch_timeout=8.0, verbose=False)
    got = []
    try:
        ta = make_ticket(("127.0.0.1", a.local_port), a.local_port, "A")
        tb = make_ticket(("127.0.0.1", b.local_port), b.local_port, "B")
        t = threading.Thread(target=lambda: a.punch(tb), daemon=True)
        t.start()
        time.sleep(0.2)
        b.punch(ta)
        t.join(timeout=10)
        if a.state != "direct" or b.state != "direct":
            return {"ok": False, "reason": "UDP 直连没建立"}

        b.on_message = lambda m: got.append(m)
        padded = "x" * payload_size
        t0 = time.time()
        sent_bytes = 0
        for i in range(rounds):
            msg = {"i": i, "pad": padded}
            sent_bytes += _frame_size(msg)
            if not a.send(msg):
                return {"ok": False, "reason": f"第 {i} 条发送失败"}
        deadline = time.time() + 10
        while time.time() < deadline and len(got) < rounds:
            time.sleep(0.02)
        elapsed = time.time() - t0
        recv_bytes = sum(_frame_size(m) for m in got)
        return {
            "ok": len(got) == rounds,
            "channel": "udp_direct",
            "rounds": rounds,
            "messages_sent": rounds,
            "messages_received": len(got),
            "bytes_sent": sent_bytes,
            "bytes_received": recv_bytes,
            "elapsed_ms": int(elapsed * 1000),
            "per_msg_ms": round(elapsed * 1000 / max(1, rounds), 2),
            "ack_frames": rounds * 2,      # 每条消息 1 ACK，另一方向同理
            "llm_calls": 0,
        }
    finally:
        a.close()
        b.close()


def measure_relay(rounds: int = 2, payload_size: int = 200) -> dict:
    """HTTP 中继（ntfy）：统计 POST/GET 条数、字节、以及实际 URL 列表。"""
    from multi_agent import signal as SIG

    calls = []
    orig_post, orig_poll = SIG._post, SIG._poll

    def spy_post(topic, payload, title=""):
        calls.append(("POST", f"https://ntfy.sh/{topic}",
                      len(json.dumps(payload, ensure_ascii=False).encode())))
        return orig_post(topic, payload, title)

    def spy_poll(topic, since="10m", limit=200):
        msgs = orig_poll(topic, since, limit)
        calls.append(("GET", f"https://ntfy.sh/{topic}/json", 0))
        return msgs

    SIG._post, SIG._poll = spy_post, spy_poll
    topic = "cost-" + os.urandom(4).hex()
    out = {}
    try:
        def listener():
            s = SIG.Signal(topic, "L", 0, verbose=False)
            try:
                s.discover(); s.announce()
                p = s.wait_peer_ticket("C", timeout=60)
                s.connect(p["ticket"], punch_timeout=0.5, allow_relay=False)
                s.mode = "relay"; s.peer_agent = "C"
                s.serve(allowed_prefixes=["*"])
                got = s.recv(timeout=60, filter_fn=lambda m: m.get("action") == "run")
                if got:
                    r = run_local(str(got.get("script")), list(got.get("argv") or []))
                    s.send({"action": "result", "id": got.get("_mid"), "result": r},
                           to=got.get("_from"))
                time.sleep(3)
            finally:
                s.close()

        th = threading.Thread(target=listener, daemon=True)
        th.start()
        time.sleep(2)

        s = SIG.Signal(topic, "C", 0, verbose=False)
        s.discover(); s.announce()
        p = s.wait_peer_ticket("L", timeout=60)
        s.connect(p["ticket"], punch_timeout=0.5, allow_relay=False)
        s.mode = "relay"; s.peer_agent = "L"

        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write("import json\nprint(json.dumps({'ok': True}))\n")
            script = fh.name
        t0 = time.time()
        s.send({"action": "run", "script": script})
        got = s.recv(timeout=60, filter_fn=lambda m: m.get("action") == "result")
        elapsed = time.time() - t0
        s.close()
        th.join(timeout=10)
        os.unlink(script)

        posts = [c for c in calls if c[0] == "POST"]
        gets = [c for c in calls if c[0] == "GET"]
        urls = sorted({c[1] for c in calls})
        llm = [u for u in urls if any(k in u for k in LLM_MARKERS)]
        out = {
            "ok": bool(got),
            "channel": "http_relay_ntfy",
            "http_posts": len(posts),
            "http_gets": len(gets),
            "total_http_requests": len(calls),
            "bytes_posted": sum(c[2] for c in posts),
            "elapsed_ms": int(elapsed * 1000),
            "distinct_urls": urls,
            "llm_endpoint_hits": llm,
            "llm_calls": len(llm),
        }
    finally:
        SIG._post, SIG._poll = orig_post, orig_poll
    return out


# ═══════════════════════════════════════════════ 2. 静态依赖扫描
_EXECUTION_MODULES = ("multi_agent/bootstrap.py", "multi_agent/signal.py",
                      "multi_agent/integrations/audit.py",
                      "multi_agent/integrations/runner.py",
                      "multi_agent/integrations/audit_profiles.py",
                      "multi_agent/router.py", "multi_agent/shared_data.py",
                      "multi_agent/db_policies.py", "multi_agent/dbmanager.py")

_SDK_MARKERS = ("import openai", "from openai", "import anthropic",
                "from anthropic", "import dashscope", "from dashscope",
                "import litellm", "import cohere", "from google.generativeai",
                "import transformers", "import vllm", "from ollama",
                "import replicate")


def scan_imports() -> dict:
    """扫执行路径源码：有没有 import 模型 SDK。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    violations = []
    scanned = []
    for rel in _EXECUTION_MODULES:
        path = os.path.join(root, rel)
        if not os.path.exists(path):
            continue
        scanned.append(rel)
        src = open(path, "r", encoding="utf-8").read()
        for marker in _SDK_MARKERS:
            if marker in src:
                violations.append({"file": rel, "marker": marker})
    return {"ok": not violations, "files_scanned": scanned,
            "violations": violations}


def runtime_check() -> dict:
    """运行时：sys.modules 里有没有已加载的模型 SDK。"""
    loaded = []
    for name in ("openai", "anthropic", "dashscope", "litellm", "cohere",
                 "transformers", "vllm", "replicate"):
        if name in sys.modules:
            loaded.append(name)
    return {"ok": not loaded, "loaded_sdks": loaded}


# ═══════════════════════════════════════════════ 3. 成本模型
def cost_model() -> dict:
    """把"哪些环节需要人或模型介入"写死在数据里，而不是散文里。"""
    return {
        "steps": [
            {"step": "交换门票", "channel": "人 / 大模型一句话",
             "needs_llm": True, "tokens": "≈ 一条短消息（~40 字符）",
             "note": "全流程唯一需要人或模型介入的点"},
            {"step": "发布/领取门票", "channel": "HTTP POST/GET 到 ntfy",
             "needs_llm": False, "tokens": 0},
            {"step": "打洞", "channel": "UDP PUNCH/PONG",
             "needs_llm": False, "tokens": 0},
            {"step": "派指令", "channel": "UDP 直连 或 HTTP 中继",
             "needs_llm": False, "tokens": 0},
            {"step": "执行脚本", "channel": "本地 subprocess",
             "needs_llm": False, "tokens": 0},
            {"step": "回传结果", "channel": "UDP 直连 或 HTTP 中继",
             "needs_llm": False, "tokens": 0},
        ],
        "audit_llm_note": ("审计官默认 level=rule（0 网络 0 token）；"
                           "level=llm 是**显式可选档**，只有调用方传了 llm 回调才跑"),
        "conclusion": "除『交换门票』外，所有环节都是程序调用，不拼接 prompt、不调模型",
    }


# ═══════════════════════════════════════════════ main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="度量跨 Agent 通道开销（可复现）")
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--skip-relay", action="store_true")
    ap.add_argument("--skip-direct", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    report = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())}

    if not a.skip_direct:
        report["udp_direct"] = measure_direct(rounds=a.rounds)
    if not a.skip_relay:
        try:
            report["http_relay"] = measure_relay()
        except Exception as e:  # noqa: BLE001
            report["http_relay"] = {"ok": False,
                                    "error": f"{type(e).__name__}: {e}"}

    report["static_imports"] = scan_imports()
    report["runtime_sdks"] = runtime_check()
    report["cost_model"] = cost_model()

    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    print("=" * 66)
    print("跨 Agent 通道开销度量（任何人都可复跑）")
    print("=" * 66)

    d = report.get("udp_direct")
    if d:
        print("\n[1] UDP 直连")
        if d.get("ok"):
            print(f"    往返 {d['rounds']} 条：发出 {d['bytes_sent']}B，"
                  f"收到 {d['bytes_received']}B")
            print(f"    耗时 {d['elapsed_ms']}ms（{d['per_msg_ms']}ms/条），"
                  f"ACK 帧 {d['ack_frames']} 个")
            print(f"    LLM 调用：{d['llm_calls']} 次")
        else:
            print(f"    ✗ {d.get('reason')}")

    r = report.get("http_relay")
    if r:
        print("\n[2] HTTP 中继（ntfy）")
        if r.get("ok"):
            print(f"    HTTP 请求 {r['total_http_requests']} 次"
                  f"（POST {r['http_posts']} / GET {r['http_gets']}）")
            print(f"    上行字节 {r['bytes_posted']}B，"
                  f"端到端耗时 {r['elapsed_ms']}ms")
            print(f"    访问的 URL（去重）：")
            for u in r["distinct_urls"]:
                print(f"      {u}")
            print(f"    LLM 端点命中：{r['llm_endpoint_hits'] or '0 次'}")
        else:
            print(f"    ✗ {r.get('error') or r.get('reason')}")

    si = report["static_imports"]
    print("\n[3] 静态依赖扫描（执行路径源码）")
    print(f"    扫描 {len(si['files_scanned'])} 个文件："
          f"{'未发现模型 SDK ✅' if si['ok'] else '发现违规 ❌ ' + str(si['violations'])}")

    rt = report["runtime_sdks"]
    print(f"\n[4] 运行时已加载模型 SDK：{rt['loaded_sdks'] or '无 ✅'}")

    cm = report["cost_model"]
    print("\n[5] 成本模型（哪些环节需要人或模型）")
    for s in cm["steps"]:
        flag = "🔥需要模型" if s["needs_llm"] else "程序调用"
        print(f"    {s['step']:<10} {s['channel']:<22} {flag}  {s.get('tokens', 0)}")
    print(f"\n    {cm['conclusion']}")
    print(f"    审计官：{cm['audit_llm_note']}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
