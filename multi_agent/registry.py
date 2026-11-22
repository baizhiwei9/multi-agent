"""Agent 注册表：能力清单（角色 + 动作）、心跳、TTL、路由选择。

每个 Agent 注册时提交自己的"动作表"：
    {"agent_id": "stock-a", "roles": [
        {"name": "macro", "actions": ["regime", "rates"], "description": "宏观"},
        {"name": "micro", "actions": ["dialectic"], "description": "微观辩证"}
    ]}
Hub 只按 `capability(=role.name) + action` 选人，不关心 Agent 内部怎么实现。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

DEFAULT_TTL_S = 90.0
MIN_TTL_S = 5.0
MAX_TTL_S = 600.0


@dataclass
class AgentRecord:
    agent_id: str
    roles: List[Dict[str, Any]] = field(default_factory=list)
    endpoint: str = ""
    labels: Dict[str, Any] = field(default_factory=dict)
    ttl_s: float = DEFAULT_TTL_S
    last_seen: float = field(default_factory=time.time)
    registered_at: float = field(default_factory=time.time)
    meta: Dict[str, Any] = field(default_factory=dict)

    def alive(self, now: Optional[float] = None) -> bool:
        return (now or time.time()) - self.last_seen <= self.ttl_s

    def capabilities(self) -> Set[Tuple[str, str]]:
        out: Set[Tuple[str, str]] = set()
        for role in self.roles:
            for action in role.get("actions", []):
                out.add((role["name"], action))
        return out

    def as_dict(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = now or time.time()
        return {
            "agent_id": self.agent_id,
            "roles": self.roles,
            "endpoint": self.endpoint,
            "labels": self.labels,
            "ttl_s": self.ttl_s,
            "last_seen": self.last_seen,
            "age_s": round(now - self.last_seen, 1),
            "alive": self.alive(now),
            "registered_at": self.registered_at,
            "meta": self.meta,
        }


class Registry:
    def __init__(self, ttl_s: float = DEFAULT_TTL_S, persist_path: Optional[str] = None):
        self._default_ttl = _clamp_ttl(ttl_s)
        self._agents: Dict[str, AgentRecord] = {}
        self._lock = threading.RLock()
        self._rr: Dict[Tuple[str, str], int] = {}
        self._persist = Path(persist_path) if persist_path else None
        if self._persist and self._persist.exists():
            self._load()

    # ---------- 写 ----------
    def register(self, manifest: Dict[str, Any]) -> AgentRecord:
        agent_id = manifest["agent_id"].strip()
        ttl = _clamp_ttl(manifest.get("ttl_s", self._default_ttl))
        with self._lock:
            old = self._agents.get(agent_id)
            rec = AgentRecord(
                agent_id=agent_id,
                roles=manifest["roles"],
                endpoint=str(manifest.get("endpoint") or ""),
                labels=dict(manifest.get("labels") or {}),
                ttl_s=ttl,
                last_seen=time.time(),
                registered_at=old.registered_at if old else time.time(),
                meta=dict(manifest.get("meta") or {}),
            )
            self._agents[agent_id] = rec
            self._save_locked()
            return rec

    def heartbeat(self, agent_id: str) -> bool:
        with self._lock:
            rec = self._agents.get(agent_id)
            if not rec:
                return False
            rec.last_seen = time.time()
            self._save_locked()
            return True

    def deregister(self, agent_id: str) -> bool:
        with self._lock:
            existed = self._agents.pop(agent_id, None) is not None
            if existed:
                self._save_locked()
            return existed

    def sweep(self) -> List[str]:
        """清掉过期 Agent，返回被清掉的 id。"""
        now = time.time()
        with self._lock:
            dead = [aid for aid, rec in self._agents.items() if not rec.alive(now)]
            for aid in dead:
                self._agents.pop(aid, None)
            if dead:
                self._save_locked()
            return dead

    # ---------- 读 ----------
    def get(self, agent_id: str) -> Optional[AgentRecord]:
        with self._lock:
            return self._agents.get(agent_id)

    def list(self, include_expired: bool = False) -> List[AgentRecord]:
        now = time.time()
        with self._lock:
            recs = list(self._agents.values())
        if not include_expired:
            recs = [r for r in recs if r.alive(now)]
        return sorted(recs, key=lambda r: r.agent_id)

    def find(self, capability: str, action: str,
             exclude: Sequence[str] = (), labels: Optional[Dict[str, Any]] = None) -> List[AgentRecord]:
        """返回能执行 capability/action 的存活 Agent（已排除 exclude、匹配 labels）。"""
        now = time.time()
        exclude_set = set(exclude)
        out = []
        with self._lock:
            for rec in self._agents.values():
                if rec.agent_id in exclude_set or not rec.alive(now):
                    continue
                if labels and any(rec.labels.get(k) != v for k, v in labels.items()):
                    continue
                if _supports(rec, capability, action):
                    out.append(rec)
        return sorted(out, key=lambda r: r.agent_id)

    def next(self, capability: str, action: str,
             exclude: Sequence[str] = (), labels: Optional[Dict[str, Any]] = None) -> Optional[AgentRecord]:
        """按 round-robin 选一个 Agent，避免总压在第一个实例上。"""
        candidates = self.find(capability, action, exclude=exclude, labels=labels)
        if not candidates:
            return None
        key = (capability, action)
        with self._lock:
            idx = self._rr.get(key, 0) % len(candidates)
            self._rr[key] = idx + 1
        return candidates[idx]

    # ---------- 持久化 ----------
    def _save_locked(self) -> None:
        if not self._persist:
            return
        data = {"version": 1, "agents": [rec.as_dict() for rec in self._agents.values()]}
        tmp = self._persist.with_suffix(self._persist.suffix + ".tmp")
        try:
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._persist)
        except OSError:
            pass

    def _load(self) -> None:
        try:
            data = json.loads(self._persist.read_text(encoding="utf-8"))  # type: ignore[union-attr]
        except (OSError, ValueError):
            return
        now = time.time()
        for item in data.get("agents", []):
            try:
                rec = AgentRecord(
                    agent_id=item["agent_id"],
                    roles=item.get("roles") or [],
                    endpoint=item.get("endpoint") or "",
                    labels=item.get("labels") or {},
                    ttl_s=float(item.get("ttl_s") or self._default_ttl),
                    last_seen=float(item.get("last_seen") or 0),
                    registered_at=float(item.get("registered_at") or now),
                    meta=item.get("meta") or {},
                )
            except (KeyError, TypeError, ValueError):
                continue
            if rec.alive(now):
                self._agents[rec.agent_id] = rec


def _supports(rec: AgentRecord, capability: str, action: str) -> bool:
    for role in rec.roles:
        if role.get("name") != capability:
            continue
        actions = role.get("actions") or []
        if action in actions or "*" in actions:
            return True
    return False


def _clamp_ttl(value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return DEFAULT_TTL_S
    return max(MIN_TTL_S, min(MAX_TTL_S, v))