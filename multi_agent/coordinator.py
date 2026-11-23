"""Coordinator：按 DAG 推进任务，结果落 DBManager。

它既是"协调者 Agent"（注册 capability=coordinator，接受 start/status/notify），
也可以在进程内直接 `Coordinator.start()` 使用。

流程（大宗平衡表）
------------------
A 建表 → db.write 平衡表 → B 新闻 → db.append 新闻流
      → Q 修正判定（仅当新闻事件 > 0）→ db.write 判定
      → E 报告 → **审计官 auditor 审报告** → J 终审汇总

**审计级别由协调者控制**（这是设计要点："能不能联网烧 token"不该由 Auditor 自己决定）：

    coord.start("balance_sheet", {"commodity": "copper",
                                 "audit_level": "rule"})   # 默认：纯规则，0 token
    coord.start("balance_sheet", {"commodity": "copper",
                                 "audit_level": "stat"})   # + 统计检验
    coord.start("balance_sheet", {"commodity": "copper",
                                 "audit_level": "llm"})    # + 语义审计（烧 token）

未传时取环境变量 `AUDIT_LEVEL`，再缺省为 `rule` —— **默认永远不联网**。

终态
----
`succeeded` / `failed` / `canceled` / **`degraded`**（跑完了但审计不通，结果需要人看）
"""

from __future__ import annotations

import copy
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from .client import HubClient
from .dbmanager import DBClient
from .pipelines import PIPELINES

TERMINAL = ("succeeded", "failed", "canceled", "degraded")
TERMINAL_TASK = ("succeeded", "failed", "canceled")   # 任务终态（扇出等待用）
DEFAULT_AUDIT_LEVEL = "rule"          # ★ 默认最省：纯规则审计，0 网络 0 token


def _audit_level(params: Optional[Dict[str, Any]]) -> str:
    """审计级别：运行参数 > 环境变量 AUDIT_LEVEL > rule。"""
    if params:
        lv = params.get("audit_level")
        if lv:
            return str(lv)
    return os.environ.get("AUDIT_LEVEL") or DEFAULT_AUDIT_LEVEL


