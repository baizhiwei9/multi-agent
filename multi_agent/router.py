"""能力路由：协调者按「谁声明了这个能力」来派活，而不是靠猜或写死。

要解决的问题
============
你说的是这个意思：

    某个 agent 声明"我支持债券审计"→ 这个能力注册到 Hub →
    协调者按能力往下分活，不用预先知道是哪台机器

Hub 已经在注册表里存了每个 agent 的能力清单（`roles[].actions`），
`registry.find(capability, action, labels=...)` 也能按能力+标签过滤。
缺的是**协调者侧的决策层**：

  1. **能力清单聚合**：从注册表反推"现在全网有哪些能力、各由谁提供、
     有几台在线、剩余 TTL 多少"
  2. **路由决策**：给定一个能力，按策略选一台（轮询 / 最少负载 / 固定偏好 /
     就近标签），并给出**可解释的理由**
  3. **缺口诊断**：要的能力没人提供 → 明确说"缺 X/Y，现有的是 Z"，
     而不是等超时
  4. **批量规划**：一条流水线需要多个能力 → 先算可行性，缺的提前报出来

设计约束
========
- **不猜**：能力必须由 agent 显式声明；没声明就是不支持，不会"试试看"
- **可解释**：每次路由返回 `reason` 和候选清单（排障时能看懂为什么选它）
- **可降级**：目标机器不在了 → 自动 fallback 到其他声明了该能力的 agent
  （除非调用方要求"必须这台"）
- **不改 Hub**：全部建立在现有 `hub.agents` + `labels` 定向投递之上

用法
====
    from multi_agent.router import CapabilityRouter

    r = CapabilityRouter(client)                 # client 可以是 HubClient/TCPClient

    r.catalog()                                  # 全网能力清单
    r.route("auditor", "balance")                # 选一台，带理由
    r.route_plan([("A","build_sheet"), ("auditor","balance")])   # 批量可行性
    r.submit("auditor", "balance", {"balance": bal}, project_hint="bond")
    r.gaps(["bond:audit"])                       # 缺什么
"""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

ROUTE_STRATEGIES = ("round_robin", "least_loaded", "prefer_label", "first")


class NoCapability(RuntimeError):
    """没有任何存活 agent 提供这个能力。"""


