"""侦探团端到端演示：Hub + worker 全在内存里跑，不需要任何外部服务。

    python examples/detective_panel/run_demo.py
    python examples/detective_panel/run_demo.py --rounds 2 --topic "国常会后是否在构筑底"

它做的事和 DSH 那边**完全一样**：起 Hub → 注册五个侦探 + 裁决官 + 报告官 →
调 `debate/run`（panel 形态）→ 拿裁决和转录 → 调 `report/build` 出 Markdown。
DSH 侧只是把这两次调用换成 MCP tool（`debate__run` / `report__build`）。

没配模型端点也能跑：发言会带 `【模拟·未配模型】`，报告里标明"结论不作数"。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from multi_agent.client import AgentRuntime                 # noqa: E402
from multi_agent.server import HubServer                     # noqa: E402

from examples.detective_panel.project import (               # noqa: E402
    REPORT_ACTION, REPORT_CAP, install, load_evidence, panel_params)

TOKEN = "demo-token"
DEFAULT_TOPIC = "（示例）国常会后的行情影响：是否开始构筑底部"


def run(evidence_path: str | None = None, rounds: int = 2, topic: str | None = None,
        quiet: bool = False) -> Dict[str, Any]:
    """跑一局侦探团，返回 {panel, report, markdown}。"""
    project = install()
    srv = HubServer(host="127.0.0.1", port=0, db_path=":memory:", token=TOKEN)
    srv.start_background()

    worker = AgentRuntime(srv.url, "panel-worker", project.manifests(),
                          token=TOKEN, concurrency=8, poll_wait=1)
    project.register(worker)          # 业务 handler + debate/moderator（框架实现）

    evidence = load_evidence(evidence_path)
    event_name = str((evidence.get("event") or {}).get("name") or "")
    if event_name.startswith("（填"):
        event_name = ""                 # 占位符不当议题
    subject = topic or event_name or DEFAULT_TOPIC

    worker.start()
    try:
        panel_task = worker.client.submit(
            "debate", "run", panel_params(subject, evidence, max_rounds=rounds),
            wait=True, timeout_s=180)
        if not isinstance(panel_task, dict) or panel_task.get("state") != "succeeded":
            raise RuntimeError(f"debate/run 失败：{(panel_task or {}).get('error')}")
        panel = panel_task.get("result") or {}

        report_task = worker.client.submit(
            REPORT_CAP, REPORT_ACTION,
            {"topic": subject, "panel": panel}, wait=True, timeout_s=60)
        if not isinstance(report_task, dict) or report_task.get("state") != "succeeded":
            raise RuntimeError(f"report/build 失败：{(report_task or {}).get('error')}")
        report = report_task.get("result") or {}
    finally:
        worker.stop()
        srv.stop()

    out = {"panel": panel, "report": report,
           "markdown": report.get("markdown") or ""}
    if not quiet:
        print(f"议题：{subject}")
        print(f"发言：{len(panel.get('transcript') or [])} 条 / "
              f"{panel.get('rounds')} 轮 → 裁决 {panel.get('decision')} "
              f"（{panel.get('stopped_reason')}）")
        print(f"字数 {panel.get('chars_used')} / 估算 token {panel.get('tokens_used')}")
        simulated = any(str(t.get("text") or "").startswith("【模拟·未配模型】")
                        for t in (panel.get("transcript") or []))
        if simulated:
            print("注意：本轮含模拟发言（未配模型端点），结论不作数。")
        print("\n" + out["markdown"])
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="侦探团演示（零外部依赖）")
    ap.add_argument("--evidence", default=None, help="证据包 JSON 路径")
    ap.add_argument("--rounds", type=int, default=2, help="最多几轮（受 limits 上限约束）")
    ap.add_argument("--topic", default=None, help="议题")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    try:
        out = run(args.evidence, args.rounds, args.topic, args.quiet)
    except Exception as exc:  # noqa: BLE001 - 演示脚本要给出明确退出码
        print(f"运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0 if out["panel"].get("decision") else 1


if __name__ == "__main__":
    sys.exit(main())