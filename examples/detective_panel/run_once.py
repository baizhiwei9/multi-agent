"""一局跑完就退出：不需要单独起 Hub，也不需要配 MCP。

给手机 / 没有终端的场景用：把这一条命令交给 DSH 执行即可。

    python examples/detective_panel/run_once.py \
        --topic "国常会后A股是否开始构筑底部" \
        --news "把搜到的政策要点贴在这里（可长文）"

也可以把新闻存成文件再传：

    python examples/detective_panel/run_once.py --topic "..." --news-file /tmp/news.md

它会做五件事，然后退出：
1. 内存里起 Hub + worker（不占端口、不留进程）
2. 组装证据包（新闻正文 + 占位行情字段，占位字段会显式标「待补」）
3. 跑 `debate/run`（两正两反四席，默认 3 轮，只在最后裁一次）
4. 拼 Markdown 报告
5. 按实际 token 用量算出这一局花了多少钱（DeepSeek Flash 峰谷两档都算给你看）

成本是**估算**，不是账单：按字符数 × 0.75 折算 token，缓存命中按未命中保守计。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from multi_agent.client import AgentRuntime      # noqa: E402
from multi_agent.integrations import llm as llm_layer   # noqa: E402
from multi_agent.server import HubServer          # noqa: E402

from examples.detective_panel.project import (   # noqa: E402
    REPORT_ACTION, REPORT_CAP, install, load_evidence, panel_params)

TOKEN = "local-once"
DEFAULT_TOPIC = "（未给议题）"

#: DeepSeek Flash 系列单价（元 / 百万 token），2026-09-10 12:00 起的空闲时段口径：
#: 输入（缓存未命中）1 元、输出 4 元；高峰时段（工作日 9-12、14-18）翻倍。
#: 价格会变，跑前请按官方价格页核对；这里只用来给你一个量级。
FLASH_PRICE = {"off": (1.0, 4.0), "peak": (2.0, 8.0)}


def _merge_news(evidence: Dict[str, Any], news: str) -> Dict[str, Any]:
    """把新闻正文塞进证据包；不动行情字段（那些必须靠取数补，不能靠新闻猜）。"""
    ev = dict(evidence or {})
    event = dict(ev.get("event") or {})
    if news.strip():
        event["news_digest"] = news.strip()[:6000]
        event.setdefault("source", "由调用方（DSH）检索后粘贴，未经二次核验")
    ev["event"] = event
    ev.setdefault("note", "行情字段若仍为 null，结论只代表「基于新闻的判断」，不含技术面")
    return ev


def _wrap_token_counter() -> Tuple[Dict[str, int], Any]:
    """包一层 llm.chat，只为统计这一局用了多少 token（不改任何行为）。"""
    stats = {"calls": 0, "in": 0, "out": 0}
    orig = llm_layer.chat

    def patched(params: Optional[Dict[str, Any]] = None,
                ctx: Optional[Dict[str, Any]] = None,
                caller: Any = None) -> Dict[str, Any]:
        res = orig(params, ctx, caller)
        p = params or {}
        stats["calls"] += 1
        stats["in"] += int(len(str(p.get("prompt") or "")) * 0.75)
        stats["out"] += int(len(str((res or {}).get("text") or "")) * 0.75)
        return res

    llm_layer.chat = patched          # type: ignore[assignment]
    return stats, orig


def _cost(stats: Dict[str, int], tier: str) -> Dict[str, float]:
    pin, pout = FLASH_PRICE.get(tier, FLASH_PRICE["off"])
    return {"input_yuan": stats["in"] / 1e6 * pin,
            "output_yuan": stats["out"] / 1e6 * pout,
            "total_yuan": stats["in"] / 1e6 * pin + stats["out"] / 1e6 * pout}


def run(topic: str, news: str = "", news_file: Optional[str] = None,
        evidence_path: Optional[str] = None, rounds: int = 3,
        models: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    text = news
    if news_file:
        try:
            text = Path(news_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"新闻文件读不到（{exc}），继续用内联 --news 的内容", file=sys.stderr)

    evidence = _merge_news(load_evidence(evidence_path), text)
    project = install()
    srv = HubServer(host="127.0.0.1", port=0, db_path=":memory:", token=TOKEN)
    srv.start_background()
    worker = AgentRuntime(srv.url, "panel-once", project.manifests(),
                          token=TOKEN, concurrency=8, poll_wait=1)
    project.register(worker)

    stats, orig_chat = _wrap_token_counter()
    worker.start()
    try:
        task = worker.client.submit("debate", "run",
                                    panel_params(topic, evidence, max_rounds=rounds,
                                                 models=models),
                                    wait=True, timeout_s=600)
        if not isinstance(task, dict) or task.get("state") != "succeeded":
            raise RuntimeError(f"debate/run 失败：{(task or {}).get('error')}")
        panel = task.get("result") or {}

        rep = worker.client.submit(REPORT_CAP, REPORT_ACTION,
                                   {"topic": topic, "panel": panel},
                                   wait=True, timeout_s=120)
        if not isinstance(rep, dict) or rep.get("state") != "succeeded":
            raise RuntimeError(f"report/build 失败：{(rep or {}).get('error')}")
        report = rep.get("result") or {}
    finally:
        worker.stop()
        srv.stop()
        llm_layer.chat = orig_chat     # type: ignore[assignment]

    return {"panel": panel, "markdown": report.get("markdown") or "",
            "stats": stats,
            "cost_off": _cost(stats, "off"), "cost_peak": _cost(stats, "peak"),
            "simulated": any(str(t.get("text") or "").startswith("【模拟")
                             for t in (panel.get("transcript") or []))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="一局跑完就退出（无需起服务、无需 MCP）")
    ap.add_argument("--topic", required=True, help="议题，一句话")
    ap.add_argument("--news", default="", help="新闻/政策要点正文（DSH 搜完直接粘）")
    ap.add_argument("--news-file", default=None, help="或给一个文本文件路径")
    ap.add_argument("--evidence", default=None, help="证据包 JSON（可选，给了就优先用）")
    ap.add_argument("--rounds", type=int, default=3, help="轮数，默认 3")
    ap.add_argument("--models", action="append", default=[],
                    help="覆盖档位，形如 bear_memory=judge_pro，可重复")
    args = ap.parse_args(argv)

    models = {}
    for item in args.models:
        if "=" in item:
            k, v = item.split("=", 1)
            models[k.strip()] = v.strip()

    out = run(args.topic, args.news, args.news_file, args.evidence,
              args.rounds, models or None)

    p, s = out["panel"], out["stats"]
    print("=" * 60)
    print(f"议题：{args.topic}")
    print(f"发言：{s['calls']} 次模型调用 / {p.get('rounds')} 轮 / "
          f"{len(p.get('transcript') or [])} 条发言")
    print(f"裁决：{p.get('decision')}（停止原因 {p.get('stopped_reason')}）")
    if out["simulated"]:
        print("⚠ 本轮含【模拟·未配模型】发言，结论不作数；把这些 Key 填好再跑：")
        print("   config/llm.local.json 的 detective_fast / detective_deep / judge_pro")
        print("   （模拟发言不计输出 token，所以下面「输出」为 0 属正常）")
    print("-" * 60)
    print(f"token 估算：输入 ≈{s['in']:,} / 输出 ≈{s['out']:,}")
    print(f"花费（DeepSeek Flash 空闲时段）：约 {out['cost_off']['total_yuan']:.4f} 元")
    print(f"花费（DeepSeek Flash 高峰时段）：约 {out['cost_peak']['total_yuan']:.4f} 元")
    print("（估算非账单：按字符×0.75 折算，缓存命中按未命中保守计）")
    print("=" * 60)
    print(out["markdown"] or "（未生成报告）")
    return 0 if p.get("decision") else 1


if __name__ == "__main__":
    sys.exit(main())