"""事件官：发现「预定要发生」的事，并强制标注它到底是事实还是推断。

为什么单开一层
--------------
新闻官（`frameworks/news`）管**已发生**：抓到的文本里有什么。
事件官管**将发生**：指数调仓、期权到期、季末结算、发布冻结、证书到期……
这些东西在新闻流里往往一条都没有（实测：MSCI 11 月审议当天，新浪 7x24 + 央视
72 小时 520 条里命中 0 条），只能靠日期规则与日历推出来。

三路生成，互不掺和
------------------
    rules  规则路（纯代码，0 token）：月末/季末/第几个周几这类可编码的规律
    facts  事实路（正则抽日期 + 事件词）：文本里明写了日期的，才算 fact
    infer  推理路（大模型，可选）：模型基于先验想起的，一律 inferred

任何一条都必须回答两个问题：`basis`（凭什么觉得是这天）和 `verify`（怎么核）。
`kind="inferred"` 而没有 `verify` 的条目**直接丢弃**——宁可少一条，也不给
一个看起来对、实际没法验证的日期。

用法
----
    from multi_agent.frameworks.events import EventOfficer

    out = EventOfficer(today="2026-11-28").scan(facts=news_items, horizon_days=7)
    out["events"]      # [{date, event, kind, basis, verify, source}, ...]
    out["meta"]        # {today, horizon_days, counts, errors, rules_loaded}

命令行：

    python -m multi_agent.frameworks.events.officer --today 2026-11-28 --horizon 7

输出 JSON 可直接喂给 `detective_panel` 的 `--calendar-cmd`。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

DEFAULT_RULES = "config/events/rules.json"

#: 事务词：与日期同时出现时，才认为文本在讲一件"事"，而不是单纯列日期
_EVENT_WORDS = ("审议", "调整", "调仓", "再平衡", "生效", "实施", "到期", "结算",
                "截止", "窗口", "发布", "会议", "纳入", "剔除", "冻结", "上线",
                "审核", "解禁", "缴款", "交割")

_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})|(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


# ───────────────────────────────────────────── 1. 日期工具
def parse_day(value: Any) -> date:
    """认 date / datetime / 'YYYY-MM-DD' / 'YYYYMMDD'；认不出就抛，不猜。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"日期认不出：{value!r}（要 YYYY-MM-DD 或 YYYYMMDD）")


def last_business_day(year: int, month: int) -> date:
    """当月最后一个工作日（周一至周五）。

    只看星期，**不考虑法定节假日**——这是近似，basis 里会写明，
    真正排期请用你自己的交易日历覆盖。
    """
    if month == 12:
        day = date(year, 12, 31)
    else:
        day = date(year, month + 1, 1) - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def nth_weekday(year: int, month: int, weekday: int, n: int) -> Optional[date]:
    """当月的第 n 个某星期几（n 从 1 起）；不存在返回 None。"""
    if month == 12:
        first = date(year, 12, 1)
    else:
        first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    day = first + timedelta(days=offset + 7 * (n - 1))
    return day if day.month == month else None


# ───────────────────────────────────────────── 2. 规则路（0 token）
def _match_when(when: Dict[str, Any], today: date) -> Optional[date]:
    """判断规则条件是否命中，命中则返回事件日期。"""
    month = today.month
    if "month_in" in when and month not in when["month_in"]:
        return None
    if when.get("quarter_end") and month not in (3, 6, 9, 12):
        return None

    if when.get("month_end_last_business_day"):
        target = last_business_day(today.year, month)
        return target

    nth = when.get("nth_weekday_in_month")
    if isinstance(nth, dict):
        wd = nth.get("weekday")
        if isinstance(wd, str):
            wd = _WEEKDAYS.get(wd.lower())
        if wd is None:
            return None
        return nth_weekday(today.year, month, int(wd), int(nth.get("n") or 1))

    if "day" in when:
        try:
            return date(today.year, month, int(when["day"]))
        except ValueError:
            return None
    return None