class CapabilityRouter:
    """按能力选 agent，并给可解释的理由。

    参数
    ----
    client: HubClient（HTTP）或 TCPClient（TCP），只需有 `_request` 或 `call`
      · 为了让两种线缆都能用，内部有两个小适配方法 `_agents_raw()`
    strategy: 默认 "round_robin"
      · round_robin   —— 轮流，避免总压在第一个实例上
      · least_loaded  —— 按当前已派发但未完成的任务数最少
      · prefer_label  —— 优先匹配 prefer 里的 labels，其余按 round_robin
      · first         —— 永远第一个（调试用）
    """

    def __init__(self, client: Any, strategy: str = "round_robin"):
        self.client = client
        self.strategy = strategy if strategy in ROUTE_STRATEGIES else "round_robin"
        self._rr: Dict[Tuple[str, str], int] = {}
        self._lock = threading.RLock()

    # ---------- 线缆适配 ----------
    def _agents_raw(self) -> List[Dict[str, Any]]:
        c = self.client
        if hasattr(c, "call"):                      # TCPClient
            return (c.call("hub.agents") or {}).get("agents") or []
        if hasattr(c, "_request"):                  # HubClient
            return c._request("GET", "/v1/agents").get("agents") or []
        raise TypeError("client 必须是 HubClient 或 TCPClient")

    @staticmethod
    def _caps(agent: Dict[str, Any]) -> List[Tuple[str, str]]:
        out: List[Tuple[str, str]] = []
        for role in agent.get("roles") or []:
            name = role.get("name")
            for act in role.get("actions") or []:
                if name and act:
                    out.append((name, act))
        return out

    @staticmethod
    def _supports(agent: Dict[str, Any], capability: str, action: str) -> bool:
        caps = CapabilityRouter._caps(agent)
        return (capability, action) in caps or (capability, "*") in caps

    # ---------- 能力清单 ----------
    def catalog(self) -> Dict[str, Any]:
        """全网能力清单：每个 (capability/action) 有哪些 agent 提供。"""
        agents = self._agents_raw()
        now = time.time()
        table: Dict[str, List[Dict[str, Any]]] = {}
        for a in agents:
            alive = a.get("alive", True)
            for cap, act in self._caps(a):
                key = f"{cap}/{act}"
                table.setdefault(key, []).append({
                    "agent_id": a.get("agent_id"),
                    "alive": alive,
                    "age_s": a.get("age_s"),
                    "ttl_s": a.get("ttl_s"),
                    "labels": a.get("labels") or {},
                })
        for key in table:
            table[key].sort(key=lambda x: (not x["alive"], x["agent_id"] or ""))
        return {
            "capabilities": table,
            "agent_count": len(agents),
            "alive_count": sum(1 for a in agents if a.get("alive", True)),
            "ts": now,
        }

    def providers(self, capability: str, action: str) -> List[Dict[str, Any]]:
        """某个能力的存活提供者（按 round-robin 顺序）。"""
        return [a for a in self._agents_raw()
                if a.get("alive", True) and self._supports(a, capability, action)]

    # ---------- 路由 ----------
    def route(self, capability: str, action: str,
              prefer_labels: Optional[Dict[str, Any]] = None,
              require_agent: Optional[str] = None,
              strategy: Optional[str] = None) -> Dict[str, Any]:
        """选一台 agent，返回 {agent_id, reason, candidates}。

        require_agent 给定时：只有它；它不在/不支持 → 直接报错（不 fallback，
        因为这是调用方明确点名，静默换人会违背意图）。
        """
        st = strategy or self.strategy
        cands = self.providers(capability, action)
        all_agents = {a.get("agent_id"): a for a in self._agents_raw()}

        if require_agent:
            target = all_agents.get(require_agent)
            if target is None:
                raise NoCapability(
                    f"点名的 agent {require_agent} 未注册；"
                    f"当前可用：{sorted(all_agents)}")
            if not target.get("alive", True):
                raise NoCapability(f"点名的 agent {require_agent} 已失联")
            if not self._supports(target, capability, action):
                raise NoCapability(
                    f"agent {require_agent} 没有声明 {capability}/{action}；"
                    f"它声明的是 {sorted(self._caps(target))}")
            return {"agent_id": require_agent, "reason": "调用方点名",
                    "candidates": [require_agent], "strategy": "explicit"}

        if not cands:
            live = {k: v for k, v in all_agents.items() if v.get("alive", True)}
            raise NoCapability(
                f"没有存活 agent 提供 {capability}/{action}；"
                f"在线 agent：{ {k: sorted(self._caps(v)) for k, v in live.items()} }")

        ids = [a.get("agent_id") for a in cands]

        # ★ 只要调用方给了 prefer_labels，就**先试标签匹配**（不管默认策略是什么）。
        #   否则传了标签却被静默忽略，是很隐蔽的坑。
        if prefer_labels:
            matched = [a for a in cands
                       if all((a.get("labels") or {}).get(k) == v
                              for k, v in prefer_labels.items())]
            if matched:
                mids = [a.get("agent_id") for a in matched]
                return {"agent_id": mids[0],
                        "reason": f"标签匹配 {prefer_labels}",
                        "candidates": mids, "strategy": "prefer_label"}
            # 没匹配上就退化，但理由必须说清楚（不能静默）
            fallback_note = f"标签 {prefer_labels} 无匹配，退回轮询"
            st = "round_robin" if st == "prefer_label" else st
        else:
            fallback_note = ""

        if st == "prefer_label":
            st = "round_robin"      # 没给标签的 prefer_label 等价于轮询

        if st == "first":
            return {"agent_id": ids[0], "reason": "first 策略（调试用）",
                    "candidates": ids, "strategy": st}

        if st == "least_loaded":
            loads = self._loads()
            pick = min(ids, key=lambda i: (loads.get(i, 0), i))
            return {"agent_id": pick,
                    "reason": f"负载最低（{pick}: {loads.get(pick, 0)} 个在跑）",
                    "candidates": ids, "strategy": st,
                    "loads": {i: loads.get(i, 0) for i in ids}}

        # round_robin
        key = (capability, action)
        with self._lock:
            idx = self._rr.get(key, 0) % len(ids)
            self._rr[key] = idx + 1
        reason = f"轮询第 {idx + 1}/{len(ids)} 个"
        if fallback_note:
            reason = fallback_note + "；" + reason
        return {"agent_id": ids[idx], "reason": reason,
                "candidates": ids, "strategy": "round_robin"}

    def _loads(self) -> Dict[str, int]:
        """每个 agent 当前 assigned 的任务数（Hub 不可用时返回空）。"""
        try:
            c = self.client
            if hasattr(c, "call"):
                tasks = (c.call("hub.tasks", {"limit": 200}) or {}).get("tasks") or []
            else:
                tasks = (c._request("GET", "/v1/tasks?limit=200") or {}).get("tasks") or []
        except Exception:  # noqa: BLE001
            return {}
        out: Dict[str, int] = {}
        for t in tasks:
            if t.get("state") == "assigned" and t.get("agent_id"):
                out[t["agent_id"]] = out.get(t["agent_id"], 0) + 1
        return out

    # ---------- 批量可行性 ----------
    def route_plan(self, needs: Sequence[Tuple[str, str]],
                   prefer_labels: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """给一条流水线算可行性：每个能力能不能派、派给谁、缺哪些。

        返回 ok=False 时 `missing` 里是缺的能力 —— 提前报出来，不用等跑挂。
        """
        steps: List[Dict[str, Any]] = []
        missing: List[str] = []
        for cap, act in needs:
            try:
                picked = self.route(cap, act, prefer_labels=prefer_labels)
                steps.append({"capability": cap, "action": act,
                              "agent_id": picked["agent_id"],
                              "reason": picked["reason"]})
            except NoCapability as e:
                missing.append(f"{cap}/{act}")
                steps.append({"capability": cap, "action": act,
                              "agent_id": None, "error": str(e)})
        return {"ok": not missing, "steps": steps, "missing": missing,
                "hint": (f"缺能力：{missing}" if missing
                         else "全部能力都有存活 agent 提供")}

    def gaps(self, needs: Sequence[str]) -> Dict[str, Any]:
        """批量查缺口。

        支持两种写法：
          · `"auditor:bond/balance"`  —— 以**最后一个** `/` 切分（角色名可含冒号）
          · `"auditor:bond:balance"`  —— 以**最后一个** `:` 切分（兼容旧写法）
        优先用 `/`，因为角色名里已经带冒号（`auditor:bond`）。
        """
        cat = self.catalog()["capabilities"]
        out = []
        for need in needs:
            key = self._norm_cap(need)
            alive = [p for p in cat.get(key, []) if p.get("alive")]
            out.append({"need": need, "normalized": key,
                        "available": bool(alive),
                        "providers": [p["agent_id"] for p in alive]})
        missing = [x["need"] for x in out if not x["available"]]
        return {"checks": out, "missing": missing, "ok": not missing,
                "hint": ("用 'role/action' 写法，role 可含冒号（如 auditor:bond/balance）"
                         if missing else "全部能力都有存活 agent")}

    @staticmethod
    def _norm_cap(need: str) -> str:
        s = (need or "").strip()
        if not s:
            return s
        if "/" in s:
            role, _, action = s.rpartition("/")
            return f"{role}/{action}"
        if ":" in s:
            role, _, action = s.rpartition(":")
            return f"{role}/{action}"
        return s

    # ---------- 派活 ----------
    def submit(self, capability: str, action: str,
               params: Optional[Dict[str, Any]] = None,
               prefer_labels: Optional[Dict[str, Any]] = None,
               require_agent: Optional[str] = None,
               strategy: Optional[str] = None,
               wait: bool = True, timeout_s: float = 300) -> Dict[str, Any]:
        """路由 + 派活一步到位。返回里带 `routed_to` 和 `reason` 便于审计。"""
        picked = self.route(capability, action, prefer_labels=prefer_labels,
                            require_agent=require_agent, strategy=strategy)
        agent_id = picked["agent_id"]
        params = dict(params or {})
        c = self.client
        if hasattr(c, "call"):
            task = c.call("hub.submit", {
                "capability": capability, "action": action, "params": params,
                "timeout_s": timeout_s, "wait": False,
                "labels": {"agent_id": agent_id}})
            get = lambda tid: c.call("hub.task", {"task_id": tid})  # noqa: E731
        else:
            task = c.submit(capability, action, params, timeout_s=timeout_s,
                            wait=False, labels={"agent_id": agent_id})
            get = c.task  # type: ignore[assignment]

        if wait:
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                snap = get(task["id"])
                if snap.get("state") in ("succeeded", "failed", "timeout", "canceled"):
                    snap["routed_to"] = agent_id
                    snap["reason"] = picked["reason"]
                    snap["candidates"] = picked["candidates"]
                    return snap
                time.sleep(0.2)
            snap = get(task["id"])
            snap["routed_to"] = agent_id
            snap["reason"] = "超时未返回"
            return snap

        task["routed_to"] = agent_id
        task["reason"] = picked["reason"]
        return task

    def submit_with_fallback(self, capability: str, action: str,
                             params: Optional[Dict[str, Any]] = None,
                             max_attempts: int = 3,
                             **kwargs: Any) -> Dict[str, Any]:
        """派活失败自动换下一台（同一能力多实例时有用）。

        只有"任务失败/超时"才换人；"能力不存在"直接抛错（换人也没用）。
        """
        tried: List[str] = []
        last: Optional[Dict[str, Any]] = None
        for _ in range(max(1, max_attempts)):
            cands = [a["agent_id"] for a in self.providers(capability, action)
                     if a["agent_id"] not in tried]
            if not cands:
                break
            agent_id = cands[0]
            tried.append(agent_id)
            snap = self.submit(capability, action, params,
                               require_agent=agent_id, **kwargs)
            snap["attempts"] = list(tried)
            if snap.get("state") == "succeeded":
                return snap
            last = snap
        if last is None:
            raise NoCapability(
                f"{capability}/{action} 没有可用 agent（已尝试 {tried}）")
        return last
