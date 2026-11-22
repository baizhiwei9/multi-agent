"""Coordinator 的远程派活能力：把一个任务派给**指定 agent**，而不是"谁能干就谁干"。

为什么需要
==========
Hub 默认按 `capability + action` 选人（round-robin），这在"同一个能力有多个实例"
时是对的。但真实调度里经常要**点名**：

  · "这条流水线跑在 hermes 那台机上，别在 cohere 上跑"
  · "A 角色绑 openclaw，B 角色绑 hermes"（角色与机器的对应关系是固定的）
  · "先让 A 算，再让**刚才那个** agent 继续算下一步"（保持数据本地）

为此在 Coordinator 上加两个能力：

1. `submit_to(agent_id, capability, action, params)` —— 点名派活
   （通过 Hub 的 `labels` 过滤 + 目标校验实现，不改 Hub 协议）
2. `assign_role(agent_id, roles)` —— 让远端 agent 在运行时挂/卸角色
   （调该 agent 的 `runner/assign` 动作；agent 侧需挂 runner）

角色 ↔ agent 绑定表
===================
`RoleBinding` 把"谁演哪个角色"显式记下来，避免同一角色双写：

    bind = RoleBinding({"A": "openclaw-1", "B": "hermes-1", "Q": "coze-1"})
    bind.agent_for("A")            # → "openclaw-1"
    bind.bind("A", "openclaw-2")   # → 拒绝（A 已被占）
    bind.bind("A", "openclaw-2", force=True)   # 显式抢占，返回旧绑定

BalanceAI 的 `balance/`、`verdicts/`、`agents/state/` 都是按角色命名的目录，
同一角色两个 agent 会互相覆盖（docs/AGENTS.md §4.3），所以这个表是**必需**的，
不是锦上添花。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional, Sequence

from .client import HubClient, HubError
from .coordinator import Coordinator
from .dbmanager import DBClient


class BindingConflict(RuntimeError):
    """同一角色已经绑了别的 agent（默认拒绝，需显式 force）。"""


class RoleBinding:
    """角色 → agent 的绑定表（线程安全，可落 DBManager）。"""

    def __init__(self, initial: Optional[Dict[str, str]] = None):
        self._map: Dict[str, str] = dict(initial or {})
        self._lock = threading.RLock()

    def agent_for(self, role: str) -> Optional[str]:
        with self._lock:
            return self._map.get(role)

    def role_of(self, agent_id: str) -> List[str]:
        with self._lock:
            return sorted(r for r, a in self._map.items() if a == agent_id)

    def bind(self, role: str, agent_id: str, force: bool = False) -> Dict[str, Any]:
        if not role or not agent_id:
            raise ValueError("role 和 agent_id 都不能为空")
        with self._lock:
            old = self._map.get(role)
            if old and old != agent_id and not force:
                raise BindingConflict(
                    f"角色 {role} 已绑定 {old}；同一角色双写会互相覆盖"
                    f"（balance/、verdicts/），如确认要换请传 force=True")
            # 同一个 agent 不重复绑同一角色
            self._map[role] = agent_id
            return {"role": role, "agent_id": agent_id,
                    "previous": old if old != agent_id else None,
                    "replaced": bool(old and old != agent_id)}

    def unbind(self, role: str) -> Optional[str]:
        with self._lock:
            return self._map.pop(role, None)

    def snapshot(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._map)

    def check_duplicates(self, agents: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """体检：注册表里同一角色是否被多个存活 agent 同时声明（真实双写风险）。"""
        seen: Dict[str, List[str]] = {}
        for a in agents or []:
            if not a.get("alive", True):
                continue
            for role in a.get("roles") or []:
                name = role.get("name")
                if name:
                    seen.setdefault(name, []).append(a.get("agent_id"))
        return [{"role": r, "agents": ids} for r, ids in seen.items() if len(ids) > 1]


class RemoteCoordinator(Coordinator):
    """在 Coordinator 基础上加"点名派活 + 角色分配"。

    支持两种线缆（**业务代码不用改**）：

        # HTTP
        rc = RemoteCoordinator("http://hub:8787", token="***")
        # TCP（内网/同机房，少一层 HTTP 解析）
        from multi_agent.tcp import TCPClient
        rc = RemoteCoordinator.from_tcp("hub", 8790, token="***")
    """

    def __init__(self, hub_url: str, token: Optional[str] = None,
                 db: Optional[DBClient] = None,
                 binding: Optional[RoleBinding] = None,
                 tcp_client: Any = None):
        super().__init__(hub_url, token=token, db=db)
        self.binding = binding or RoleBinding()
        self._tcp = tcp_client

    @classmethod
    def from_tcp(cls, host: str, port: int = 8790, token: Optional[str] = None,
                 binding: Optional[RoleBinding] = None) -> "RemoteCoordinator":
        """用 TCP 线缆连 Hub（需要 Hub 侧同时开了 TCPHubServer 且共享同一 Hub）。"""
        from .tcp import TCPClient
        rc = cls(f"http://{host}:{port}", token=token, binding=binding,
                 tcp_client=TCPClient(host, port, token=token))
        return rc

    # ---------- 线缆无关的两件事 ----------
    def _agents(self) -> List[Dict[str, Any]]:
        if self._tcp is not None:
            return (self._tcp.call("hub.agents") or {}).get("agents") or []
        return self.client._request("GET", "/v1/agents").get("agents") or []

    def _submit_raw(self, capability: str, action: str, params: Dict[str, Any],
                    timeout_s: float, labels: Dict[str, Any]) -> Dict[str, Any]:
        if self._tcp is not None:
            return self._tcp.call("hub.submit", {
                "capability": capability, "action": action, "params": params,
                "timeout_s": timeout_s, "wait": False, "labels": labels})
        return self.client.submit(capability, action, params, timeout_s=timeout_s,
                                  wait=False, labels=labels)

    def _task(self, task_id: str) -> Dict[str, Any]:
        if self._tcp is not None:
            return self._tcp.call("hub.task", {"task_id": task_id})
        return self.client.task(task_id)

    # ---------- 点名派活 ----------
    def submit_to(self, agent_id: str, capability: str, action: str,
                  params: Optional[Dict[str, Any]] = None, wait: bool = True,
                  timeout_s: float = 300, verify_target: bool = True
                  ) -> Dict[str, Any]:
        """把任务派给指定 agent。用 Hub 的 labels 过滤实现定向投递。

        `verify_target=True` 时会先查注册表确认该 agent 存活且声明了这个能力，
        避免"派出去没人接"（那种情况要等 timeout 才知道，很浪费）。
        """
        if verify_target:
            agents = self._agents()
            target = next((a for a in agents if a.get("agent_id") == agent_id), None)
            if target is None:
                raise HubError(f"agent {agent_id} 未注册（先让它 register）")
            if not target.get("alive", True):
                raise HubError(f"agent {agent_id} 已失联（last_seen 超 TTL）")
            caps = {(r.get("name"), act)
                    for r in (target.get("roles") or [])
                    for act in (r.get("actions") or [])}
            if (capability, action) not in caps and (capability, "*") not in caps:
                raise HubError(
                    f"agent {agent_id} 没有 ({capability}/{action})；"
                    f"它声明的是 {sorted(caps)}")

        task = self._submit_raw(capability, action, params or {}, timeout_s,
                                {"agent_id": agent_id})
        if wait:
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                snap = self._task(task["id"])
                if snap.get("state") in ("succeeded", "failed", "timeout", "canceled"):
                    return snap
                time.sleep(0.2)
            return snap
        return task

    # ---------- 远程角色分配 ----------
    def assign_role(self, agent_id: str, roles: Sequence[Dict[str, Any]],
                    force: bool = False) -> Dict[str, Any]:
        """让远端 agent 运行时挂角色（先写绑定表，再调 runner/assign）。"""
        results = []
        for role in roles:
            name = str(role.get("name") or "").strip()
            if not name:
                continue
            info = self.binding.bind(name, agent_id, force=force)
            results.append(info)
        try:
            remote = self.submit_to(agent_id, "runner", "assign",
                                    {"roles": list(roles)}, wait=True, timeout_s=30)
            ok = remote.get("state") == "succeeded"
        except HubError as e:
            ok = False
            remote = {"state": "failed", "error": str(e)}
        return {"binds": results, "remote": remote, "ok": ok}

    def release_role(self, agent_id: str, roles: Sequence[str]) -> Dict[str, Any]:
        for r in roles:
            self.binding.unbind(r)
        try:
            remote = self.submit_to(agent_id, "runner", "release",
                                    {"roles": list(roles)}, wait=True, timeout_s=30)
        except HubError as e:
            remote = {"state": "failed", "error": str(e)}
        return {"released": list(roles), "remote": remote}

    def run_python(self, agent_id: str, script: str,
                   argv: Optional[Sequence[str]] = None,
                   cwd: Optional[str] = None, timeout_s: float = 300,
                   env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """让指定 agent 跑一个 Python 程序（需该 agent 挂 runner + 白名单允许）。"""
        params = {"script": script, "timeout_s": timeout_s}
        if argv:
            params["argv"] = list(argv)
        if cwd:
            params["cwd"] = cwd
        if env:
            params["env"] = env
        task = self.submit_to(agent_id, "runner", "py", params,
                              wait=True, timeout_s=timeout_s + 30)
        if task.get("state") != "succeeded":
            return {"ok": False, "agent_id": agent_id, "task": task}
        return dict(task.get("result") or {}, agent_id=agent_id,
                    task_id=task.get("id"))

    # ---------- 体检 ----------
    def health(self) -> Dict[str, Any]:
        agents = self._agents()
        dup = self.binding.check_duplicates(agents)
        return {
            "agents": [{"agent_id": a.get("agent_id"), "alive": a.get("alive"),
                        "roles": [r.get("name") for r in (a.get("roles") or [])]}
                       for a in agents],
            "binding": self.binding.snapshot(),
            "duplicate_roles": dup,
            "ok": not dup,
            "hint": ("同一角色被多个存活 agent 声明 → balance/ 等目录会互相覆盖"
                     if dup else "无角色冲突"),
        }


# ───────────────────────────────────────────── 便捷函数
def plan_roles(spec: Dict[str, Sequence[str]]) -> Dict[str, Any]:
    """从 {agent_id: [role...]} 生成可执行的分配计划（不落任何状态）。

    >>> plan_roles({"openclaw-1": ["A", "F", "E"],
    ...             "hermes-1": ["B", "C"],
    ...             "coze-1": ["D", "Q", "J"]})["ok"]
    True
    """
    seen: Dict[str, str] = {}
    dups: List[Dict[str, Any]] = []
    plan: List[Dict[str, Any]] = []
    for agent_id, roles in spec.items():
        for role in roles:
            if role in seen and seen[role] != agent_id:
                dups.append({"role": role, "agents": [seen[role], agent_id]})
            else:
                seen[role] = agent_id
            plan.append({"agent_id": agent_id, "role": role})
    return {"plan": plan, "duplicates": dups, "ok": not dups,
            "hint": "有重复角色时请拆开，否则同一角色双写会互相覆盖" if dups else "分配无冲突"}
