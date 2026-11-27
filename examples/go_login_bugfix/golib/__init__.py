"""示例自带的 Go 工具链 / 自愈 / 同步：放在示例目录里，框架目录零侵入。

    go_build / run_login / tool_specs   go_toolchain.py   绑 Go 编译器
    selfheal                            go_heal.py        编不过就规则修（0 token）
    sync_artifact                       go_sync.py        同步到目标端并自证

只借框架的只读工具集（`multi_agent.integrations.dev_tools`），不改框架任何文件。
"""
from .go_heal import parse_build_errors, selfheal
from .go_sync import sync_artifact
from .go_toolchain import find_go, go_build, run_login, tool_specs

__all__ = ["find_go", "go_build", "run_login", "tool_specs",
           "parse_build_errors", "selfheal", "sync_artifact"]