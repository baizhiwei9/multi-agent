"""成本计量：口径、场景与"执行路径没有大模型"的可复现断言。

这里不追求精确到 TCP/IP 头，只守住两件事实：
1. 一次任务在 HTTP / 直连 / 中继上到底有多少帧、多少字节（同机可复跑）；
2. 跑完这些场景后，进程里没有加载任何大模型 SDK。
"""
from __future__ import annotations

from multi_agent.cost import format_table, llm_import_check, measure


def test_http_scenario_counts_frames_and_bytes():
    rep = measure(skip_relay=True)["http"]
    assert rep["http_calls"] >= 2          # submit + 结果回传/轮询
    assert rep["http_bytes_out"] > 0
    assert rep["http_bytes_in"] > 0


def test_udp_direct_and_relay_are_both_measured():
    rep = measure()
    assert rep["udp"]["mode"] == "direct"
    assert rep["udp"]["udp_frames_out"] >= 1
    assert rep["udp"]["udp_bytes_out"] >= 1024

    assert rep["relay"]["mode"] == "relay"
    # 中继多一跳：出向字节必然比直连大（base64 + 信封）
    assert rep["relay"]["udp_bytes_out"] > rep["udp"]["udp_bytes_out"]


def test_no_llm_sdk_anywhere_in_execution_path():
    assert llm_import_check()["ok"] is True


def test_table_has_all_three_rows():
    text = format_table(measure())
    for key in ("http", "udp", "relay"):
        assert key in text
    assert "大模型 SDK" in text