"""共享数据协议：所有 agent 都通过 DBManager 读写同一份数据。

大佬 18:04 的要求
================
> 我希望你们走共同的这个数据协议，就是你们都共同的去上那个 DB manager 那点去拿数据。
> 比如说你那个供需平衡表是上他那拿，然后比如说股票就是拿新闻往他那塞。

为什么必须"唯一入口"
====================
如果每个 agent 各自存一份：

    平衡表在 openclaw 本地 → hermes 改了口径 → coze 读到的是旧的
    新闻在 coze 本地   → openclaw 不知道有新事件 → 判定用了陈数据

一旦跨机器，这个问题就无法靠"约定"解决，只能靠**物理上只有一个写入口**。

这一层做两件事：
1. **集合命名规范**（KeySchema）：谁写、谁读、什么格式，写死在代码里
2. **读写门面**（SharedData）：`put_balance` / `get_balance` / `append_news` ...
   全部转成 `db/*` 任务发到 DBManager 那台，**不直连库**

═══════════════════════════════════════════════════════════
集合命名规范（同 colors 前缀，机器可校验）
═══════════════════════════════════════════════════════════

    balance/{commodity}         平衡表最新版      写：A   读：Q/E/J/auditor
    balance_history/{commodity} 平衡表历史期次    写：A   读：auditor
    news                        新闻事件流        写：B   读：Q/E
    verdicts/{commodity}        品种判定          写：Q   读：E/J/auditor
    estimates/{commodity}       测算结果          写：C   读：E/auditor
    reports/{commodity}         报告正文          写：E   读：J/auditor
    audit/{project}/{key}       审计结论          写：auditor 读：J
    stock/financials/{code}     财报              写：A   读：micro/auditor
    stock/daily/{date}/{kind}   日频结果          写：macro/micro
    bond/elements/{code}        债券要素（永久）  写：A   读：auditor
    bond/quotes/{code}          债券行情          写：B   读：auditor
    pipeline_runs / pipeline_events / audit_log   框架自用

`KeySchema` 把这些写死，`SharedData` 只用它们拼 key —— 不会出现
`balance:copper` 和 `copper_balance` 两种写法各写一半的情况。

═══════════════════════════════════════════════════════════
用法
═══════════════════════════════════════════════════════════

    from multi_agent.shared_data import SharedData

    # 任意 agent 侧（不直连库，全部走 db/* 任务）
    data = SharedData(db_client)         # db_client = DBClient(hub_url, token)

    data.put_balance("copper", balance_dict, role="A")
    bal = data.get_balance("copper")
    data.append_news({"title": "...", "impact": "减产"}, role="B")
    data.put_verdict("copper", verdict_dict, role="Q")
    data.put_audit("copper", audit_report, role="auditor")

跨机器时 `db_client` 指向同一个 Hub —— **所有 agent 读写的都是同一份数据**。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Sequence

# ═══════════════════════════════════════════════════ 命名规范
COLLECTIONS = {
    "balance":          {"owner": "A",   "readers": ["Q", "E", "J", "auditor"]},
    "balance_history":  {"owner": "A",   "readers": ["auditor"]},
    "verdicts":         {"owner": "Q",   "readers": ["E", "J", "auditor"]},
    "estimates":        {"owner": "C",   "readers": ["E", "auditor"]},
    "reports":          {"owner": "E",   "readers": ["J", "auditor"]},
    "audit":            {"owner": "auditor", "readers": ["J"]},
    "stock_financials": {"owner": "A",   "readers": ["micro", "auditor"]},
    "stock_daily":      {"owner": "macro", "readers": ["report"]},
    "bond_elements":    {"owner": "A",   "readers": ["auditor"]},
    "bond_quotes":      {"owner": "B",   "readers": ["auditor"]},
    "demo":             {"owner": "any", "readers": []},
}

# 事件流（streams 表）
STREAMS = {
    "news":            {"owner": "B", "readers": ["Q", "E"]},
    "pipeline_events": {"owner": "coordinator", "readers": []},
    "audit_log":       {"owner": "auditor", "readers": ["J"]},
}

# 特殊流：按品类再分（news 已经有 30 天清理，这里给重要品种单独留）
STREAM_SEGMENTS = ("news:macro", "news:commodity", "news:equity")


class SchemaError(ValueError):
    """数据不符合共享协议（写错集合名、key 格式错等）。"""


def balance_key(commodity: str) -> str:
    if not commodity or not str(commodity).strip():
        raise SchemaError("commodity 不能为空")
    return str(commodity).strip().lower()


def validate_collection(name: str) -> str:
    if name not in COLLECTIONS:
        raise SchemaError(
            f"未登记的集合 {name!r}；可选 {sorted(COLLECTIONS)}")
    return name


def validate_stream(name: str) -> str:
    base = name.split(":")[0]
    if base not in STREAMS:
        raise SchemaError(f"未登记的事件流 {name!r}；可选 {sorted(STREAMS)}")
    return name


# ═══════════════════════════════════════════════════ 读写门面
class SharedData:
    """唯一的 DB 读写门面。所有方法都转成 `db/*` 任务（不直连库）。

    参数
    ----
    db: `DBClient`（走 Hub）或 `DBManager`（同进程直连，仅测试/单机用）
    role: 本 agent 的角色名，用于越权检查（写了不属于自己的集合会 warn）
    strict: True 时越权直接抛错；False 只在返回里标 `warn`
    """

    def __init__(self, db: Any, role: str = "any", strict: bool = False):
        self.db = db
        self.role = role
        self.strict = strict

    # ---------- 内部 ----------
    def _check_owner(self, name: str, kind: str = "doc") -> Optional[str]:
        table = COLLECTIONS if kind == "doc" else STREAMS
        base = name.split(":")[0]
        spec = table.get(base if kind == "doc" else name, None)
        if spec is None and kind == "stream":
            spec = STREAMS.get(base)
        if spec is None:
            return None
        owner = spec.get("owner")
        if owner in (None, "any", self.role):
            return None
        msg = f"角色 {self.role} 写了 {name}（owner={owner}）—— 违反共享协议"
        if self.strict:
            raise SchemaError(msg)
        return msg

    def _put(self, collection: str, key: str, value: Any,
             merge: bool = False) -> Dict[str, Any]:
        warn = self._check_owner(collection)
        out = self.db.write(collection, key, value, merge=merge) \
            if merge else self.db.write(collection, key, value)
        if isinstance(out, dict) and warn:
            out["warn"] = warn
        return out if isinstance(out, dict) else {"ok": True, "warn": warn}

    def _get(self, collection: str, key: str) -> Optional[Any]:
        r = self.db.read(collection, key)
        if not isinstance(r, dict):
            return None
        return r.get("value") if r.get("found") else None

    def _append(self, stream: str, payload: Any) -> Dict[str, Any]:
        warn = self._check_owner(stream, kind="stream")
        out = self.db.append(stream, payload)
        if isinstance(out, dict) and warn:
            out["warn"] = warn
        return out if isinstance(out, dict) else {"ok": True, "warn": warn}

    # ---------- 平衡表 ----------
    def put_balance(self, commodity: str, balance: Dict[str, Any],
                    merge: bool = True) -> Dict[str, Any]:
        """写平衡表最新版（默认 merge，保留既有字段）。"""
        key = balance_key(commodity)
        # 顺手存一份历史（auditor 要能看口径变化）
        hist_key = f"{key}:{balance.get('period') or _stamp()}"
        self._put("balance_history", hist_key, balance)
        return self._put("balance", key, balance, merge=merge)

    def get_balance(self, commodity: str) -> Optional[Dict[str, Any]]:
        return self._get("balance", balance_key(commodity))

    def list_balances(self, limit: int = 100) -> List[Dict[str, Any]]:
        r = self.db.list("balance", limit=limit)
        return (r or {}).get("items") or []

    # ---------- 新闻 ----------
    def append_news(self, item: Dict[str, Any],
                    stream: str = "news:commodity") -> Dict[str, Any]:
        payload = dict(item)
        payload.setdefault("ts", time.time())
        payload.setdefault("by", self.role)
        return self._append(validate_stream(stream), payload)

    def tail_news(self, limit: int = 50, after: int = 0,
                  stream: str = "news:commodity") -> List[Dict[str, Any]]:
        r = self.db.tail(stream, limit=limit, after=after)
        return (r or {}).get("items") or []

    # ---------- 判定 / 测算 / 报告 ----------
    def put_verdict(self, commodity: str, verdict: Dict[str, Any]) -> Dict[str, Any]:
        return self._put("verdicts", balance_key(commodity), verdict)

    def get_verdict(self, commodity: str) -> Optional[Dict[str, Any]]:
        return self._get("verdicts", balance_key(commodity))

    def put_estimate(self, commodity: str, est: Dict[str, Any]) -> Dict[str, Any]:
        return self._put("estimates", balance_key(commodity), est)

    def put_report(self, commodity: str, markdown: str,
                   meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._put("reports", balance_key(commodity),
                         {"markdown": markdown, **(meta or {})})

    def get_report(self, commodity: str) -> Optional[Dict[str, Any]]:
        return self._get("reports", balance_key(commodity))

    # ---------- 审计 ----------
    def put_audit(self, key: str, report: Dict[str, Any],
                  project: str = "balance_sheet") -> Dict[str, Any]:
        """审计结论同时落 docs（可查）和 audit_log 流（可追溯）。"""
        out = self._put("audit", f"{project}:{key}", report)
        self._append("audit_log", {"project": project, "key": key,
                                   "verdict": report.get("verdict"),
                                   "severity": report.get("severity"),
                                   "counts": report.get("counts"),
                                   "by": self.role, "ts": time.time()})
        return out

    def get_audit(self, key: str, project: str = "balance_sheet"
                  ) -> Optional[Dict[str, Any]]:
        return self._get("audit", f"{project}:{key}")

    # ---------- 股票 / 债券 ----------
    def put_financials(self, code: str, fin: Dict[str, Any]) -> Dict[str, Any]:
        return self._put("stock_financials", str(code), fin)

    def get_financials(self, code: str) -> Optional[Dict[str, Any]]:
        return self._get("stock_financials", str(code))

    def put_bond(self, code: str, elem: Dict[str, Any]) -> Dict[str, Any]:
        return self._put("bond_elements", str(code), elem)

    def get_bond(self, code: str) -> Optional[Dict[str, Any]]:
        return self._get("bond_elements", str(code))

    # ---------- 框架状态 ----------
    def put_run(self, run_id: str, run: Dict[str, Any]) -> Dict[str, Any]:
        return self._put("pipeline_runs", run_id, run)

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self._get("pipeline_runs", run_id)

    # ---------- 一致性自检 ----------
    def audit_protocol(self) -> Dict[str, Any]:
        """检查库里有没有"不符合命名规范"的集合（跨 agent 部署后必跑一次）。"""
        r = self.db.collections()
        found = {x["name"] for x in (r or {}).get("collections") or []}
        known = set(COLLECTIONS)
        unknown = sorted(found - known)
        return {"known": sorted(found & known), "unknown": unknown,
                "ok": not unknown,
                "hint": ("有未登记集合，检查是否有人绕过 SharedData 直接写库"
                         if unknown else "集合全部符合共享协议")}


def _stamp() -> str:
    return time.strftime("%Y%m%d", time.localtime())


# ═══════════════════════════════════════════════════ 跨 agent 示例
BALANCE_EXAMPLE = {
    "commodity": "copper", "period": "2025A", "unit": "万吨",
    "supply": {"refined_total": 2857.8},
    "demand": {"refined_usage": 2815.9},
    "balance": {"surplus_deficit": 41.9},
}


def demo(shared: SharedData) -> Dict[str, Any]:
    """跑一遍完整协作链，验证"大家读写同一份数据"。"""
    steps = {}
    # A 写平衡表
    steps["A_put_balance"] = shared.put_balance("copper", BALANCE_EXAMPLE)
    # B 塞新闻
    steps["B_append_news"] = shared.append_news(
        {"title": "某矿山减产", "impact": "减产", "commodity": "copper"})
    # Q 读平衡表 + 新闻 → 写判定
    bal = shared.get_balance("copper")
    news = shared.tail_news(limit=5)
    steps["Q_read"] = {"has_balance": bal is not None, "news": len(news)}
    steps["Q_put_verdict"] = shared.put_verdict(
        "copper", {"verdict": "accept", "based_on": "balance+news"})
    # E 读判定 → 写报告
    v = shared.get_verdict("copper")
    steps["E_read_verdict"] = v is not None
    steps["E_put_report"] = shared.put_report(
        "copper", "# 铜平衡表\n\n## 结论\n过剩 41.9 万吨")
    # auditor 审
    steps["auditor_put"] = shared.put_audit(
        "copper", {"verdict": "pass", "severity": "info",
                   "counts": {"error": 0, "warn": 0}})
    steps["protocol"] = shared.audit_protocol()
    return steps
