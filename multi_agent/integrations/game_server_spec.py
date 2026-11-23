"""接口规范（interface-spec/1.0）：报告阶段的纯程序校验 + 用例生成。

为什么要"先验规范再分活"
----------------------
报告里的接口表如果本身不成立（方法乱写、路径没版本、写接口没幂等键、鉴权留空、
接口 ID 重复），拆出来的开发分片一定是错的——**错误会顺着 DAG 放大**。所以这里把
"接口规范合不合格"做成一道**前置门**：不合格就停在报告阶段，不拆包、不下发开发。

三道工序（都不烧 token）
----------------------
1. `check_interface_spec()` —— 纯规则校验，0 网络 0 token，输出 error/warn + 依据；
2. `generate_test_cases()`  —— 按规范给每个接口生成正向/鉴权/幂等/错误码/并发用例；
3. `generate_test_plan()`   —— 模板生成测试计划，**可选**调用大模型润色成测试报告
   （`GAME_TEST_LLM_URL` / `AUDIT_LLM_URL`，未配置就降级为模板并写明原因）。

规范字段（报告接口表一行）
--------------------------
| 字段 | 必填 | 约束 |
|---|---|---|
| 接口ID | 是 | `M-01` / `S-103`，全局唯一 |
| 调用方 → 被调方 | 是 | 用 `→` 分隔；内部接口要写鉴权机制 |
| 方法 | 是 | GET/POST/PUT/PATCH/DELETE |
| 路径 | 是 | `/v1/` 开头、小写 kebab、段数 ≤5 |
| 鉴权 | 是 | 是/否 + 机制（token / mTLS / 服务名 / 验签） |
| 幂等键 | 写接口必填 | request_id / match_id / room_id / order_id 等；读接口写 `—` |
| 错误码 | 写接口必填 | 大写 SNAKE，如 `ROOM_EXHAUSTED` |

扩展：加规则 = 在 `check_interface_spec` 里加一段 + 在 `RULES` 表登记；不要改流水线。
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

SPEC_VERSION = "interface-spec/1.0"

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_ALL_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_PATH_RE = re.compile(r"^/v\d+/[a-z0-9][a-z0-9\-]*(/[a-z0-9][a-z0-9\-]*)*$")
_ID_RE = re.compile(r"^[A-Z]{1,4}-\d{2,4}$")
_ERR_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
_STD_IDEM = {"request_id", "match_id", "room_id", "order_id", "ticket_id",
             "trace_id", "idempotency_key"}
_AUTH_MECH_HINTS = ("mtls", "服务名", "token", "验签", "签名", "oauth", "jwt", "service")
_CALLBACK_HINTS = ("支付平台", "第三方", "平台回调", "回调", "callback")
_INTERNAL_HINTS = ("服", "service", "svc", "gateway", "db", "cache")
_EMPTY = {"", "—", "-", "无", "n/a", "na", "none"}

#: 规则清单（给文档/报告引用，code 与 findings 一致）
RULES: Tuple[Tuple[str, str, str], ...] = (
    ("spec_empty", "error", "报告里没有任何接口声明"),
    ("method_invalid", "error", "方法不在 GET/POST/PUT/PATCH/DELETE 内"),
    ("path_invalid", "error", "路径不合规：必须 /vN/ 开头、小写 kebab、段数 ≤5"),
    ("auth_unspecified", "error", "鉴权列留空或无法识别"),
    ("idem_missing", "error", "写接口没有幂等键"),
    ("idem_exempt", "warn", "认证/心跳类写接口未写幂等键（已豁免）"),
    ("idem_key_nonstandard", "warn", "幂等键名不在标准集合（需团队约定）"),
    ("idem_on_read", "warn", "读接口写了幂等键（无副作用，通常不需要）"),
    ("errors_missing", "warn", "写接口没有声明错误码"),
    ("error_code_format", "warn", "错误码不是大写 SNAKE"),
    ("id_missing", "warn", "接口缺接口ID"),
    ("id_format", "warn", "接口ID 不符合 M-01 形式"),
    ("id_duplicate", "error", "接口ID 重复"),
    ("endpoint_duplicate", "error", "同一方法+路径重复声明"),
    ("caller_missing", "warn", "缺调用方"),
    ("callee_missing", "warn", "缺被调方"),
    ("internal_auth_weak", "warn", "内部接口没写鉴权机制（mTLS/服务名/token）"),
    ("callback_unsigned", "error", "回调接口没写验签/签名机制"),
)


def _finding(code: str, severity: str, message: str, where: str = "",
             evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"code": code, "severity": severity, "message": message,
            "where": where, "evidence": evidence or {}}


def _label(row: Dict[str, Any], idx: int) -> str:
    rid = str(row.get("id") or "").strip()
    if rid:
        return rid
    return f"{row.get('method') or '?'} {row.get('path') or '?'}"


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return " / ".join(str(v) for v in value)
    return str(value)


def _auth_of(row: Dict[str, Any]) -> Tuple[Optional[bool], str]:
    """返回 (是否需要鉴权, 原始鉴权文本)。容忍 bool / 中文 / "是（mTLS）"。"""
    raw = str(row.get("auth_raw") or "").strip()
    if not raw:
        auth = row.get("auth")
        if isinstance(auth, bool):
            raw = "是" if auth else "否"
        elif isinstance(auth, str):
            raw = auth
    low = raw.lower()
    if not low:
        return None, ""
    if low.startswith(("是", "yes", "true", "required", "需要", "必须")) or low in (
            "y", "✔", "✓"):
        return True, raw
    if low.startswith(("否", "no", "false", "public", "公开", "不需要")) or low in (
            "n", "—", "-"):
        return False, raw
    return None, raw


def _idem_of(row: Dict[str, Any]) -> str:
    return _text(row.get("idem")).strip()


def _errors_of(row: Dict[str, Any]) -> List[str]:
    raw = row.get("errors")
    if isinstance(raw, (list, tuple)):
        vals = [str(x).strip() for x in raw]
    else:
        vals = [x.strip() for x in re.split(r"[/,，、;；]", _text(raw))]
    return [x for x in vals if x and x.lower() not in _EMPTY]


def _is_internal(row: Dict[str, Any]) -> bool:
    caller = str(row.get("caller") or "")
    callee = str(row.get("callee") or "")
    return bool(re.search(r"(服|service|svc|gateway|db|cache)", callee, re.I)) and \
        not re.search(r"客户端|client", caller, re.I)


def _is_callback(row: Dict[str, Any]) -> bool:
    caller = str(row.get("caller") or "")
    return any(h in caller.lower() or h in caller for h in _CALLBACK_HINTS)


def check_interface_spec(endpoints: Optional[Sequence[Dict[str, Any]]],
                         require_report_fields: bool = True) -> Dict[str, Any]:
    """纯规则校验报告接口表。返回 ok / verdict / findings / counts。

    `require_report_fields=False` 用于协议文件（只有 method/path/auth 也能过）。
    """
    rows = [r for r in (endpoints or []) if isinstance(r, dict)]
    findings: List[Dict[str, Any]] = []
    if not rows:
        findings.append(_finding("spec_empty", "error",
                                 "报告里没有任何接口声明，无法拆包派活", "接口契约"))
        return {"ok": False, "verdict": "fail", "findings": findings,
                "counts": {"error": 1, "warn": 0, "interfaces": 0},
                "interfaces": 0, "spec_version": SPEC_VERSION}

    seen_ids: Dict[str, str] = {}
    seen_eps: Dict[Tuple[str, str], str] = {}
    for idx, row in enumerate(rows):
        where = _label(row, idx)
        method_raw = str(row.get("method_raw") or row.get("method") or "").strip()
        method = method_raw.upper()
        valid_method = row.get("method_valid")
        if valid_method is None:
            valid_method = method in _ALL_METHODS
        if not valid_method:
            findings.append(_finding("method_invalid", "error",
                                     f"方法 {method_raw!r} 不合法（只允许 GET/POST/PUT/PATCH/DELETE）",
                                     where))
        path = str(row.get("path") or "").strip()
        if not _PATH_RE.match(path):
            findings.append(_finding("path_invalid", "error",
                                     f"路径 {path!r} 不合规（/vN/ 开头、小写 kebab、段数 ≤5）",
                                     where))
        auth, auth_raw = _auth_of(row)
        if auth is None:
            findings.append(_finding("auth_unspecified", "error",
                                     f"鉴权列 {auth_raw!r} 无法识别（要写 是/否 + 机制）", where))
        mechanism = auth_raw.lower()
        if auth is True and _is_internal(row) and not any(h in mechanism for h in _AUTH_MECH_HINTS):
            findings.append(_finding("internal_auth_weak", "warn",
                                     "内部接口没有写鉴权机制（mTLS/服务名/token）", where,
                                     {"auth": auth_raw}))
        if _is_callback(row) and not any(h in mechanism for h in ("验签", "签名", "sign")):
            findings.append(_finding("callback_unsigned", "error",
                                     "回调接口没有写验签/签名机制", where, {"auth": auth_raw}))

        idem = _idem_of(row)
        if method in _WRITE_METHODS:
            if idem.lower() in _EMPTY:
                # 认证/心跳类写接口天然不是"业务写"：登录每次发新 token，
                # 强制幂等键反而会卡住正常登录 —— 豁免但留 warn 让人确认。
                if any(h in path.lower() for h in
                       ("login", "logout", "refresh", "verify", "heartbeat", "renew")):
                    findings.append(_finding("idem_exempt", "warn",
                                             "认证/心跳类接口未写幂等键（已豁免，确认无副作用）",
                                             where))
                else:
                    findings.append(_finding("idem_missing", "error",
                                             "写接口没有幂等键（重放会重复扣费/发货）", where))
            elif idem.lower() not in _STD_IDEM:
                findings.append(_finding("idem_key_nonstandard", "warn",
                                         f"幂等键 {idem!r} 不在标准集合，需团队约定", where,
                                         {"standard": sorted(_STD_IDEM)}))
            errors = _errors_of(row)
            if not errors:
                findings.append(_finding("errors_missing", "warn",
                                         "写接口没有声明错误码", where))
            bad = [e for e in errors if not _ERR_RE.match(e)]
            if bad:
                findings.append(_finding("error_code_format", "warn",
                                         f"错误码 {bad} 不是大写 SNAKE", where))
        elif method == "GET" and idem.lower() not in _EMPTY:
            findings.append(_finding("idem_on_read", "warn",
                                     f"读接口写了幂等键 {idem!r}（通常不需要）", where))

        rid = str(row.get("id") or "").strip()
        if require_report_fields:
            if not rid:
                findings.append(_finding("id_missing", "warn", "接口缺接口ID", where))
            elif not _ID_RE.match(rid):
                findings.append(_finding("id_format", "warn",
                                         f"接口ID {rid!r} 不符合 M-01 形式", where))
        if rid:
            if rid in seen_ids:
                findings.append(_finding("id_duplicate", "error",
                                         f"接口ID {rid!r} 重复", where,
                                         {"first": seen_ids[rid]}))
            else:
                seen_ids[rid] = where
        key = (method, path)
        if key in seen_eps:
            findings.append(_finding("endpoint_duplicate", "error",
                                     f"{method} {path} 重复声明", where))
        else:
            seen_eps[key] = where

        if require_report_fields:
            if not str(row.get("caller") or "").strip():
                findings.append(_finding("caller_missing", "warn", "缺调用方", where))
            if not str(row.get("callee") or "").strip():
                findings.append(_finding("callee_missing", "warn", "缺被调方", where))

    errors = [f for f in findings if f["severity"] == "error"]
    warns = [f for f in findings if f["severity"] == "warn"]
    return {
        "ok": not errors,
        "verdict": "fail" if errors else ("suspect" if warns else "pass"),
        "findings": findings,
        "counts": {"error": len(errors), "warn": len(warns), "interfaces": len(rows)},
        "interfaces": len(rows),
        "spec_version": SPEC_VERSION,
        "rules": [{"code": c, "severity": s, "description": d} for c, s, d in RULES],
    }


# ═══════════════════════════════════════════════════════════ 测试用例
_SCENARIOS: Tuple[Tuple[str, str], ...] = (
    ("happy", "正向：合法请求 → 2xx 且响应结构符合协议"),
    ("no_auth", "鉴权缺失：去掉 token/证书 → 401"),
    ("forbidden", "越权：低权限账号/角色 → 403"),
    ("bad_param", "参数校验：缺必填字段 → 400 且错误码稳定"),
    ("replay", "幂等重放：同幂等键重放 → 结果一致、无重复副作用"),
    ("concurrent", "并发：同玩家/同单并发写 → 只成功一次"),
    ("timeout", "超时重试：下游超时 → 重试后仍幂等"),
)


def generate_test_cases(endpoints: Optional[Sequence[Dict[str, Any]]],
                        max_cases_per_endpoint: int = 8) -> Dict[str, Any]:
    """按规范给每个接口生成测试用例（纯模板，0 token）。"""
    rows = [r for r in (endpoints or []) if isinstance(r, dict)]
    cases: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows):
        base = str(row.get("id") or "").strip() or f"EP{idx + 1}"
        method = str(row.get("method") or "").upper()
        path = str(row.get("path") or "")
        auth, auth_raw = _auth_of(row)
        idem = _idem_of(row)
        errors = _errors_of(row)
        internal = _is_internal(row)
        created = 0

        def add(scenario: str, priority: str, steps: List[str], expected: List[str],
                precondition: str = "") -> None:
            nonlocal created
            if created >= max_cases_per_endpoint:
                return
            created += 1
            cases.append({
                "id": f"TC-{base}-{created:02d}",
                "interface": base, "method": method, "path": path,
                "scenario": scenario,
                "priority": priority,
                "precondition": precondition,
                "steps": steps,
                "expected": expected,
            })

        add("happy", "P0",
            [f"以合法身份调用 {method} {path}", "校验响应码与响应体字段"],
            ["2xx", "响应字段与协议一致", "无 5xx / 无重试耗尽"])
        if auth:
            add("no_auth", "P0",
                ["不携带 token/证书调用"],
                ["401", "不产生副作用"], precondition="接口声明需要鉴权")
            add("forbidden", "P1",
                ["用低权限账号调用（管理接口用普通账号）"],
                ["403", "记录审计日志"])
        add("bad_param", "P1",
            ["构造缺必填字段/非法枚举的请求"],
            ["400", "错误码与协议一致", "不产生副作用"])
        if method in _WRITE_METHODS:
            if idem and idem.lower() not in _EMPTY:
                add("replay", "P0",
                    [f"同 {idem} 连续调用两次", "对比两次响应与存储状态"],
                    ["两次响应一致", "只产生一次副作用", "去重记录 TTL 正确"])
            add("concurrent", "P1",
                ["同玩家/同单并发发起两次写请求"],
                ["只成功一次", "另一次返回幂等结果或冲突码"])
            if internal:
                add("timeout", "P2",
                    ["注入下游 1s 超时", "等待客户端重试"],
                    ["重试后不重复扣费/发货", "错误码可区分"])
        if errors:
            for code in errors[:2]:
                add(f"error_{code.lower()}", "P1",
                    [f"构造触发 {code} 的前置条件"],
                    [f"返回 {code}", "不产生副作用 / 状态可恢复"])

    by_scenario: Dict[str, int] = {}
    for c in cases:
        by_scenario[c["scenario"]] = by_scenario.get(c["scenario"], 0) + 1
    return {"ok": True, "cases": cases, "count": len(cases),
            "by_scenario": by_scenario, "interfaces": len(rows)}


# ═══════════════════════════════════════════════════════════ 测试计划 / 报告
def _call_llm(prompt: str, timeout: float = 60.0) -> Tuple[Optional[str], str]:
    """调大模型写测试报告；未配置就返回原因，绝不偷偷烧 token。"""
    url = os.environ.get("GAME_TEST_LLM_URL") or os.environ.get("AUDIT_LLM_URL")
    if not url:
        return None, "未配置 GAME_TEST_LLM_URL / AUDIT_LLM_URL"
    body = json.dumps({"prompt": prompt, "max_tokens": 1600}).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001 - 网络/协议错误统一降级
        return None, f"调用失败：{e}"
    text = payload.get("text") or payload.get("content")
    if not text and isinstance(payload.get("choices"), list) and payload["choices"]:
        choice = payload["choices"][0] or {}
        text = (choice.get("message") or {}).get("content") or choice.get("text")
    if not isinstance(text, str) or not text.strip():
        return None, "模型回包没有可用文本"
    return text.strip(), ""


def _template_plan(cases: Sequence[Dict[str, Any]], endpoints: Sequence[Dict[str, Any]],
                   as_of: str) -> str:
    by_scenario: Dict[str, int] = {}
    for c in cases:
        by_scenario[c["scenario"]] = by_scenario.get(c["scenario"], 0) + 1
    lines = [
        f"# 测试计划与用例（{as_of}）", "",
        f"- 接口数：{len(endpoints)}", f"- 用例数：{len(cases)}",
        f"- 规范版本：{SPEC_VERSION}", "",
        "## 场景分布", "", "| 场景 | 用例数 |", "|---|---|",
    ]
    for k, v in sorted(by_scenario.items()):
        lines.append(f"| {k} | {v} |")
    lines += ["", "## 用例明细", "",
              "| 用例ID | 接口 | 场景 | 优先级 | 预期 |", "|---|---|---|---|---|"]
    for c in cases[:60]:
        lines.append(f"| {c['id']} | {c['interface']} {c['method']} {c['path']} "
                     f"| {c['scenario']} | {c['priority']} | {'；'.join(c['expected'])} |")
    if len(cases) > 60:
        lines.append(f"| … | … | 其余 {len(cases) - 60} 条见 evidence/cases.json | | |")
    lines += ["", "## 退出标准", "",
              "- P0 用例 100% 通过；P1 ≥ 95%；P2 记录不阻塞",
              "- 幂等/并发用例必须有存储侧证据（去重表、唯一索引）",
              "- 回调验签失败、越权、重放必须被拒绝且不产生副作用",
              "- 所有用例执行结果回填到发布报告，未执行项要写明原因", ""]
    return "\n".join(lines)


def generate_test_plan(cases: Sequence[Dict[str, Any]],
                       endpoints: Sequence[Dict[str, Any]],
                       llm: bool = False,
                       as_of: Optional[str] = None) -> Dict[str, Any]:
    """生成测试计划；`llm=True` 时调模型写报告，失败降级为模板并标注。"""
    as_of = as_of or datetime.now().strftime("%Y-%m-%d %H:%M")
    template = _template_plan(cases, endpoints, as_of)
    if not llm:
        return {"markdown": template, "llm_used": False, "degraded": False,
                "reason": "未启用大模型（llm=false），使用模板", "as_of": as_of}
    prompt = (
        "你是游戏服务端测试负责人。请根据下面的接口清单与用例，写一份中文测试报告（Markdown），"
        "必须包含：测试范围、环境与数据准备、按接口的测试重点、幂等/并发/越权/回调验签的风险点、"
        "退出标准、未覆盖项。不要编造接口或字段，只能使用给定信息。\n\n"
        "接口清单：\n" + json.dumps(list(endpoints), ensure_ascii=False, indent=2)[:12000] +
        "\n\n用例清单：\n" + json.dumps(list(cases)[:120], ensure_ascii=False, indent=2)[:16000]
    )
    text, reason = _call_llm(prompt)
    if text is None:
        return {"markdown": template, "llm_used": False, "degraded": True,
                "reason": reason or "模型不可用", "as_of": as_of}
    return {"markdown": text, "llm_used": True, "degraded": False, "reason": "",
            "as_of": as_of}


# ═══════════════════════════════════════════════════════════ 动作
def spec_check(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """`producer/spec_check`：报告阶段的前置门（不合格就不拆包）。"""
    params = params or {}
    brief = params.get("brief") if isinstance(params.get("brief"), dict) else {}
    endpoints = brief.get("endpoints") or params.get("endpoints") or []
    result = check_interface_spec(endpoints)
    result["ticket_id"] = brief.get("ticket_id") or params.get("ticket_id")
    if not result["ok"]:
        result["degraded"] = True
        result["reason"] = ("接口规范不合格，未拆包、未下发开发："
                            f"{result['counts']['error']} 个 error / "
                            f"{result['counts']['warn']} 个 warn")
    return result


def test_plan(params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """`auditor/test_plan`：按接口生成用例，可选调模型写测试报告。"""
    params = params or {}
    endpoints = params.get("endpoints") or []
    if isinstance(endpoints, dict):
        endpoints = endpoints.get("endpoints") or []
    cases = generate_test_cases(endpoints)
    plan = generate_test_plan(cases["cases"], endpoints,
                              llm=bool(params.get("llm")),
                              as_of=params.get("as_of"))
    out = {
        "ok": True,
        "count": cases["count"],
        "cases": cases["cases"],
        "by_scenario": cases["by_scenario"],
        "interfaces": cases["interfaces"],
        "markdown": plan["markdown"],
        "llm_used": plan["llm_used"],
        "degraded": plan["degraded"],
        "reason": plan["reason"],
        "spec_version": SPEC_VERSION,
    }
    return out


def run_action(action: str, params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None
               ) -> Dict[str, Any]:
    if action == "spec_check":
        return spec_check(params, ctx)
    if action == "test_plan":
        return test_plan(params, ctx)
    raise ValueError(f"game_server_spec 不支持的动作：{action}")


SPEC_ACTIONS = ("spec_check",)
TEST_ACTIONS = ("test_plan",)


def register_spec(runtime, capability: str = "producer") -> None:
    for action in SPEC_ACTIONS:
        runtime.add_handler(capability, action,
                            (lambda a: (lambda p, c: run_action(a, p, c)))(action))


def register_test_plan(runtime, capability: str = "auditor") -> None:
    for action in TEST_ACTIONS:
        runtime.add_handler(capability, action,
                            (lambda a: (lambda p, c: run_action(a, p, c)))(action))


# ═══════════════════════════════════════════════════════════ 规范文件 / CLI
def load_spec_file(path: str) -> Dict[str, Any]:
    """读独立接口规范文件（JSON）。两种结构：

    · `{"spec_version": ..., "conventions": {...}, "endpoints": [...]}`（推荐）
    · 顶层直接是 endpoints 数组
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, list):
        meta, endpoints = {}, data
    elif isinstance(data, dict):
        meta = {k: data.get(k) for k in
                ("spec_version", "service", "owner", "updated_at", "conventions")}
        endpoints = data.get("endpoints") or []
    else:
        raise ValueError("规范文件顶层必须是对象或数组")
    if not isinstance(endpoints, list):
        raise ValueError("规范文件 endpoints 必须是数组")
    return {"meta": meta, "endpoints": endpoints}


