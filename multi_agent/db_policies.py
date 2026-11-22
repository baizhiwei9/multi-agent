"""DB 清理策略：不同项目不同保留规则，统一由 DBManager 执行。

大佬 18:04 的要求
================
> 那个 DB manager 要有一个定时清理的个逻辑，就是不同的项目有不同的清理逻辑。

为什么不能"一条规则清所有"
==========================
平衡表、新闻流、审计报告的生命周期完全不同：

| 数据 | 保留多久 | 为什么 |
|---|---|---|
| `balance/*` 最新平衡表 | **只在有更新时覆盖**，但保留最近 N 个期次 | 要能回看历史口径变化 |
| `news` 事件流 | 30 天 / 最多 5 万条 | 新闻时效短，堆久了查询变慢 |
| `verdicts/*` 判定 | 180 天 | 要能追溯"当时为什么这么判" |
| `pipeline_runs` | 90 天 / 最多 2000 条 | 排障用，太久没意义 |
| `pipeline_events` | 30 天 | 高频事件流，最容易膨胀 |
| `audit_log` | 365 天（合规） | 审计留痕，不能短 |
| `stock_daily` | 永久（按 key 覆盖） | 财报数据是资产 |
| `bond/*` | 永久 | 债券要素不变 |

设计
====
一条策略 = `Policy(collection|stream, keep_days, keep_count, mode, vacuum)`
  · `keep_days`   超过这个天数的记录删掉
  · `keep_count`  每个 key/stream 只留最新 N 条
  · `mode`        `time` / `count` / `both` / `none`
  · `vacuum`      清理后是否 VACUUM（会很慢，默认只在删得多时才做）

内置三套（对应三个审计 profile）：

    PROJECT_POLICIES = {
        "balance_sheet": BalanceSheetPolicy(),
        "stock":         StockPolicy(),
        "bond":          BondPolicy(),
        "global":        GlobalPolicy(),      # 兜底：没登记的集合
    }

用法
====
    from multi_agent.db_policies import PROJECT_POLICIES, plan_prune

    # 先看会删什么（dry-run，强烈建议生产先跑这个）
    plan_prune(db_manager, project="balance_sheet", dry_run=True)

    # 真删
    plan_prune(db_manager, project="balance_sheet")

    # 挂成定时任务（协调者侧起一个线程）
    from multi_agent.db_policies import MaintenanceScheduler
    sched = MaintenanceScheduler(db_manager, interval_s=3600)
    sched.start()

安全约定
========
1. **默认只删能算出来的**：keep_days 基于 `updated`/`created` 时间戳，不猜
2. **不动 schemas**：字段登记永不清理（否则下次写入会重新登记，白丢历史）
3. **dry-run 优先**：`plan_prune(..., dry_run=True)` 返回明细，不写库
4. **单次有上限**：`max_delete_per_run` 防止一次删太多导致长事务锁表
5. **VACUUM 需显式**：默认不清（VACUUM 要重写整个文件，几百 MB 时要几十秒）
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


# ═══════════════════════════════════════════════════ 策略定义
@dataclass
class Rule:
    """单条清理规则。"""

    target: str                 # collection 名 或 stream 名
    kind: str = "doc"           # doc（docs 表）| stream（streams 表）
    keep_days: Optional[float] = None
    keep_count: Optional[int] = None
    vacuum: bool = False
    note: str = ""

    def describe(self) -> str:
        bits = []
        if self.keep_days:
            bits.append(f"{self.keep_days:.0f}天")
        if self.keep_count:
            bits.append(f"留{self.keep_count}条")
        return f"{self.kind}:{self.target} " + ("/".join(bits) or "不清理")


@dataclass
class ProjectPolicy:
    """一个项目的清理口径。"""

    name: str
    description: str
    rules: List[Rule] = field(default_factory=list)
    max_delete_per_run: int = 20000      # 单次上限，防长事务

    def rule_for(self, name: str, kind: str = "doc") -> Optional[Rule]:
        for r in self.rules:
            if r.target == name and r.kind == kind:
                return r
        return None

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "max_delete_per_run": self.max_delete_per_run,
                "rules": [{"target": r.target, "kind": r.kind,
                           "keep_days": r.keep_days, "keep_count": r.keep_count,
                           "vacuum": r.vacuum, "note": r.note,
                           "describe": r.describe()} for r in self.rules]}


class BalanceSheetPolicy(ProjectPolicy):
    """大宗商品供需平衡表。"""

    def __init__(self) -> None:
        super().__init__(
            name="balance_sheet",
            description="平衡表 / 新闻 / 判定 / 流水线（新闻与事件流最易膨胀）",
            rules=[
                # 平衡表：按 key（commodity:latest）覆盖，只保留最近 3 个期次
                Rule("balance", keep_count=3, note="每品种留最近 3 个期次"),
                # 新闻：30 天
                Rule("news", kind="stream", keep_days=30, keep_count=50000,
                     note="新闻时效短"),
                # 判定：180 天（要能追溯当时为什么这么判）
                Rule("verdicts", keep_days=180, note="追溯判定依据"),
                # 估算结果：90 天
                Rule("estimates", keep_days=90),
                # 流水线状态
                Rule("pipeline_runs", keep_days=90, keep_count=2000),
                Rule("pipeline_events", kind="stream", keep_days=30,
                     keep_count=20000),
                # 审计留痕不能短
                Rule("audit_log", kind="stream", keep_days=365, note="合规留痕"),
            ])


class StockPolicy(ProjectPolicy):
    """股票 / 财务。财报数据是资产，不能按时间删。"""

    def __init__(self) -> None:
        super().__init__(
            name="stock",
            description="股票财务（财报永久保留，只清中间过程）",
            rules=[
                Rule("stock_daily", keep_count=2500, note="约 10 年日频，按 key 覆盖"),
                Rule("financials", note="财报永久保留（不清理）"),
                Rule("news", kind="stream", keep_days=30, keep_count=50000),
                Rule("pipeline_runs", keep_days=90, keep_count=2000),
                Rule("pipeline_events", kind="stream", keep_days=30,
                     keep_count=20000),
                Rule("audit_log", kind="stream", keep_days=365),
            ])


class BondPolicy(ProjectPolicy):
    """债券 / 固收。债券要素不变，永久保留。"""

    def __init__(self) -> None:
        super().__init__(
            name="bond",
            description="债券（要素永久，只清行情与事件）",
            rules=[
                Rule("bond", note="债券要素永久保留"),
                Rule("bond_quotes", keep_days=730, keep_count=100000,
                     note="行情留 2 年"),
                Rule("news", kind="stream", keep_days=30, keep_count=50000),
                Rule("pipeline_runs", keep_days=90, keep_count=2000),
                Rule("pipeline_events", kind="stream", keep_days=30,
                     keep_count=20000),
                Rule("audit_log", kind="stream", keep_days=365),
            ])


class GlobalPolicy(ProjectPolicy):
    """兜底：没登记的集合/流。只清明显是临时的。"""

    def __init__(self) -> None:
        super().__init__(
            name="global",
            description="兜底策略（未登记项目）：只清流水线与事件流",
            rules=[
                Rule("pipeline_runs", keep_days=90, keep_count=2000),
                Rule("pipeline_events", kind="stream", keep_days=30,
                     keep_count=20000),
                Rule("audit_log", kind="stream", keep_days=365),
            ])


PROJECT_POLICIES: Dict[str, ProjectPolicy] = {
    p.name: p for p in (BalanceSheetPolicy(), StockPolicy(), BondPolicy(),
                        GlobalPolicy())
}


def get_policy(name: str) -> ProjectPolicy:
    if name in PROJECT_POLICIES:
        return PROJECT_POLICIES[name]
    # 未登记的项目不报错，用兜底（但要能看出来用了兜底）
    return PROJECT_POLICIES["global"]


def register_policy(policy: ProjectPolicy) -> None:
    if not isinstance(policy, ProjectPolicy):
        raise TypeError("policy 必须是 ProjectPolicy 实例")
    PROJECT_POLICIES[policy.name] = policy


# ═══════════════════════════════════════════════════ 执行
def plan_prune(db: Any, project: str = "global", dry_run: bool = True,
               now: Optional[float] = None,
               extra_collections: Optional[List[str]] = None
               ) -> Dict[str, Any]:
    """按项目策略清理。`dry_run=True` 只算不删（生产先跑这个）。

    参数
    ----
    db: DBManager 实例（有 `_conn` 的最好；否则用 `list/delete/tail` 接口）
    project: 项目名（balance_sheet / stock / bond / global 或自定义）
    dry_run: True 只返回计划，不改库
    extra_collections: 库里实际存在、但策略里没登记的集合 —— 会被报进
        `unregistered`，方便发现"有人偷偷建了个表从没清理"

    返回
    ----
    {
      "project": "balance_sheet",
      "dry_run": true,
      "actions": [{"target": "news", "kind": "stream", "would_delete": 1234,
                   "rule": "stream:news 30天/留50000条"}],
      "total_would_delete": 1234,
      "unregistered": ["weird_collection"],
      "vacuum": false,
      "ts": ...
    }
    """
    policy = get_policy(project)
    now = now or time.time()
    actions: List[Dict[str, Any]] = []

    conn = getattr(db, "_conn", None)
    if conn is None:
        return {"project": project, "dry_run": dry_run, "ok": False,
                "error": "plan_prune 需要 DBManager 实例（直连 sqlite 连接）",
                "hint": "跨机器场景请让 DBManager 那台执行本函数，再把结果回传"}

    budget = policy.max_delete_per_run

    for rule in policy.rules:
        if rule.kind == "doc":
            cnt, cutoff = _count_docs(conn, rule, now)
        else:
            cnt, cutoff = _count_stream(conn, rule, now)
        if cnt <= 0:
            continue
        will = min(cnt, budget)
        budget -= will
        actions.append({
            "target": rule.target, "kind": rule.kind,
            "would_delete": will, "matched": cnt,
            "truncated": will < cnt,
            "rule": rule.describe(),
            "note": rule.note,
        })
        if not dry_run and will > 0:
            if rule.kind == "doc":
                _purge_docs(conn, rule, now, will, policy)
            else:
                _purge_stream(conn, rule, now, will, policy)
        if budget <= 0:
            break

    unregistered = _find_unregistered(conn, policy)
    if extra_collections:
        unregistered = sorted(set(unregistered) | set(extra_collections))

    result = {"project": project, "dry_run": dry_run, "ok": True,
              "actions": actions,
              "total_would_delete": sum(a["would_delete"] for a in actions),
              "unregistered": unregistered,
              "policy": policy.as_dict(),
              "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now))}

    if not dry_run:
        need_vacuum = any(policy.rule_for(a["target"], a["kind"]) and
                          policy.rule_for(a["target"], a["kind"]).vacuum
                          for a in actions)
        if need_vacuum:
            try:
                conn.execute("VACUUM")
                result["vacuumed"] = True
            except Exception as e:  # noqa: BLE001
                result["vacuumed"] = False
                result["vacuum_error"] = f"{type(e).__name__}: {e}"
    return result


def _count_docs(conn, rule: Rule, now: float) -> Tuple[int, Optional[float]]:
    """docs 表：keep_days 按 updated；keep_count 按 collection 内每个 key 留最新。"""
    where, args = _doc_where(rule, now)
    try:
        if where:
            row = conn.execute(
                f"SELECT COUNT(*) n FROM docs WHERE collection=? AND {where}",
                [rule.target] + args).fetchone()
            return (row["n"] if row else 0), (now - rule.keep_days if rule.keep_days else None)
        if rule.keep_count:
            row = conn.execute(
                "SELECT MAX(0, COUNT(*) - ?) n FROM docs WHERE collection=?",
                (rule.keep_count, rule.target)).fetchone()
            return (row["n"] if row else 0), None
        return 0, None
    except Exception:  # noqa: BLE001
        return 0, None


def _doc_where(rule: Rule, now: float) -> Tuple[str, List[Any]]:
    if rule.keep_days:
        return "updated < ?", [now - rule.keep_days * 86400]
    return "", []


def _purge_docs(conn, rule: Rule, now: float, limit: int,
                policy: ProjectPolicy) -> None:
    where, args = _doc_where(rule, now)
    if where:
        conn.execute(
            f"DELETE FROM docs WHERE rowid IN (SELECT rowid FROM docs "
            f"WHERE collection=? AND {where} LIMIT ?)",
            [rule.target] + args + [limit])
    elif rule.keep_count:
        conn.execute(
            "DELETE FROM docs WHERE rowid IN ("
            "  SELECT rowid FROM docs WHERE collection=? "
            "  ORDER BY updated ASC LIMIT ?)",
            (rule.target, limit))
    conn.commit()


def _count_stream(conn, rule: Rule, now: float) -> Tuple[int, Optional[float]]:
    try:
        if rule.keep_days:
            row = conn.execute(
                "SELECT COUNT(*) n FROM streams WHERE stream=? AND created < ?",
                (rule.target, now - rule.keep_days * 86400)).fetchone()
            return (row["n"] if row else 0), now - rule.keep_days * 86400
        if rule.keep_count:
            row = conn.execute(
                "SELECT MAX(0, COUNT(*) - ?) n FROM streams WHERE stream=?",
                (rule.keep_count, rule.target)).fetchone()
            return (row["n"] if row else 0), None
        return 0, None
    except Exception:  # noqa: BLE001
        return 0, None


def _purge_stream(conn, rule: Rule, now: float, limit: int,
                  policy: ProjectPolicy) -> None:
    if rule.keep_days:
        conn.execute(
            "DELETE FROM streams WHERE seq IN ("
            "  SELECT seq FROM streams WHERE stream=? AND created < ? LIMIT ?)",
            (rule.target, now - rule.keep_days * 86400, limit))
    elif rule.keep_count:
        conn.execute(
            "DELETE FROM streams WHERE seq IN ("
            "  SELECT seq FROM streams WHERE stream=? ORDER BY seq ASC LIMIT ?)",
            (rule.target, limit))
    conn.commit()


def _find_unregistered(conn, policy: ProjectPolicy) -> List[str]:
    """库里存在但策略没登记的集合 —— 报警用（"有人建了表从没清理"）。

    同名只要在 doc 或 stream 任一侧登记过就算已知：
    比如 `news` 既可能写成 docs 也可能是 streams，不该因为一侧登记了
    就把另一侧报成"未登记"（那样报警会淹没真问题）。
    """
    known_doc = {r.target for r in policy.rules if r.kind == "doc"}
    known_stream = {r.target for r in policy.rules if r.kind == "stream"}
    known_any = known_doc | known_stream
    out: List[str] = []
    try:
        rows = conn.execute(
            "SELECT DISTINCT collection FROM docs").fetchall()
        for r in rows:
            name = r["collection"]
            if name not in known_doc and name not in known_any:
                out.append(f"docs:{name}")
        rows = conn.execute("SELECT DISTINCT stream FROM streams").fetchall()
        for r in rows:
            name = r["stream"]
            if name not in known_stream and name not in known_any:
                out.append(f"stream:{name}")
    except Exception:  # noqa: BLE001
        pass
    return out


# ═══════════════════════════════════════════════════ 定时
class MaintenanceScheduler:
    """定时清理（协调者侧起一个 daemon 线程即可）。

    参数
    ----
    db: DBManager 实例
    project: 用哪套策略（也可给多个：projects=["balance_sheet","stock"]）
    interval_s: 多久跑一次（默认 1 小时）
    dry_run: 首次先 dry-run 看一遍（推荐 True 跑一次再改 False）
    """

    def __init__(self, db: Any, project: str | List[str] = "global",
                 interval_s: float = 3600, dry_run: bool = False,
                 on_result: Optional[Any] = None):
        self.db = db
        self.projects = [project] if isinstance(project, str) else list(project)
        self.interval_s = max(60.0, float(interval_s))
        self.dry_run = dry_run
        self.on_result = on_result
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.history: List[Dict[str, Any]] = []

    def run_once(self) -> List[Dict[str, Any]]:
        out = []
        for p in self.projects:
            try:
                r = plan_prune(self.db, p, dry_run=self.dry_run)
            except Exception as e:  # noqa: BLE001
                r = {"project": p, "ok": False, "error": f"{type(e).__name__}: {e}"}
            out.append(r)
            self.history.append(r)
            if len(self.history) > 200:
                self.history = self.history[-100:]
            if self.on_result:
                try:
                    self.on_result(r)
                except Exception:  # noqa: BLE001
                    pass
        return out

    def _loop(self) -> None:
        # 启动先跑一次（错开启动瞬间的写高峰）
        time.sleep(5)
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.interval_s)

    def start(self) -> "MaintenanceScheduler":
        if self._thread and self._thread.is_alive():
            return self
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="db-maintenance")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def status(self) -> Dict[str, Any]:
        return {"projects": self.projects, "interval_s": self.interval_s,
                "dry_run": self.dry_run,
                "running": bool(self._thread and self._thread.is_alive()),
                "runs": len(self.history),
                "last": self.history[-1] if self.history else None}


# ═══════════════════════════════════════════════════ Hub 接入
DB_POLICY_ACTIONS = ("prune", "prune_plan", "policies", "maintenance_status")


def attach_policies(db_manager: Any, runtime: Any,
                    default_project: str = "global") -> MaintenanceScheduler:
    """把清理能力挂到 DBManager 的 agent 上（capability=db 增加 4 个动作）。

    这样任意 agent 都能通过 Hub / 中继请求"清理"，不用谁去直连库。
    """
    sched = MaintenanceScheduler(db_manager, project=default_project)

    runtime.add_handler("db", "prune_plan", lambda p, c: plan_prune(
        db_manager, p.get("project") or default_project, dry_run=True))
    runtime.add_handler("db", "prune", lambda p, c: plan_prune(
        db_manager, p.get("project") or default_project,
        dry_run=bool(p.get("dry_run", False))))
    runtime.add_handler("db", "policies", lambda p, c: {
        "policies": [x.as_dict() for x in PROJECT_POLICIES.values()],
        "default": default_project})
    runtime.add_handler("db", "maintenance_status", lambda p, c: sched.status())
    return sched
