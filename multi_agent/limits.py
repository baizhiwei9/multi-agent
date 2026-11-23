"""限额配置：审计字数、引用条数、评议轮数、角色数量。

为什么要单独一层
---------------
"最多引用 5 处错误观点 / 审计报告最多 5000 字 / 多轮评议最多 10 轮"这些数字
会被审计官、评议器、提示词共用，散落在代码里必然漂移。统一从
`config/limits.json` 读，改数字不用改代码。

查找顺序（`load_limits()`）
--------------------------
1. 环境变量 `MULTI_AGENT_LIMITS` 指定的文件；
2. 当前工作目录的 `config/limits.json`；
3. 仓库根的 `config/limits.json`；
4. 都没有就用本模块的 `DEFAULTS`（装成 wheel 时的兜底）。

校验原则：**错了就报错，不静默降级**。限额写坏（0、负数、字符串、轮数 > 50）
比没有限额更危险，`LimitsError` 直接把问题抛给调用方。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_REL = Path("config") / "limits.json"

#: 内置兜底默认值；与仓库根 config/limits.json 保持一致
DEFAULTS: Dict[str, Any] = {
    "audit": {
        "max_citations": 5,
        "max_input_chars": 5000,
        "max_output_chars": 5000,
        "max_rounds": 10,
        "max_total_chars": 100000,
        "on_exceed": "redo",
    },
    # 多方辩论：成本按"发言条数"算（三方一轮 = 三条），所以轮数之外再加两道闸：
    # `max_participants` 卡人数、`max_speeches` 卡总发言条数。
    "debate": {"max_rounds": 10, "max_message_chars": 5000, "max_total_chars": 100000,
               "max_participants": 6, "max_speeches": 24},
    "news": {"max_items_per_collector": 50, "max_topics": 5},
    # 角色规模：项目角色表里用 `RoleSpec(..., instances=N)` 声明"这个角色要几个 Agent"，
    # `overrides` 是全局覆盖（按 capability 名，改这里不用改代码），`default_instances`
    # 是没声明的角色的兜底。`co_locate` 是"可以合在一个 Agent 上"的角色组
    # （例 [["B","supervisor"]]：Worker 少时采集官兼管理员）。
    "roles": {"default_instances": 1, "max_instances_per_role": 50,
              "overrides": {}, "co_locate": []},
}

_ON_EXCEED = ("redo", "warn", "truncate")
_BOUNDS = {
    ("audit", "max_citations"): (1, 50),
    ("audit", "max_input_chars"): (200, 200000),
    ("audit", "max_output_chars"): (200, 200000),
    ("audit", "max_rounds"): (1, 50),
    ("audit", "max_total_chars"): (1000, 5000000),
    ("debate", "max_rounds"): (1, 50),
    ("debate", "max_message_chars"): (100, 200000),
    ("debate", "max_total_chars"): (1000, 5000000),
    ("debate", "max_participants"): (1, 12),
    ("debate", "max_speeches"): (1, 200),
    ("news", "max_items_per_collector"): (1, 1000),
    ("news", "max_topics"): (1, 50),
    ("roles", "default_instances"): (0, 50),
    ("roles", "max_instances_per_role"): (1, 200),
}


class LimitsError(ValueError):
    """限额配置不合法（类型错、越界、未知取值）。"""


class Section:
    """一段限额（只读属性式访问：`limits.audit.max_citations`）。"""

    __slots__ = ("_data", "_name")

    def __init__(self, name: str, data: Dict[str, Any]):
        self._name, self._data = name, dict(data)

    def __getattr__(self, key: str) -> Any:
        try:
            return self._data[key]
        except KeyError as e:  # pragma: no cover - 访问未定义字段属编程错误
            raise AttributeError(f"limits.{self._name} 没有字段 {key}") from e

    def as_dict(self) -> Dict[str, Any]:
        return dict(self._data)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Section {self._name} {self._data}>"


class Limits:
    """整套限额：audit / debate / news / roles。"""

    __slots__ = ("audit", "debate", "news", "roles", "source", "raw")

    def __init__(self, raw: Dict[str, Any], source: str = "defaults"):
        self.raw = raw
        self.source = source
        self.audit = Section("audit", raw["audit"])
        self.debate = Section("debate", raw["debate"])
        self.news = Section("news", raw["news"])
        self.roles = Section("roles", raw["roles"])

    def as_dict(self) -> Dict[str, Any]:
        return {"audit": self.audit.as_dict(), "debate": self.debate.as_dict(),
                "news": self.news.as_dict(), "roles": self.roles.as_dict(),
                "source": self.source}

    def instances_for(self, capability: str, declared: Optional[int] = None) -> int:
        """某个角色实际要几个 Agent。

        优先级：`roles.overrides[capability]`（全局改规模） → 项目角色表的
        `RoleSpec.instances`（声明） → `roles.default_instances`（兜底）。
        声明值必须 1..`roles.max_instances_per_role`，超了直接报错——
        这里是唯一起进程的执行入口，只在 `validate()` 查 ≥1 不够
        （`instances=60` 会被原样起 60 个进程）。
        """
        roles = self.roles.as_dict()
        cap_max = int(roles.get("max_instances_per_role", 50))
        overrides = roles.get("overrides") or {}
        if capability in overrides:
            return int(overrides[capability])
        if declared is not None:
            if isinstance(declared, bool) or not isinstance(declared, int):
                raise LimitsError(f"角色 {capability} 的 instances 必须是整数（当前 {declared!r}）")
            if declared < 1:
                raise LimitsError(f"角色 {capability} 的 instances 至少为 1（当前 {declared}）")
            if declared > cap_max:
                raise LimitsError(f"角色 {capability} 的 instances={declared} 超过上限 "
                                  f"{cap_max}（改 config/limits.json 的 max_instances_per_role）")
            return declared
        return int(roles.get("default_instances", 1))


_CACHE: Dict[str, tuple] = {}


def limits_path(explicit: Optional[str] = None) -> Optional[Path]:
    """按优先级找配置文件；都没有返回 None（用内置默认值）。"""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get("MULTI_AGENT_LIMITS")
    if env:
        candidates.append(Path(env))
    candidates += [Path.cwd() / CONFIG_REL, REPO_ROOT / CONFIG_REL]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = json.loads(json.dumps(base))          # 深拷贝，别改 DEFAULTS
    for key, value in (over or {}).items():
        if key.startswith("_"):
            continue                            # `_doc` 之类的说明字段
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def validate(raw: Dict[str, Any]) -> Dict[str, Any]:
    """校验并回填：未知字段保留（便于扩展），已知字段必须合法。"""
    for section in ("audit", "debate", "news", "roles"):
        if not isinstance(raw.get(section), dict):
            raise LimitsError(f"[{section}] 必须是对象")
        data = raw[section]
        for (sec, key), (lo, hi) in _BOUNDS.items():
            if sec != section or key not in data:
                continue
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, int):
                raise LimitsError(f"[{section}].{key} 必须是整数（当前 {value!r}）")
            if not lo <= value <= hi:
                raise LimitsError(f"[{section}].{key} 必须在 {lo}..{hi}（当前 {value}）")
    on_exceed = raw["audit"].get("on_exceed", "redo")
    if on_exceed not in _ON_EXCEED:
        raise LimitsError(f"[audit].on_exceed 只能是 {_ON_EXCEED}（当前 {on_exceed!r}）")
    # 单条字数不能超过总预算，否则第一轮就撞预算
    if raw["debate"]["max_message_chars"] > raw["debate"]["max_total_chars"]:
        raise LimitsError("[debate] max_message_chars 不能大于 max_total_chars")
    # 发言总数至少够跑完一轮，否则多方辩论第一轮就撞上限、等于没法辩
    if raw["debate"]["max_speeches"] < raw["debate"]["max_participants"]:
        raise LimitsError("[debate] max_speeches 不能小于 max_participants")
    # 角色规模：overrides 是 {capability: 实例数}，逐项必须是 0..max 的整数
    roles = raw["roles"]
    if roles["default_instances"] > roles["max_instances_per_role"]:
        raise LimitsError("[roles] default_instances 不能大于 max_instances_per_role")
    overrides = roles.get("overrides")
    if overrides is None:
        roles["overrides"] = {}
    elif not isinstance(overrides, dict):
        raise LimitsError("[roles].overrides 必须是对象（capability → 实例数）")
    else:
        for cap, count in overrides.items():
            if not isinstance(cap, str) or not cap.strip():
                raise LimitsError(f"[roles].overrides 的键必须是非空 capability 名（当前 {cap!r}）")
            if isinstance(count, bool) or not isinstance(count, int):
                raise LimitsError(f"[roles].overrides.{cap} 必须是整数（当前 {count!r}）")
            if not 0 <= count <= roles["max_instances_per_role"]:
                raise LimitsError(f"[roles].overrides.{cap} 必须在 0.."
                                  f"{roles['max_instances_per_role']}（当前 {count}）")
    # co_locate：可以合在一个 Agent 上的角色组，例如 [["B","supervisor"]]
    groups = roles.get("co_locate")
    if groups is None:
        roles["co_locate"] = []
    elif not isinstance(groups, list):
        raise LimitsError("[roles].co_locate 必须是数组（每个元素是一组 capability 名）")
    else:
        for group in groups:
            if not isinstance(group, list) or len(group) < 2:
                raise LimitsError(f"[roles].co_locate 每组至少两个 capability（当前 {group!r}）")
            for cap in group:
                if not isinstance(cap, str) or not cap.strip():
                    raise LimitsError(f"[roles].co_locate 里只能写 capability 名（当前 {cap!r}）")
            if len(set(group)) != len(group):
                raise LimitsError(f"[roles].co_locate 同一组里 capability 重复：{group}")
    return raw


def load_limits(path: Optional[str] = None, *, reload: bool = False) -> Limits:
    """读限额（带 mtime 缓存）。`path` 显式给文件时优先于环境变量。

    显式指定（参数或 `MULTI_AGENT_LIMITS`）但文件不存在 → 直接报错。
    以前会静默回退到仓库 `config/limits.json`，用户以为改了限额，
    实际读的是另一份——限额这类安全参数不能"悄悄换文件"。
    """
    explicit = path or os.environ.get("MULTI_AGENT_LIMITS")
    if explicit and not Path(explicit).is_file():
        raise LimitsError(f"限额文件不存在：{explicit}（显式指定时不做静默回退）")
    target = limits_path(path)
    if target is None:
        return Limits(_merge(DEFAULTS, {}), "defaults")
    try:
        stat = target.stat()
        key = f"{target.resolve()}::{stat.st_mtime_ns}"
    except OSError:
        key = ""
    if not reload and key and key in _CACHE:
        return _CACHE[key]
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise LimitsError(f"读不了限额文件 {target}：{type(e).__name__}: {e}") from e
    if not isinstance(raw, dict):
        raise LimitsError(f"限额文件 {target} 顶层必须是对象")
    merged = validate(_merge(DEFAULTS, raw))
    limits = Limits(merged, str(target))
    if key:
        _CACHE[key] = limits
    return limits


def truncate(text: Any, limit: int) -> str:
    """按字符数截断（按 `limit` 计，不额外加省略号，方便字数对账）。"""
    if text is None:
        return ""
    s = text if isinstance(text, str) else str(text)
    return s if len(s) <= limit else s[:limit]