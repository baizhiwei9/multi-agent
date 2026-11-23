"""确定性回放引擎（示例）：step(state, cmd) 必须是纯函数。

风控官的 replay 动作会把这个文件放到子进程里跑两遍，比较最终状态哈希。
真实项目里换成战斗/结算/经济系统的 tick 模拟即可。
"""
from __future__ import annotations

import copy


def initial_state():
    return {"tick": 0, "players": {}, "gold": {}}


def step(state, cmd):
    st = copy.deepcopy(state)
    st["tick"] = int(st.get("tick", 0)) + 1
    cmd = cmd or {}
    op = cmd.get("op")
    player = str(cmd.get("player") or "")
    if op == "join":
        st.setdefault("players", {})[player] = {"room": "r-1", "ready": True}
    elif op == "buy":
        cost = int(cmd.get("cost") or 0)
        gold = dict(st.get("gold") or {})
        gold[player] = int(gold.get(player, 1000)) - cost
        st["gold"] = gold
    elif op == "claim":
        gain = int(cmd.get("gold") or 0)
        gold = dict(st.get("gold") or {})
        gold[player] = int(gold.get(player, 0)) + gain
        st["gold"] = gold
    elif op == "tick":
        pass
    return st