class Coordinator:
    def __init__(self, hub_url: str, token: Optional[str] = None,
                 db: Optional[DBClient] = None):
        self.client = HubClient(hub_url, token=token)
        self.db = db
        self._runs: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    # ---------- 对外 ----------
    def start(self, pipeline: Any, params: Optional[Dict[str, Any]] = None,
              run_id: Optional[str] = None) -> Dict[str, Any]:
        spec = PIPELINES.get(pipeline) if isinstance(pipeline, str) else pipeline
        if not spec:
            raise ValueError(f"未知流水线：{pipeline}")
        run_id = run_id or "r_" + uuid.uuid4().hex[:12]
        # ★ 审计级别在这里定下来，传给 auditor 步骤（默认 rule = 不联网不烧 token）
        params = dict(params or {})
        params["audit_level"] = _audit_level(params)
        now = time.time()
        run = {"run_id": run_id, "pipeline": spec["name"], "params": params,
               "state": "running", "steps": {}, "channels": {},
               "created": now, "updated": now,
               "audit": {"level": params["audit_level"], "verdict": None}}
        # foreach 配错要在建 run 之前就拒绝（否则要等执行线程才发现，run 会卡在 running）
        for step in spec["steps"]:
            fo = step.get("foreach")
            if fo is not None:
                if not isinstance(fo, dict) or not fo.get("from"):
                    raise ValueError(f"步骤 {step.get('name')} 的 foreach 必须是 "
                                     '{"from": "..."} 且 from 非空')
                if fo.get("max") is not None and (isinstance(fo["max"], bool)
                                                   or not isinstance(fo["max"], int)):
                    raise ValueError(f"步骤 {step.get('name')} 的 foreach.max 必须是整数")
        for step in spec["steps"]:
            run["steps"][step["name"]] = {"state": "pending"}
        with self._lock:
            self._runs[run_id] = run
        self._persist(run)
        threading.Thread(target=self._execute, args=(run_id, copy.deepcopy(spec)),
                         name=f"run-{run_id}", daemon=True).start()
        return self._snapshot(run_id)

    def status(self, run_id: str) -> Dict[str, Any]:
        with self._lock:
            if run_id in self._runs:
                return copy.deepcopy(self._runs[run_id])
        if self.db:
            rec = self.db.read("pipeline_runs", run_id)
            if rec.get("found"):
                return rec["value"]
        raise KeyError(run_id)

    def list_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._lock:
            runs = sorted(self._runs.values(), key=lambda r: r["created"], reverse=True)
        return copy.deepcopy(runs[:limit])

    def wait(self, run_id: str, timeout: float = 300) -> Dict[str, Any]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            snap = self.status(run_id)
            if snap["state"] in TERMINAL:
                return snap
            time.sleep(0.2)
        return self.status(run_id)

    # ---------- 执行 ----------
    def _execute(self, run_id: str, spec: Dict[str, Any],
                 resume: bool = False) -> None:
        steps: List[Dict[str, Any]] = spec["steps"]
        by_name = {s["name"]: s for s in steps}
        with self._lock:
            run = self._runs[run_id]
            # 每次 resume 递增：重跑步骤的幂等键要带后缀，否则命中的是上一次的旧任务
            # （create 命中幂等键直接返回旧回执），等于"没重跑"。
            attempt = int(run.get("resume_count") or 0)
            ctx = {"params": dict(run["params"]),
                   "steps": {n: {"state": s.get("state"), "result": s.get("result")}
                             for n, s in run["steps"].items()
                             if s.get("state") in ("succeeded", "skipped")},
                   "channels": dict(run.get("channels") or {})}
        # 首次运行仍用旧键 `run_id:step`；resume 后换 `run_id:step:r{n}`
        suffix = "" if attempt <= 0 else f":r{attempt}"
        pending = [s["name"] for s in steps]
        if resume:
            # 断点续跑：已成功/已跳过的步骤不重做；failed / running / pending 重新排队
            pending = [n for n in pending
                       if (run["steps"].get(n) or {}).get("state")
                       not in ("succeeded", "skipped")]
            for n in pending:
                self._set_step(run_id, n, {"state": "pending", "resumed": True,
                                           "error": None, "result": None})
        failed = False

        while pending:
            progressed = False
            for name in list(pending):
                step = by_name[name]
                deps = step.get("depends_on") or []
                dep_states = [self._step_state(run_id, d) for d in deps]
                if any(s == "failed" for s in dep_states):
                    self._skip_step(run_id, name, "upstream_failed", "上游失败")
                    pending.remove(name)
                    progressed = True
                    continue
                if any(s == "skipped" for s in dep_states) and not step.get("run_on_skipped"):
                    self._skip_step(run_id, name, "upstream_skipped", "上游被跳过")
                    pending.remove(name)
                    progressed = True
                    continue
                if not all(s in ("succeeded", "skipped") for s in dep_states):
                    continue

                if not self._when_ok(run_id, step.get("when")):
                    self._set_step(run_id, name, {"state": "skipped",
                                                  "reason": "when 条件不满足"})
                    pending.remove(name)
                    progressed = True
                    continue

                self._set_step(run_id, name, {"state": "running", "started": time.time()})
                if step.get("foreach"):
                    try:
                        ok, result, detail = self._execute_foreach(
                            run_id, name, step, ctx, suffix)
                    except Exception as e:  # noqa: BLE001 - 配置错误不能把线程打死
                        # 以前这里没包 try：foreach 配错（非 list / 超 max）直接抛，
                        # 线程退出后 run 永远停在 running。现在明确失败。
                        self._set_step(run_id, name, {
                            "state": "failed",
                            "error": {"code": "fanout_error", "message": str(e)},
                            "ended": time.time()})
                        ctx["steps"][name] = {"state": "failed", "result": None}
                        failed = True
                        pending.remove(name)
                        progressed = True
                        continue
                    if ok:
                        self._set_step(run_id, name, {
                            "state": "succeeded", "result": result,
                            "items": detail, "ended": time.time()})
                        ctx["steps"][name] = {"state": "succeeded", "result": result}
                    else:
                        self._set_step(run_id, name, {
                            "state": "failed", "items": detail,
                            "error": {"code": "fanout_failed",
                                      "message": "至少一个分片失败"},
                            "ended": time.time()})
                        ctx["steps"][name] = {"state": "failed", "result": None}
                        failed = True
                    try:
                        self._emit(run_id, step, result if ok else None, ctx)
                    except Exception as e:  # noqa: BLE001 - merge 配错要明确失败
                        self._set_step(run_id, name, {
                            "state": "failed",
                            "error": {"code": "emit_error", "message": str(e)}})
                        failed = True
                    pending.remove(name)
                    progressed = True
                    continue

                try:
                    params = _resolve(step.get("params") or {}, ctx)
                    task = self.client.submit(
                        step["capability"], step["action"], params,
                        timeout_s=float(step.get("timeout_s") or 300),
                        wait=True, max_retries=int(step.get("max_retries") or 0),
                        idempotency_key=f"{run_id}:{name}{suffix}",
                    )
                except Exception as e:  # noqa: BLE001
                    task = {"state": "failed",
                            "error": {"code": "submit_error", "message": str(e)}}

                if task.get("state") == "succeeded":
                    result = task.get("result")
                    try:
                        self._emit(run_id, step, result, ctx)      # 先合并通道，再放行下游
                    except Exception as e:  # noqa: BLE001
                        self._set_step(run_id, name, {
                            "state": "failed",
                            "error": {"code": "emit_error", "message": str(e)}})
                        ctx["steps"][name] = {"state": "failed", "result": None}
                        pending.remove(name)
                        progressed = True
                        failed = True
                        continue
                    self._set_step(run_id, name, {"state": "succeeded", "result": result,
                                                  "task_id": task.get("id"),
                                                  "ended": time.time()})
                    ctx["steps"][name] = {"state": "succeeded", "result": result}
                    pending.remove(name)
                    progressed = True
                else:
                    self._set_step(run_id, name, {
                        "state": "failed", "task_id": task.get("id"),
                        "error": task.get("error") or {"code": "unknown", "message": "失败"},
                        "ended": time.time()})
                    ctx["steps"][name] = {"state": "failed", "result": None}
                    pending.remove(name)
                    progressed = True
                    failed = True

            if not progressed:
                for name in pending:
                    self._set_step(run_id, name, {"state": "skipped",
                                                  "error": {"code": "deadlock",
                                                            "message": "依赖无法满足"}})
                pending.clear()
                failed = True

        self._finish(run_id, "failed" if failed else "succeeded")

    # ---------- 断点续跑（对应 LangGraph 的 checkpointer） ----------
    def resume(self, run_id: str, *, force: bool = False) -> Dict[str, Any]:
        """从落库的运行状态续跑：已成功的步骤不重做，只补未完成的。

        `running` 只在 `force=True` 时允许（进程崩溃遗留的 run；正常在跑的直接拒绝，
        防双跑）。每次 resume 递增 `resume_count`，重跑步骤用新的幂等键
        （`run_id:step:r{n}`）——否则会命中旧任务、拿回旧的失败回执，等于没重跑。
        """
        with self._lock:
            run = self._runs.get(run_id)
            if run is None and self.db:
                rec = self.db.read("pipeline_runs", run_id)
                if rec.get("found"):
                    run = copy.deepcopy(rec["value"])
                    self._runs[run_id] = run
            if run is None:
                raise KeyError(f"没有这个 run：{run_id}")
            state = run.get("state")
            if state not in TERMINAL + ("paused",) and not (state == "running" and force):
                raise ValueError(f"run {run_id} 还在 {state}，不能 resume"
                                 "（确认进程已退出后传 force=True）")
            spec = PIPELINES.get(run.get("pipeline") or "")
            if not spec:
                raise ValueError(f"流水线已不存在：{run.get('pipeline')}")
            run["resume_count"] = int(run.get("resume_count") or 0) + 1
            run["state"] = "running"
            run["updated"] = time.time()
            snap = copy.deepcopy(run)
        self._persist(snap)
        threading.Thread(target=self._execute,
                         args=(run_id, copy.deepcopy(spec)),
                         kwargs={"resume": True},
                         name=f"run-{run_id}-resume", daemon=True).start()
        return self._snapshot(run_id)

    # ---------- 通道（对应 LangGraph 的 state channel + reducer） ----------
    def _emit(self, run_id: str, step: Dict[str, Any], result: Any,
              ctx: Dict[str, Any]) -> None:
        """把步骤结果按 `emit.merge` 合并进共享通道，下游用 `${channels.xxx}` 取。

        `emit` 可写字符串（通道名）或对象：
        `{"channel": "news", "path": "events", "merge": "append"}`
        merge：`replace`（默认）/ `append`（列表追加）/ `sum`（数值累加）/ `merge`（dict 合并）。
        """
        spec = step.get("emit")
        if not spec or result is None:
            return
        if isinstance(spec, str):
            spec = {"channel": spec}
        channel = (spec.get("channel") or "").strip()
        if not channel:
            raise ValueError(f"步骤 {step['name']} 的 emit 缺少 channel")
        merge = str(spec.get("merge") or "replace").lower()
        value = result
        if spec.get("path"):
            value = _lookup({"result": result}, f"result.{spec['path']}")
        with self._lock:
            run = self._runs[run_id]
            channels = run.setdefault("channels", {})
            channels[channel] = _merge_value(channels.get(channel), value, merge)
            ctx["channels"] = dict(channels)
            snap = copy.deepcopy(run)
        self._persist(snap)

    # ---------- 动态扇出（对应 LangGraph 的 Send） ----------
    def _execute_foreach(self, run_id: str, name: str, step: Dict[str, Any],
                         ctx: Dict[str, Any], suffix: str = ""
                         ) -> tuple[bool, Any, List[Dict[str, Any]]]:
        """按列表把一步展开成 N 个并行任务，全部结束后按序收结果。

        `foreach: {"from": "${params.commodities}", "as": "item", "max": 20}`
        —— 列表里每项注入 ctx 的同名变量（默认 `item`），params 照常写 `${item}`。
        `suffix` 是 resume 的重跑后缀（首次为空），保证续跑不会命中旧任务。
        """
        spec = step.get("foreach") or {}
        items = _resolve(spec.get("from"), ctx)
        if isinstance(items, str):
            items = [s for s in items.split(",") if s.strip()]
        if not isinstance(items, list):
            raise ValueError(f"步骤 {name} 的 foreach.from 必须解析成列表（当前 {type(items).__name__}）")
        limit = int(spec.get("max") or 20)
        if len(items) > limit:
            raise ValueError(f"步骤 {name} 的扇出 {len(items)} 超过上限 {limit}")
        if not items:
            return True, [], []          # 空列表：立即完成，不空等 timeout
        as_name = spec.get("as") or "item"

        ids: List[str] = []
        for idx, item in enumerate(items):
            sub_ctx = dict(ctx)
            sub_ctx[as_name] = item
            sub_ctx["index"] = idx
            params = _resolve(step.get("params") or {}, sub_ctx)
            try:
                task = self.client.submit(
                    step["capability"], step["action"], params,
                    timeout_s=float(step.get("timeout_s") or 300), wait=False,
                    max_retries=int(step.get("max_retries") or 0),
                    idempotency_key=f"{run_id}:{name}:{idx}{suffix}")
                ids.append(task.get("id"))
            except Exception as e:  # noqa: BLE001
                return False, None, [{"index": idx, "state": "failed",
                                      "error": {"code": "submit_error", "message": str(e)}}]

        timeout = float(step.get("timeout_s") or 300) + 30
        deadline = time.time() + timeout
        tasks: Dict[str, Dict[str, Any]] = {}
        while time.time() < deadline:
            try:
                tasks = {tid: self.client.task(tid) for tid in ids if tid}
            except Exception:  # noqa: BLE001 - 轮询期间网络抖动继续等
                tasks = {}
            if tasks and all(t.get("state") in TERMINAL_TASK for t in tasks.values()):
                break
            time.sleep(0.2)

        detail: List[Dict[str, Any]] = []
        results: List[Any] = []
        ok_all = True
        for idx, tid in enumerate(ids):
            task = tasks.get(tid) or {}
            state = task.get("state") or "timeout"
            if state != "succeeded":
                ok_all = False
            detail.append({"index": idx, "task_id": tid, "state": state,
                           "result": task.get("result")})
            if state == "succeeded":
                results.append(task.get("result"))
        return ok_all, results, detail

    def _when_ok(self, run_id: str, when: Optional[Dict[str, Any]]) -> bool:
        if not when:
            return True
        with self._lock:
            step = self._runs[run_id]["steps"].get(when.get("step", ""), {})
        value = _lookup(step, when.get("path", ""))
        op = when.get("op", "truthy")
        target = when.get("value")
        try:
            if op == "gt":
                return value is not None and value > target
            if op == "gte":
                return value is not None and value >= target
            if op == "lt":
                return value is not None and value < target
            if op == "lte":
                return value is not None and value <= target
            if op == "eq":
                return value == target
            if op == "ne":
                return value != target
            if op == "exists":
                return value is not None
            return bool(value)
        except TypeError:
            return False

    # ---------- 状态 ----------
    def _set_step(self, run_id: str, name: str, fields: Dict[str, Any]) -> None:
        with self._lock:
            run = self._runs[run_id]
            run["steps"].setdefault(name, {}).update(fields)
            run["updated"] = time.time()
            snap = copy.deepcopy(run)
        self._persist(snap)

    def _step_state(self, run_id: str, name: str) -> str:
        with self._lock:
            return self._runs[run_id]["steps"].get(name, {}).get("state", "pending")

    def _skip_step(self, run_id: str, name: str, code: str, message: str) -> None:
        self._set_step(run_id, name, {"state": "skipped",
                                      "error": {"code": code, "message": message}})

    def _finish(self, run_id: str, state: str) -> None:
        """收尾：先把审计结论摘出来，再定终态。

        规则：流程本身失败 → `failed`；流程跑通但审计判 fail/suspect → `degraded`
        （**结果可看但不可直接用**，这正是审计官存在的意义）；其余 → `succeeded`。
        """
        with self._lock:
            run = self._runs[run_id]
            audit_step = run["steps"].get("audit") or {}
            audit_result = audit_step.get("result") or {}
            verdict = audit_result.get("verdict")
            run["audit"] = {
                "level": (run.get("params") or {}).get("audit_level"),
                "verdict": verdict,
                "severity": audit_result.get("severity"),
                "counts": audit_result.get("counts"),
                "findings": (audit_result.get("findings") or [])[:20],
                "step_state": audit_step.get("state"),
            }
            final_state = state
            if state == "succeeded" and verdict in ("fail", "suspect"):
                final_state = "degraded"
            run["state"] = final_state
            run["updated"] = time.time()
            snap = copy.deepcopy(run)
        self._persist(snap)

    def _persist(self, run: Dict[str, Any]) -> None:
        if not self.db:
            return
        try:
            self.db.write("pipeline_runs", run["run_id"], run)
            self.db.append("pipeline_events", {
                "run_id": run["run_id"], "state": run["state"],
                "pipeline": run["pipeline"], "ts": run["updated"],
            })
        except Exception:  # noqa: BLE001
            pass  # DB 不可用时不影响编排，状态仍在内存

    def _snapshot(self, run_id: str) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._runs[run_id])


