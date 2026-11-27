"""事件官取数脚本：给 run_topic 的 `--calendar-cmd` 用。

    python examples/detective_panel/fetch_events.py --today 2026-11-28 --horizon 7

stdout 输出 `{"events": [{date, event, kind, basis, verify, source}], "meta": {...}}`，
`run_topic` 会自动解包 `events` 并写进证据包的 `calendar`。

只读日期规则（0 token）；想让它顺带看当日新闻里写死的日期，加
`--facts <新闻 JSON>`。模型推断那一路默认关（`--infer-url` 打开才走），
因为推断必须带 verify，没端点时宁可不出。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from multi_agent.frameworks.events import EventOfficer      # noqa: E402


def _load_facts(path: str) -> List[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"facts 读不了（{exc}），按空事实继续", file=sys.stderr)
        return []
    if isinstance(data, dict):
        data = data.get("items") or data.get("events") or []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def _infer_via_http(url: str, model: str, token: str):
    """可选的推理路：一次 OpenAI 兼容调用，只要 JSON 数组。"""
    def _call(payload: Dict[str, Any]) -> str:
        body = json.dumps({"model": model, "stream": False,
                           "messages": [{"role": "user",
                                         "content": json.dumps(payload, ensure_ascii=False)}]},
                          ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        return str(data["choices"][0]["message"]["content"])
    return _call


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="事件官：预定事件 → calendar JSON")
    ap.add_argument("--today", default=None, help="基准日 YYYY-MM-DD，默认今天")
    ap.add_argument("--horizon", type=int, default=7, help="往后看几天，默认 7")
    ap.add_argument("--facts", default=None, help="当日事实 JSON（新闻）")
    ap.add_argument("--rules", default=str(ROOT / "config/events/rules.json"))
    ap.add_argument("--infer-url", default=None, help="可选：OpenAI 兼容端点，开推理路")
    ap.add_argument("--infer-model", default="", help="推理路模型名")
    ap.add_argument("--infer-token", default="", help="推理路 Key（建议用环境变量传）")
    args = ap.parse_args(argv)

    facts = _load_facts(args.facts) if args.facts else []
    infer = (_infer_via_http(args.infer_url, args.infer_model, args.infer_token)
             if args.infer_url else None)

    out = EventOfficer(today=args.today, horizon_days=args.horizon,
                       rules_path=args.rules).scan(facts=facts, infer=infer)
    print(json.dumps(out, ensure_ascii=False))
    counts = out["meta"]["counts"]
    print(f"事件 {len(out['events'])} 条（fact {counts['fact']} / inferred "
          f"{counts['inferred']}），规则 {out['meta']['rules_loaded']} 条",
          file=sys.stderr)
    for err in out["meta"]["errors"]:
        print(f"  ! {err}", file=sys.stderr)
    return 0 if out["events"] else 1


if __name__ == "__main__":
    sys.exit(main())