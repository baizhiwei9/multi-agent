"""一局跑完就退出：不需要单独起 Hub，也不需要配 MCP。

给手机 / 没有终端的场景用：把这一条命令交给 DSH 执行即可。

    # 关键词由框架内的检索官去调搜索能力（推荐）
    python examples/detective_panel/run_once.py \
        --topic "国常会后A股是否开始构筑底部" \
        --keywords "国常会,逆周期,构筑底部" \
        --search-mcp "<你的搜索 MCP 启动命令>"

    # 已经有新闻池文件（离线/回放）也能跑
    NEWS_FEED_FILE=/tmp/news.json python examples/detective_panel/run_once.py \
        --topic "..." --keywords "国常会"

    # 最省事：直接把新闻正文粘进来（不检索）
    python examples/detective_panel/run_once.py --topic "..." --news "要点……"

检索发生在框架内：`news/collect` 把关键词发给 Hub 上的 `search/*` 能力
（`--search-mcp` 会用 `MCPAgent` 把一个搜索 MCP 挂成 `search` 能力），
命中过滤、去重、拼摘要都在框架里完成，调用方只递关键词。

它会做五件事，然后退出：
1. 内存里起 Hub + worker（不占端口、不留进程）
2. 若给了关键词：先检索 → 组装证据包（检索不到就明说，不编造）
3. 跑 `debate/run`（两正两反四席，默认 3 轮，只在最后裁一次）
4. 拼 Markdown 报告
5. 按实际 token 用量算出这一局花了多少钱（DeepSeek Flash 峰谷两档）
"""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from multi_agent.client import AgentRuntime          # noqa: E402
from multi_agent.integrations import llm as llm_layer  # noqa: E402
from multi_agent.mcp import MCPAgent                  # noqa: E402
from multi_agent.server import HubServer              # noqa: E402

from examples.detective_panel.project import (       # noqa: E402
    NEWS_ACTION, NEWS_CAP, REPORT_ACTION, REPORT_CAP,
    install, load_evidence, panel_params)

TOKEN = "local-once"
DEFAULT_TOPIC = "（未给议题）"

#: DeepSeek Flash 系列单价（元 / 百万 token），2026-09-10 12:00 起的空闲时段口径：
#: 输入（缓存未命中）1 元、输出 4 元；高峰时段（工作日 9-12、14-18）翻倍。
#: 价格会变，跑前请按官方价格页核对；这里只用来给你一个量级。
FLASH_PRICE = {"off": (1.0, 4.0), "peak": (2.0, 8.0)}


def _merge_news(evidence: Dict[str, Any], news: str,
                digest: str = "", meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把新闻/检索结果塞进证据包；不动行情字段（那些必须靠取数补，不能靠新闻猜）。"""
    ev = dict(evidence or {})
    event = dict(ev.get("event") or {})
    text = (digest or "").strip() or news.strip()
    if text:
        event["news_digest"] = text[:8000]
    if meta:
        event["news_meta"] = meta
    if text:
        event.setdefault("source", "框架内检索（news/collect）" if digest
                         else "调用方粘贴，未经二次核验")
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
        models: Optional[Dict[str, str]] = None,
        keywords: str = "", search_mcp: Optional[str] = None,
        search_via: str = "search", search_action: str = "search") -> Dict[str, Any]:
    text = news
    if news_file:
        try:
            text = Path(news_file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"新闻文件读不到（{exc}），继续用内联 --news 的内容", file=sys.stderr)

    evidence = load_evidence(evidence_path)
    project = install(search_via=search_via, search_action=search_action)
    srv = HubServer(host="127.0.0.1", port=0, db_path=":memory:", token=TOKEN)
    srv.start_background()

    searcher: Optional[MCPAgent] = None
    if search_mcp:
        searcher = MCPAgent(srv.url, "panel-searcher", shlex.split(search_mcp),
                            token=TOKEN, prefix=search_via, concurrency=2)
        info = searcher.start()
        print(f"已挂搜索 MCP → {search_via}/*：{info.get('tools')}")

    worker = AgentRuntime(srv.url, "panel-once", project.manifests(),
                          token=TOKEN, concurrency=8, poll_wait=1)
    project.register(worker)

    stats, orig_chat = _wrap_token_counter()
    worker.start()
    news_info: Dict[str, Any] = {}
    try:
        if keywords:
            task = worker.client.submit(
                NEWS_CAP, NEWS_ACTION,
                {"keywords": keywords, "limit": 12, "via": search_via},
                wait=True, timeout_s=180)
            payload = (task or {}).get("result") if isinstance(task, dict) else None
            if isinstance(task, dict) and task.get("state") == "succeeded" and isinstance(payload, dict):
                if payload.get("degraded"):
                    news_info = {"degraded": True, "reason": payload.get("reason")}
                    print(f"检索未命中：{payload.get('reason')}；"
                          f"（如给了 --news 将退回粘贴内容）", file=sys.stderr)
                else:
                    news_info = {"source": payload.get("source"),
                                 "keywords": payload.get("keywords"),
                                 "scanned": payload.get("scanned"),
                                 "kept": payload.get("kept")}
                    evidence = _merge_news(evidence, text, payload.get("digest") or "",
                                           news_info)
                    print(f"检索命中 {payload.get('kept')} 条"
                          f"（扫描 {payload.get('scanned')}，源 {payload.get('source')}）")
            else:
                news_info = {"degraded": True,
                             "reason": (task or {}).get("error") if isinstance(task, dict) else "任务失败"}
                print(f"检索任务失败：{news_info['reason']}", file=sys.stderr)

        if not evidence.get("event", {}).get("news_digest") and text.strip():
            evidence = _merge_news(evidence, text, "", {"source": "调用方粘贴"})

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
        if searcher is not None:
            searcher.stop()
        srv.stop()
        llm_layer.chat = orig_chat     # type: ignore[assignment]

    return {"panel": panel, "markdown": report.get("markdown") or "",
            "stats": stats, "news": news_info,
            "cost_off": _cost(stats, "off"), "cost_peak": _cost(stats, "peak"),
            "simulated": any(str(t.get("text") or "").startswith("【模拟")
                             for t in (panel.get("transcript") or []))}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="一局跑完就退出（无需起服务、无需 MCP）")
    ap.add_argument("--topic", required=True, help="议题，一句话")
    ap.add_argument("--keywords", default="",
                    help="关键词（逗号分隔）：由框架内的检索官去调搜索能力")
    ap.add_argument("--search-mcp", default=None,
                    help="把搜索 MCP 挂成 search 能力，例：--search-mcp \"npx -y some-search-mcp\"")
    ap.add_argument("--search-via", default="search", help="挂载后的能力名，默认 search")
    ap.add_argument("--search-action", default="search", help="该能力下要调的工具名，默认 search")
    ap.add_argument("--news", default="", help="直接给新闻正文（不检索时用）")
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
              args.rounds, models or None, args.keywords, args.search_mcp,
              args.search_via, args.search_action)

    p, s = out["panel"], out["stats"]
    print("=" * 60)
    print(f"议题：{args.topic}")
    if out["news"]:
        n = out["news"]
        if n.get("degraded"):
            print(f"检索：未命中（{n.get('reason')}）")
        else:
            print(f"检索：{n.get('kept')} 条命中 / 扫描 {n.get('scanned')} / 源 {n.get('source')}")
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