def check_spec_file(path: str, require_report_fields: bool = True) -> Dict[str, Any]:
    """校验一份规范文件，返回与 `spec_check` 同结构的回执（附文件元信息）。"""
    loaded = load_spec_file(path)
    result = check_interface_spec(loaded["endpoints"], require_report_fields)
    result["file"] = os.path.abspath(path)
    result["meta"] = loaded["meta"]
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m multi_agent.integrations.game_server_spec",
        description="校验接口规范文件（interface-spec/1.0），0 token 0 网络")
    ap.add_argument("file", help="规范文件路径（JSON）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 回执")
    ap.add_argument("--no-report-fields", action="store_true",
                    help="协议文件模式：不要求接口ID/调用方等报告字段")
    args = ap.parse_args(list(argv) if argv is not None else None)

    result = check_spec_file(args.file, require_report_fields=not args.no_report_fields)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        sc = result["counts"]
        print(result["file"])
        print(f"  规范版本：{result.get('spec_version')}  接口 {result['interfaces']} 个")
        print(f"  结果：{'通过' if result['ok'] else '不通过'} "
              f"（error {sc['error']} / warn {sc['warn']}）")
        for f in result["findings"]:
            print(f"  - [{f['severity']}] {f['code']} {f['message']} @ {f['where']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())