class CoordinatorAgent:
    """把 Coordinator 挂成一个 Agent（capability=coordinator）。"""

    def __init__(self, hub_url: str, token: Optional[str] = None,
                 db: Optional[DBClient] = None):
        self.coord = Coordinator(hub_url, token=token, db=db)

    def attach(self, runtime) -> None:
        runtime.add_handler("coordinator", "start", self._start)
        runtime.add_handler("coordinator", "status", self._status)
        runtime.add_handler("coordinator", "runs", self._runs)
        runtime.add_handler("coordinator", "resume", self._resume)
        runtime.add_handler("coordinator", "notify", self._notify)

    def _start(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        run = self.coord.start(params["pipeline"], params.get("params") or {})
        return {"run_id": run["run_id"], "state": run["state"]}

    def _status(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        return self.coord.status(params["run_id"])

    def _runs(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
        return self.coord.list_runs(limit=int(params.get("limit") or 20))

    def _notify(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        """Agent 主动上报（可选）：写进 pipeline_events，不阻塞主链路。"""
        if self.coord.db:
            self.coord.db.append("pipeline_events", {
                "run_id": params.get("run_id"), "step": params.get("step"),
                "status": params.get("status"), "payload": params.get("payload"),
                "ts": time.time(), "source": ctx.get("agent_id"),
            })
        return {"ok": True}

    def _resume(self, params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        """断点续跑：给 run_id，已成功的步骤不重做。"""
        run = self.coord.resume(params["run_id"])
        return {"run_id": run["run_id"], "state": run["state"]}


def _lookup(root: Any, path: str) -> Any:
    cur = root
    for part in [p for p in path.split(".") if p]:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if 0 <= idx < len(cur) else None
        else:
            return None
    return cur


_VAR = re.compile(r"\$\{([^{}]+)\}")


def _as_text(v: Any) -> str:
    """嵌入字符串时把取值转成文本；取不到按空串（不写 "None"）。"""
    return "" if v is None else str(v)


def _resolve(value: Any, ctx: Dict[str, Any]) -> Any:
    """代入 `${...}`。

    两种形态：
    · 整串就是一个变量（`"${steps.build.result}"`）→ 直接返回原值，类型不丢；
    · 变量嵌在字符串里（`"${params.commodity}:latest"`，或
      `"${steps.build.result.results.${params.commodity}.balance}"`）→ 由内向外逐层展开，
      展开后若整串恰好是变量，仍返回原值。

    最多展开 10 层，防自引用死循环；嵌入变量取不到值时按空串处理。
    """
    if isinstance(value, str) and "${" in value:
        text = value
        for _ in range(10):
            if text.startswith("${") and text.endswith("}") and text.count("${") == 1:
                return _lookup(ctx, text[2:-1])
            new = _VAR.sub(lambda m: _as_text(_lookup(ctx, m.group(1))), text)
            if new == text:
                return text
            text = new
        return text
    if isinstance(value, dict):
        return {k: _resolve(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, ctx) for v in value]
    return value


def _merge_value(current: Any, incoming: Any, how: str) -> Any:
    """通道合并（对应 LangGraph 的 reducer）：replace / append / sum / merge。

    并行分支写同一通道时靠它合并，而不是各自约定 key 再让下游猜。
    配错就报错（类型不符不静默放过），步骤会以 `emit_error` 失败。
    """
    how = (how or "replace").lower()
    if how == "replace":
        return incoming
    if how == "append":
        base = current if isinstance(current, list) else ([] if current is None else [current])
        add = incoming if isinstance(incoming, list) else [incoming]
        return base + add
    if how == "sum":
        if incoming is None:
            return current
        if isinstance(incoming, bool) or not isinstance(incoming, (int, float)):
            raise ValueError(f"emit.merge=sum 只接受数值（当前 {type(incoming).__name__}）")
        if current is None:
            return incoming
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            raise ValueError(f"emit.merge=sum 的通道已有非数值（当前 {type(current).__name__}）")
        return current + incoming
    if how == "merge":
        if not isinstance(incoming, dict):
            raise ValueError(f"emit.merge=merge 只接受对象（当前 {type(incoming).__name__}）")
        base = dict(current) if isinstance(current, dict) else {}
        base.update(incoming)
        return base
    raise ValueError(f"emit.merge 不支持 {how!r}（replace/append/sum/merge）")