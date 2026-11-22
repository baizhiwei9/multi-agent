"""项目扩展点：一个项目 = 角色表 + 流水线 + 审计口径 + 新闻源 + 报告器。

为什么要有这一层
----------------
框架（Hub / Coordinator / DBManager / 审计引擎）不该认识"期货平衡表"或"股票 MATT"。
项目方只继承 `Project`，声明五件事，框架负责把它们变成可运行的东西：

1. `roles`      —— 本项目的 capability/action 表（Agent 注册什么，就能接什么活）
2. `pipeline`   —— DAG（沿用框架的步骤语法，可直接塞进 PIPELINES）
3. `audit_project` —— 审计官用哪套 profile（balance_sheet / stock / bond / 自定义）
4. `news()`     —— 新闻采集官（`NewsCollector`），没有就返回 None
5. `reporter()` —— 报告器（`Reporter`），没有就返回 None

框架侧只做三件事：登记、把角色表变成 Agent manifest、把流水线塞进 PIPELINES。

新增一个项目的三种姿势
----------------------
- **只声明**：写好 roles / pipeline / audit_project，handlers 全继承默认（返回 degraded 说明），
  可以先让流水线跑通、把审计接上，再逐步填实现；
- **覆写 `handlers()`**：真正执行你项目里的脚本（本地或经 `runner` 远程执行都行）；
- **覆写 `news()` / `reporter()`**：换新闻源、换报告模板，流水线本身不用改。

继承层次（对应仓库里三个实现）：

    Project
      ├─ FuturesBalanceProject   （BalanceAI：A/B/T/F/Q/E/J + 平衡表流水线）
      ├─ StockMattProject        （stocks_matt_report：M1..M5 + 新闻采集官）
      └─ 你的项目（只需覆写自己那几项）

不继承也能用：`Project` 是普通类，`ProjectRegistry` 只要求"有 name/roles"这两样。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

Handler = Callable[[Dict[str, Any], Dict[str, Any]], Any]


@dataclass
class RoleSpec:
    """一个 capability（角色）及其动作表。"""

    name: str
    actions: List[str]
    description: str = ""

    def to_manifest(self) -> Dict[str, Any]:
        """转成 Hub 注册用的 manifest 片段。"""
        return {"name": self.name, "actions": list(self.actions),
                "description": self.description}


class Project:
    """项目基类：声明式优先，实现可后补。

    子类最少只需要写 `name` 和 `roles`；其余留空即"该项目暂不支持"，
    调用方会拿到明确的 `degraded` 说明，而不是静默空跑。
    """

    name: str = "project"
    title: str = "未命名项目"
    description: str = ""
    roles: Sequence[RoleSpec] = ()
    # 流水线 DAG：{"name": "...", "description": "...", "steps": [...]}
    pipeline: Optional[Dict[str, Any]] = None
    # 审计官用哪套 profile；None 表示不审计
    audit_project: Optional[str] = None
    # 流水线默认参数（可被调用方覆盖）
    default_params: Dict[str, Any] = field(default_factory=dict)

    # ---------------- 角色 / 协议 ----------------
    def role_specs(self) -> List[RoleSpec]:
        return [r if isinstance(r, RoleSpec) else RoleSpec(**r) for r in self.roles]

    def manifests(self) -> List[Dict[str, Any]]:
        """Agent 注册时直接拿去用的 capability 列表。"""
        return [r.to_manifest() for r in self.role_specs()]

    def capabilities(self) -> List[str]:
        return [r.name for r in self.role_specs()]

    def actions_for(self, capability: str) -> List[str]:
        for r in self.role_specs():
            if r.name == capability:
                return list(r.actions)
        return []

    # ---------------- 实现（默认"声明了但没实现"） ----------------
    def handlers(self) -> Dict[str, Dict[str, Handler]]:
        """{capability: {action: handler}}；默认空，即只有声明没有实现。"""
        return {}

    def register(self, runtime: Any) -> None:
        """把 handlers 挂到 AgentRuntime 上。"""
        for cap, actions in self.handlers().items():
            for action, fn in actions.items():
                runtime.add_handler(cap, action, fn)

    def news(self) -> Optional[Any]:
        """新闻采集官；返回 `NewsCollector` 实例或 None。"""
        return None

    def reporter(self) -> Optional[Any]:
        """报告器；返回 `Reporter` 实例或 None。"""
        return None

    # ---------------- 自检 ----------------
    def describe(self) -> Dict[str, Any]:
        news = self.news()
        rep = self.reporter()
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "roles": [{"name": r.name, "actions": list(r.actions),
                       "description": r.description} for r in self.role_specs()],
            "pipeline": (self.pipeline or {}).get("name"),
            "pipeline_steps": [s.get("name") for s in (self.pipeline or {}).get("steps", [])],
            "audit_project": self.audit_project,
            "news": getattr(news, "name", None),
            "reporter": getattr(rep, "name", None),
            "implemented": sorted(self.handlers().keys()),
            "default_params": dict(self.default_params or {}),
        }

    def validate(self) -> List[str]:
        """返回问题列表；空列表表示这个项目的声明是自洽的。"""
        problems: List[str] = []
        if not self.name or not self.name.strip():
            problems.append("name 不能为空")
        caps = self.capabilities()
        if not caps:
            problems.append("roles 不能为空（至少一个 capability）")
        if len(set(caps)) != len(caps):
            problems.append(f"capability 重名：{caps}")
        for cap in caps:
            if not self.actions_for(cap):
                problems.append(f"capability {cap} 没有 action")

        # 流水线里的 capability 必须在本项目角色表里（db / coordinator / auditor 除外）
        allowed = set(caps) | {"db", "coordinator", "auditor", "runner"}
        for step in (self.pipeline or {}).get("steps", []):
            cap = step.get("capability")
            if cap and cap not in allowed:
                problems.append(f"流水线步骤 {step.get('name')} 用了未声明的 capability：{cap}")
        return problems


class ProjectRegistry:
    """项目登记表（一个进程内一份）。"""

    def __init__(self) -> None:
        self._items: Dict[str, Project] = {}

    def register(self, project: Project, replace: bool = False) -> Project:
        key = project.name
        if key in self._items and not replace:
            raise ValueError(f"项目 {key} 已登记；要覆盖请传 replace=True")
        problems = project.validate()
        if problems:
            raise ValueError(f"项目 {key} 声明不自洽：" + "；".join(problems))
        self._items[key] = project
        return project

    def get(self, name: str) -> Project:
        if name not in self._items:
            raise KeyError(f"没有登记的项目 {name}；已登记：{sorted(self._items)}")
        return self._items[name]

    def names(self) -> List[str]:
        return sorted(self._items)

    def all(self) -> List[Project]:
        return [self._items[k] for k in self.names()]

    def describe_all(self) -> List[Dict[str, Any]]:
        return [p.describe() for p in self.all()]


REGISTRY = ProjectRegistry()


def register(project: Project, replace: bool = False) -> Project:
    return REGISTRY.register(project, replace=replace)


def install_pipelines(registry: Optional[ProjectRegistry] = None) -> List[str]:
    """把项目的流水线塞进框架的 PIPELINES，返回装进去的名字。"""
    from .. import pipelines as P

    reg = registry or REGISTRY
    added: List[str] = []
    for project in reg.all():
        if not project.pipeline:
            continue
        spec = dict(project.pipeline)
        spec.setdefault("project", project.name)
        if project.audit_project:
            spec.setdefault("audit_project", project.audit_project)
        P.PIPELINES[spec["name"]] = spec
        added.append(spec["name"])
    return added