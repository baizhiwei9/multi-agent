"""游戏服务端风控官（capability = ``server_risk``）：审计之后的上线闸门。

为什么单独一个角色
----------------
`integrations/risk.py` 是**行情风控**（波动率/回撤/资金流），和"服务端能不能上线"不是一回事。
这里不做预测、不做估值，只按服务器逻辑算硬指标，并把审计结论当成一道输入：

    capacity  —— CCU 余量、tick 预算（p99 是否吃掉帧）
    replay    —— 同一份输入跑两遍，状态哈希必须一致（确定性回放，防卡帧/回档）
    rollback  —— 回滚脚本是否真的存在、是否演练过
    canary    —— 灰度比例是否在安全区间
    economy   —— 产出上限/通胀是否会把经济打崩
    audit     —— 审计 fail / 关键 error 直接拦（strict 模式下）

输出只有三档：`pass` / `warn` / `block`，并给出 `blocked_by`（哪几条拦的），
报告官据此写"未通过"而不是"已通过"。

边界
----
· 回放会**执行被审仓库里的代码**（`server/sim.py`）：必须显式 `allow_exec=True`，
  且在**子进程 + 超时**里跑，绝不 import 到当前进程；
· 真实环境应把回放交给 `runner`（隔离容器/CI）执行，这里只提供同口径的本地复核；
· 拿不到证据时判 `warn` 并写明缺什么，不假装通过。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

#: 这些审计发现直接拦上线（strict=True 时）
CRITICAL_CODES = {
    "auth_missing", "tx_missing", "secret_hardcoded",
    "migration_gap", "migration_destructive", "migration_duplicate",
    "drop_prob_sum", "config_item_missing", "endpoint_duplicate",
}

_DRIVER = r"""
import importlib.util, json, sys
engine_path, scenario_path = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("game_engine", engine_path)
mod = importlib.util.module_from_spec(spec)
sys.modules["game_engine"] = mod
spec.loader.exec_module(mod)
state = mod.initial_state() if hasattr(mod, "initial_state") else {}
with open(scenario_path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
cmds = data.get("commands") if isinstance(data, dict) else data
if not isinstance(cmds, list):
    raise SystemExit("scenario 必须是 list 或 {commands: [...]}")
step = getattr(mod, "step")
for cmd in cmds:
    state = step(state, cmd)
print(json.dumps({"ok": True, "state": state, "commands": len(cmds)},
                 sort_keys=True, ensure_ascii=False))
"""


def _hash_state(state: Any) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def _check(checks: List[Dict[str, Any]], name: str, level: str, detail: str,
           **evidence: Any) -> None:
    checks.append({"name": name, "level": level, "detail": detail,
                   "evidence": evidence or {}})


# ═══════════════════════════════════════════════════════════ 容量
def capacity_check(params: Dict[str, Any], checks: List[Dict[str, Any]]) -> Dict[str, Any]:
    expected = params.get("expected_ccu")
    limit = params.get("ccu_limit")
    tick_budget = params.get("tick_budget_ms")
    p99 = params.get("p99_tick_ms")
    out: Dict[str, Any] = {}

    if isinstance(expected, (int, float)) and isinstance(limit, (int, float)) \
            and limit and not isinstance(expected, bool) and not isinstance(limit, bool):
        ratio = float(expected) / float(limit)
        out["ccu_ratio"] = round(ratio, 4)
        out["headroom_pct"] = round((1 - ratio) * 100, 1)
        if ratio > 0.9:
            _check(checks, "capacity_ccu", "block",
                   f"预期 {expected} CCU / 容量 {limit} = {ratio:.0%}，没有余量", ratio=ratio)
        elif ratio > 0.75:
            _check(checks, "capacity_ccu", "warn",
                   f"预期 {expected} CCU / 容量 {limit} = {ratio:.0%}，建议扩容", ratio=ratio)
        else:
            _check(checks, "capacity_ccu", "ok",
                   f"预期 {expected} CCU / 容量 {limit} = {ratio:.0%}", ratio=ratio)
    else:
        _check(checks, "capacity_ccu", "warn", "缺少 expected_ccu / ccu_limit，容量未评估")
        out["ccu_ratio"] = None

    if isinstance(tick_budget, (int, float)) and isinstance(p99, (int, float)) \
            and tick_budget and not isinstance(tick_budget, bool):
        use = float(p99) / float(tick_budget)
        out["tick_use"] = round(use, 4)
        if use > 1.0:
            _check(checks, "capacity_tick", "block",
                   f"p99 tick {p99}ms 超帧预算 {tick_budget}ms（{use:.0%}）", use=use)
        elif use > 0.8:
            _check(checks, "capacity_tick", "warn",
                   f"p99 tick 已用 {use:.0%} 预算，再叠加逻辑会掉帧", use=use)
        else:
            _check(checks, "capacity_tick", "ok",
                   f"p99 tick 用 {use:.0%} 预算", use=use)
    else:
        _check(checks, "capacity_tick", "warn", "缺少 tick_budget_ms / p99_tick_ms，帧预算未评估")
        out["tick_use"] = None
    return out


# ═══════════════════════════════════════════════════════════ 确定性回放
def run_replay(params: Dict[str, Any]) -> Dict[str, Any]:
    """跑同一 scenario 多次，比较最终状态哈希。执行被审代码，需 allow_exec=True。"""
    engine = params.get("engine")
    scenario = params.get("scenario")
    runs = int(params.get("runs") or 2)
    timeout = float(params.get("timeout_s") or 30)
    if not params.get("allow_exec"):
        return {"ok": False, "skipped": True, "deterministic": None,
                "reason": "未显式允许执行被审代码（allow_exec=True），回放跳过"}
    if not engine or not os.path.isfile(engine):
        return {"ok": False, "skipped": True, "deterministic": None,
                "reason": f"engine 不存在：{engine!r}"}

    tmpdir = tempfile.mkdtemp(prefix="gs-replay-")
    scenario_path = os.path.join(tmpdir, "scenario.json")
    try:
        if isinstance(scenario, list):
            with open(scenario_path, "w", encoding="utf-8") as fh:
                json.dump({"commands": scenario}, fh, ensure_ascii=False)
        elif isinstance(scenario, str) and os.path.isfile(scenario):
            scenario_path = scenario
        else:
            return {"ok": False, "skipped": True, "deterministic": None,
                    "reason": "scenario 必须是命令列表或 JSON 文件路径"}
    except OSError as e:
        return {"ok": False, "skipped": True, "deterministic": None,
                "reason": f"scenario 写入失败：{e}"}

    hashes: List[str] = []
    states: List[Any] = []
    errors: List[str] = []
    started = time.time()
    for _ in range(max(2, runs)):
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _DRIVER, os.path.abspath(engine), scenario_path],
                capture_output=True, text=True, timeout=timeout,
                cwd=os.path.dirname(os.path.abspath(engine)) or None)
        except subprocess.TimeoutExpired:
            errors.append(f"回放超时（>{timeout}s）")
            break
        except OSError as e:
            errors.append(f"回放进程启动失败：{e}")
            break
        if proc.returncode != 0:
            errors.append((proc.stderr or proc.stdout or "未知错误").strip()[-800:])
            break
        line = (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else ""
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"回放输出不是 JSON：{line[:200]}")
            break
        hashes.append(_hash_state(payload.get("state")))
        states.append(payload.get("state"))

    deterministic = None
    if hashes:
        deterministic = len(set(hashes)) == 1
    return {
        "ok": not errors and deterministic is True,
        "skipped": False,
        "deterministic": deterministic,
        "runs": len(hashes),
        "hashes": hashes,
        "final_state": states[0] if states else None,
        "errors": errors,
        "elapsed_ms": int((time.time() - started) * 1000),
        "scenario": scenario_path,
        "engine": os.path.abspath(engine),
    }


# ═══════════════════════════════════════════════════════════ 闸门
def gate(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """上线闸门：把审计回执 + 服务器证据合成 pass/warn/block。"""
    params = params or {}
    checks: List[Dict[str, Any]] = []
    notes: List[str] = []
    strict = params.get("strict", True)

    # 1) 审计结论
    audit = params.get("audit") if isinstance(params.get("audit"), dict) else {}
    verdict = audit.get("verdict") or audit.get("decision")
    findings = [f for f in (audit.get("findings") or []) if isinstance(f, dict)]
    hard_errors = [f for f in findings if f.get("severity") == "error"]
    critical = [f for f in hard_errors if f.get("code") in CRITICAL_CODES]
    if verdict == "fail":
        _check(checks, "audit", "block" if strict else "warn",
               f"审计 verdict=fail（{len(hard_errors)} 个 error）",
               errors=[f.get("code") for f in hard_errors][:10])
    elif verdict == "suspect":
        _check(checks, "audit", "warn",
               f"审计 verdict=suspect（{len([f for f in findings if f.get('severity') == 'warn'])} 个 warn）")
    elif verdict == "pass":
        _check(checks, "audit", "ok", "审计 verdict=pass")
    else:
        _check(checks, "audit", "warn", "没有审计回执，无法判断开发与文档是否一致")
    if critical:
        _check(checks, "audit_critical", "block",
               f"{len(critical)} 个关键缺陷（鉴权/事务/迁移/经济/密钥）",
               codes=[f.get("code") for f in critical][:10])

    # 2) 容量
    capacity = capacity_check(params.get("capacity") or {}, checks)

    # 3) 确定性回放（可直接给证据，也可现场跑）
    replay = params.get("replay")
    replay_evidence: Dict[str, Any]
    if isinstance(replay, dict) and ("engine" in replay or "allow_exec" in replay):
        replay_evidence = run_replay(replay)
    elif isinstance(replay, dict):
        replay_evidence = dict(replay)
    else:
        replay_evidence = {}
    if not replay_evidence:
        _check(checks, "replay", "warn", "没有回放证据，确定性未验证")
    elif replay_evidence.get("skipped") or replay_evidence.get("deterministic") is None:
        _check(checks, "replay", "warn",
               replay_evidence.get("reason") or "回放被跳过，确定性未验证")
    elif replay_evidence.get("errors"):
        _check(checks, "replay", "block",
               f"回放报错：{replay_evidence['errors'][0][:160]}")
    elif replay_evidence.get("deterministic") is False:
        _check(checks, "replay", "block",
               "同一输入两次回放状态哈希不一致（非确定性，可能回档/漂移）",
               hashes=replay_evidence.get("hashes"))
    else:
        _check(checks, "replay", "ok",
               f"{replay_evidence.get('runs', 2)} 次回放状态一致",
               hash=(replay_evidence.get("hashes") or [None])[0])

    # 4) 回滚 / 灰度
    rollback = params.get("rollback") if isinstance(params.get("rollback"), dict) else {}
    script = rollback.get("script")
    tested = rollback.get("tested")
    require_rollback = params.get("require_rollback", True)
    if not rollback:
        _check(checks, "rollback", "warn" if require_rollback else "ok", "没有回滚方案")
    else:
        exists = isinstance(script, str) and (os.path.isfile(script) or script.strip())
        if not exists:
            _check(checks, "rollback", "block" if require_rollback else "warn",
                   "回滚方案存在但没有可执行脚本/步骤", plan=bool(rollback.get("plan")))
        elif tested is False:
            _check(checks, "rollback", "warn", "回滚脚本未演练（tested=false）")
        else:
            _check(checks, "rollback", "ok", "回滚脚本存在" + ("且已演练" if tested else ""))

    canary = params.get("canary_pct")
    if isinstance(canary, (int, float)) and not isinstance(canary, bool):
        if canary <= 0:
            _check(checks, "canary", "warn", "灰度比例为 0（等于全量直发）")
        elif canary <= 10:
            _check(checks, "canary", "ok", f"灰度 {canary}%")
        elif canary <= 20:
            _check(checks, "canary", "warn", f"灰度 {canary}% 偏大，建议先 1-5%")
        else:
            _check(checks, "canary", "block", f"灰度 {canary}% 过大，出问题影响面不可控")
    else:
        _check(checks, "canary", "warn", "没有灰度比例")

    # 5) 经济
    economy = params.get("economy") if isinstance(params.get("economy"), dict) else {}
    if not economy:
        _check(checks, "economy", "warn", "没有经济证据（产出上限/通胀）")
    else:
        cap = economy.get("faucet_daily_cap", economy.get("max_gold_per_day"))
        infl = economy.get("inflation_daily_pct")
        if cap in (None, 0, "0"):
            _check(checks, "economy", "warn", "缺每日产出上限")
        else:
            _check(checks, "economy", "ok", f"每日产出上限 {cap}")
        if isinstance(infl, (int, float)) and not isinstance(infl, bool):
            if float(infl) > 1.0:
                _check(checks, "inflation", "block",
                       f"每日通胀 {infl}% > 1%，经济会崩", inflation_daily_pct=infl)
            elif float(infl) > 0.5:
                _check(checks, "inflation", "warn", f"每日通胀 {infl}% 偏高",
                       inflation_daily_pct=infl)
            else:
                _check(checks, "inflation", "ok", f"每日通胀 {infl}%")

    blocked_by = [c["name"] for c in checks if c["level"] == "block"]
    warns = [c["name"] for c in checks if c["level"] == "warn"]
    result = "block" if blocked_by else ("warn" if warns else "pass")
    if result != "pass":
        notes.append("闸门未通过：按 blocked_by/warns 逐条整改后重跑" if result == "block"
                     else "闸门有条件通过：warns 需在上线前确认")
    return {
        "ok": True,
        "gate": result,
        "verdict": result,
        "checks": checks,
        "blocked_by": blocked_by,
        "warns": warns,
        "notes": notes,
        "capacity": capacity,
        "replay": {k: replay_evidence.get(k) for k in
                   ("deterministic", "runs", "hashes", "errors", "skipped", "reason")},
        "strict": bool(strict),
    }


def run_action(action: str, params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None
               ) -> Dict[str, Any]:
    if action == "gate":
        return gate(params, ctx)
    if action == "capacity":
        checks: List[Dict[str, Any]] = []
        out = capacity_check(params.get("capacity") or params, checks)
        return {"ok": True, "capacity": out, "checks": checks}
    if action == "replay":
        return {"ok": True, **run_replay(params)}
    raise ValueError(f"server_risk 不支持的动作：{action}")


RISK_ACTIONS = ("gate", "capacity", "replay")


def register(runtime, capability: str = "server_risk",
             actions: tuple = RISK_ACTIONS) -> None:
    for action in actions:
        runtime.add_handler(capability, action,
                            (lambda a: (lambda p, c: run_action(a, p, c)))(action))