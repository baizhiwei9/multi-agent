"""跨 Agent 通讯平台：HTTP/JSON Hub + 长轮询 Agent SDK。

不经过大模型：协调端发 `task`，Agent 本地执行代码，回 `result`。
"""

from .protocol import VERSION, ProtocolError, envelope, validate
from .client import AgentRuntime, HubClient, HubError
from .server import HubServer

__all__ = ["VERSION", "ProtocolError", "envelope", "validate",
           "AgentRuntime", "HubClient", "HubError", "HubServer"]