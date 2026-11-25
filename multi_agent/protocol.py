"""协议层：消息信封、字段校验、版本与错误码。

设计原则
--------
- 纯 JSON over HTTP，跨网络/NAT/Pod 都能走；
- 执行链路不经过大模型：协调端只发 `task`，Agent 本地跑自己的代码；
- 能力用 `capability + action` 两级定位，避免"发错人"；
- 所有消息带 `v`（协议版本）与 `id`（幂等/审计用）。

消息类型
--------
register / heartbeat / task / result / cancel / error
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Iterable

VERSION = 1

TYPES = ("register", "heartbeat", "task", "result", "cancel", "error")

REQUIRED: Dict[str, Iterable[str]] = {
    "register": ("agent_id", "roles"),
    "heartbeat": ("agent_id",),
    "task": ("task_id", "capability", "action"),
    "result": ("task_id", "agent_id", "ok"),
    "cancel": ("task_id",),
    "error": ("code", "message"),
}

ERROR_CODES = (
    "bad_request",
    "unauthorized",
    "forbidden",
    "not_found",
    "conflict",
    "no_agent",
    "timeout",
    "handler_error",
    "canceled",
    "internal",
)


class ProtocolError(ValueError):
    """消息不符合协议时抛出，HTTP 层映射为 400。"""


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


def envelope(type_: str, **fields: Any) -> Dict[str, Any]:
    """构造一条标准消息。"""
    msg: Dict[str, Any] = {"v": VERSION, "type": type_, "id": new_id(), "ts": now_iso()}
    msg.update(fields)
    return msg


def validate_role(role: Any) -> Dict[str, Any]:
    if not isinstance(role, dict):
        raise ProtocolError("role 必须是对象")
    name = role.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ProtocolError("role.name 必须是非空字符串")
    actions = role.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ProtocolError(f"role[{name}].actions 必须是非空数组")
    for a in actions:
        if not isinstance(a, str) or not a.strip():
            raise ProtocolError(f"role[{name}] 的 action 必须是非空字符串")
    return {
        "name": name.strip(),
        "actions": [a.strip() for a in actions],
        "description": str(role.get("description") or ""),
    }


def validate(msg: Any) -> Dict[str, Any]:
    """校验并返回消息本体；不合法抛 ProtocolError。"""
    if not isinstance(msg, dict):
        raise ProtocolError("消息必须是 JSON 对象")
    if msg.get("v") != VERSION:
        raise ProtocolError(f"协议版本不支持：{msg.get('v')!r}，当前 {VERSION}")
    t = msg.get("type")
    if t not in REQUIRED:
        raise ProtocolError(f"未知消息类型：{t!r}")
    if not isinstance(msg.get("id"), str) or not msg["id"]:
        raise ProtocolError("缺少消息 id")
    for key in REQUIRED[t]:
        if key not in msg:
            raise ProtocolError(f"{t} 消息缺少字段：{key}")

    if t == "register":
        if not isinstance(msg["agent_id"], str) or not msg["agent_id"].strip():
            raise ProtocolError("agent_id 必须是非空字符串")
        roles = msg["roles"]
        if not isinstance(roles, list) or not roles:
            raise ProtocolError("roles 必须是非空数组")
        msg["roles"] = [validate_role(r) for r in roles]
        if "ttl_s" in msg and not isinstance(msg["ttl_s"], (int, float)):
            raise ProtocolError("ttl_s 必须是数字")
        if "labels" in msg and not isinstance(msg["labels"], dict):
            raise ProtocolError("labels 必须是对象")

    elif t == "task":
        if not isinstance(msg["task_id"], str) or not msg["task_id"]:
            raise ProtocolError("task_id 必须是非空字符串")
        for key in ("capability", "action"):
            if not isinstance(msg[key], str) or not msg[key].strip():
                raise ProtocolError(f"{key} 必须是非空字符串")
        if "params" in msg and not isinstance(msg["params"], dict):
            raise ProtocolError("params 必须是对象")
        if "timeout_s" in msg:
            v = msg["timeout_s"]
            if not isinstance(v, (int, float)) or v <= 0:
                raise ProtocolError("timeout_s 必须是正数")

    elif t == "result":
        if not isinstance(msg["ok"], bool):
            raise ProtocolError("result.ok 必须是布尔值")
        if not msg["ok"]:
            err = msg.get("error")
            if not isinstance(err, dict) or not err.get("code"):
                raise ProtocolError("失败的 result 必须带 error.code")
            if err.get("code") not in ERROR_CODES:
                raise ProtocolError(f"未知错误码：{err.get('code')!r}")

    elif t == "cancel":
        if not isinstance(msg["task_id"], str) or not msg["task_id"]:
            raise ProtocolError("task_id 必须是非空字符串")

    return msg


def task_message(task: Dict[str, Any], agent_id: str) -> Dict[str, Any]:
    """把存储层任务转成投递给 Agent 的 `task` 消息。

    `agent_id` / `attempt` 是投递归属：Hub 出队时会用它们和任务当前状态比对，
    任务被重派给下一个 Agent 后，收件箱里属于上一轮 attempt 的旧消息必须作废。
    """
    return envelope(
        "task",
        from_="hub",
        to=agent_id,
        agent_id=agent_id,
        attempt=int(task.get("attempts") or 0),
        task_id=task["id"],
        capability=task["capability"],
        action=task["action"],
        params=task.get("params") or {},
        timeout_s=task.get("timeout_s"),
        idempotency_key=task.get("idempotency_key"),
    )


def result_message(task_id: str, agent_id: str, ok: bool,
                   result: Any = None, error: Any = None,
                   duration_ms: int | None = None) -> Dict[str, Any]:
    msg = envelope("result", task_id=task_id, agent_id=agent_id, ok=bool(ok),
                   result=result, error=error)
    if duration_ms is not None:
        msg["duration_ms"] = int(duration_ms)
    return msg