def from_rules(today: date, horizon: int, rules: List[Dict[str, Any]],
               errors: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("enable") is False:
            continue
        when = rule.get("when") or {}
        try:
            day = _match_when(when, today)
        except Exception as exc:        # noqa: BLE001 - 单条规则坏了不影响其它
            errors.append(f"规则 {rule.get('id')} 求值失败：{type(exc).__name__}: {exc}")
            continue
        if day is None or not (0 <= (day - today).days <= horizon):
            continue
        out.append({
            "date": day.isoformat(),
            "event": str(rule.get("event") or "").strip(),
            "kind": str(rule.get("kind") or "inferred"),
            "basis": str(rule.get("basis") or f"规则命中：{rule.get('id')}").strip(),
            "verify": str(rule.get("verify") or "").strip(),
            "source": f"rule:{rule.get('id') or '?'}",
        })
    return out


# ───────────────────────────────────────────── 3. 事实路（文本里明写了日期）
def _extra_dates(text: str, today: date) -> List[date]:
    out: List[date] = []
    for m in _DATE_RE.finditer(text or ""):
        try:
            if m.group(1):
                out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
            else:
                month, day = int(m.group(4)), int(m.group(5))
                candidate = date(today.year, month, day)
                if candidate < today:            # 说的是明年的事
                    candidate = date(today.year + 1, month, day)
                out.append(candidate)
        except ValueError:
            continue
    return out


def from_facts(facts: List[Dict[str, Any]], today: date, horizon: int) -> List[Dict[str, Any]]:
    """从文本里抽「日期 + 事件词」的组合；有原文就是事实，不必推理。"""
    out: List[Dict[str, Any]] = []
    for item in facts or []:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        body = str(item.get("text") or item.get("summary") or "").strip()
        text = f"{title} {body}"
        if not text.strip() or not any(w in text for w in _EVENT_WORDS):
            continue
        for day in _extra_dates(text, today):
            if not (0 <= (day - today).days <= horizon):
                continue
            out.append({
                "date": day.isoformat(),
                "event": title[:80] or "（原文标题为空）",
                "kind": "fact",
                "basis": f"原文写明日期（{str(item.get('source') or '未标来源')}）",
                "verify": str(item.get("url") or "").strip() or "核对原文",
                "source": "facts",
            })
    return out


# ───────────────────────────────────────────── 4. 推理路（可选，必须带 verify）
def from_inference(today: date, horizon: int, facts: List[Dict[str, Any]],
                   infer: Optional[Callable[[Dict[str, Any]], Any]],
                   errors: List[str]) -> List[Dict[str, Any]]:
    """把「模型想起的」接进来，但只收结构完整、且带 verify 的条目。"""
    if infer is None:
        return []
    payload = {
        "today": today.isoformat(),
        "horizon_days": horizon,
        "heads": [str((f or {}).get("title") or "")[:80] for f in (facts or [])[:40]],
        "ask": ("基于日期与常识，列出未来几天可能的预定事件；每条给 "
                "date/event/basis/verify，拿不准也要写 verify 指向怎么核。"
                "只输出 JSON 数组。"),
    }
    try:
        raw = infer(payload)
    except Exception as exc:            # noqa: BLE001 - 推理失败不影响规则路
        errors.append(f"推理路失败：{type(exc).__name__}: {exc}")
        return []

    items = raw
    if isinstance(raw, str):
        try:
            items = json.loads(raw)
        except ValueError:
            errors.append("推理路：返回不是 JSON，已丢弃")
            return []
    if isinstance(items, dict):
        items = items.get("events") or []

    out: List[Dict[str, Any]] = []
    for entry in items if isinstance(items, list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            day = parse_day(entry.get("date"))
        except ValueError:
            continue
        if not (0 <= (day - today).days <= horizon):
            continue
        verify = str(entry.get("verify") or "").strip()
        if not verify:                   # 没有核验方式的推断，一律不要
            continue
        out.append({
            "date": day.isoformat(),
            "event": str(entry.get("event") or "").strip()[:120] or "（推理事件）",
            "kind": "inferred",
            "basis": str(entry.get("basis") or "模型推断（无规则依据）").strip(),
            "verify": verify,
            "source": "infer",
        })
    return out


# ───────────────────────────────────────────── 5. 事件官
def load_rules(path: str = DEFAULT_RULES) -> List[Dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("rules") or []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


class EventOfficer:
    """事件官：日期规则 + 文本日期 +（可选）模型推断 → 候选事件清单。"""

    name = "event-officer"
    description = "发现预定事件（指数调仓/期权到期/季末/冻结），并标注 fact 或 inferred"

    def __init__(self, today: Any = None, horizon_days: int = 7,
                 rules: Optional[List[Dict[str, Any]]] = None,
                 rules_path: str = DEFAULT_RULES) -> None:
        self.today = parse_day(today) if today else date.today()
        self.horizon_days = max(0, int(horizon_days))
        self.rules = rules if rules is not None else load_rules(rules_path)

    def scan(self, facts: Optional[List[Dict[str, Any]]] = None,
             infer: Optional[Callable[[Dict[str, Any]], Any]] = None,
             horizon_days: Optional[int] = None) -> Dict[str, Any]:
        horizon = self.horizon_days if horizon_days is None else max(0, int(horizon_days))
        errors: List[str] = []
        if not self.rules:
            errors.append("规则表为空（config/events/rules.json 未读到）")

        collected = (from_rules(self.today, horizon, self.rules, errors)
                     + from_facts(facts or [], self.today, horizon)
                     + from_inference(self.today, horizon, facts or [], infer, errors))

        merged: List[Dict[str, Any]] = []
        seen = set()
        for item in sorted(collected, key=lambda x: (x["date"], x["event"])):
            key = (item["date"], item["event"][:30])
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)

        counts = {"fact": 0, "inferred": 0}
        for item in merged:
            counts[item["kind"] if item["kind"] in counts else "inferred"] += 1

        return {
            "events": merged,
            "meta": {
                "today": self.today.isoformat(),
                "horizon_days": horizon,
                "counts": counts,
                "rules_loaded": len(self.rules),
                "errors": errors,
                "note": ("kind=fact 表示原文有据；kind=inferred 是规则/推断得出，"
                         "必须按 verify 核过才能当事实用。"),
            },
        }


# ───────────────────────────────────────────── 6. CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="事件官：预定事件扫描（输出 JSON）")
    ap.add_argument("--today", default=None, help="基准日 YYYY-MM-DD，默认今天")
    ap.add_argument("--horizon", type=int, default=7, help="往后看几天，默认 7")
    ap.add_argument("--facts", default=None, help="当日事实 JSON（[{title,text,source,url}] 或 {\"items\":[...]}")
    ap.add_argument("--rules", default=DEFAULT_RULES, help="规则表路径")
    ap.add_argument("--out", default=None, help="把 JSON 写到这个文件（默认打印到 stdout）")
    args = ap.parse_args(argv)

    facts: List[Dict[str, Any]] = []
    if args.facts:
        try:
            with open(args.facts, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                loaded = loaded.get("items") or loaded.get("events") or []
            facts = [x for x in loaded if isinstance(x, dict)] if isinstance(loaded, list) else []
        except (OSError, ValueError) as exc:
            print(f"facts 读不了（{exc}），按空事实继续", file=sys.stderr)

    officer = EventOfficer(today=args.today, horizon_days=args.horizon,
                           rules_path=args.rules)
    out = officer.scan(facts=facts)
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"事件 {len(out['events'])} 条 → {args.out}", file=sys.stderr)
    else:
        print(text)
    return 0 if out["events"] else 1


if __name__ == "__main__":
    sys.exit(main())