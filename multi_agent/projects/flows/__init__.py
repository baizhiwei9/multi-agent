"""编排层：**流水线不属于任何角色项目**。

一条编排就是一张 DAG，只写 `capability + action`，不关心这些角色在哪个进程、
由哪个项目实现。角色项目各自起进程、各自注册，编排在 Hub 上按名字找人。

好处：
- 换实现（bug 采集换成 Sentry、dev 换成外部编码 Agent）不动编排；
- 换编排（bug 修完直接发版 / 先灰度）不动角色；
- 少一个角色就知道少谁：`check_capabilities()` 直接报缺哪个 capability。

用法：

    from multi_agent.projects.flows import register_all, check_capabilities
    register_all()                              # 把 DAG 塞进 PIPELINES
    check_capabilities("game_server_bugfix", online_caps)
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

FLOWS: Dict[str, Dict[str, Any]] = {}


def register_flow(spec: Dict[str, Any], registry: Optional[Any] = None) -> str:
    """登记一条编排（写进 `multi_agent.pipelines.PIPELINES`）。"""
    from ...pipelines import PIPELINES

    name = str(spec.get("name") or "").strip()
    if not name:
        raise ValueError("编排必须有 name")
    spec = dict(spec)
    spec.setdefault("flow", True)
    FLOWS[name] = spec
    PIPELINES[name] = spec
    return name


def required_capabilities(spec: Dict[str, Any]) -> List[str]:
    """这条编排需要哪些 capability（去重排序）。"""
    caps: Set[str] = set()
    for step in (spec or {}).get("steps") or []:
        cap = step.get("capability")
        if cap:
            caps.add(str(cap))
    return sorted(caps)


def check_capabilities(spec: Dict[str, Any], online: Iterable[str]) -> Dict[str, Any]:
    """编排所需能力 vs 当前在线能力：缺谁、谁多余。"""
    need = required_capabilities(spec)
    have = {str(c) for c in (online or [])}
    missing = [c for c in need if c not in have]
    return {"ok": not missing, "need": need, "missing": missing,
            "extra": sorted(have - set(need))}


def register_all(registry: Optional[Any] = None) -> List[str]:
    """把内置编排全部登记（导入时已自动登记；这里用于显式刷新）。"""
    from . import bugfix, news_balance  # noqa: F401 - 导入即登记

    names = [bugfix.BUGFIX_FLOW["name"], news_balance.NEWS_BALANCE_FLOW["name"]]
    for name in names:
        if name in FLOWS:
            register_flow(FLOWS[name], registry)
    return names


def _autoload() -> None:
    """导入即登记：让 `PIPELINES` 里始终有这两条编排，不用调用方记得先 register。"""
    from . import bugfix, news_balance

    for spec in (bugfix.BUGFIX_FLOW, news_balance.NEWS_BALANCE_FLOW):
        register_flow(spec)


_autoload()