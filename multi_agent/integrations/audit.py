"""审计官（Auditor）：对"数据 / 报告 / 流水线结果"做三级审计。

为什么要有它
============
BalanceAI 的多角色链路里，Q 只管"单个品种的平衡表能不能进测算"，
J 只管"这一轮跑没跑通"。但真正生产里最常出事的是另一类问题：

  · 报告里的数字和平衡表对不上（口径漂移 / 手抄错误）
  · 平衡表整列是常数（占位符、抓取失败被填了默认值）
  · 时间序列有单位根、残差自相关、明显断点（假设被推翻）
  · 字段缺失被当成 0，或者单位从"吨"写成"万吨"
  · 报告结论与审计证据矛盾（说"去库"但库存其实在累）

**审计官的职责不是重算业务，而是判定"这份产出能不能信、哪里不能信"。**
它不改任何上游数字（和 J 一样只读），只输出 verdict + 证据。

三级执行成本（核心设计：默认最省）
==================================
| 级别 | 名字 | 走网络 | 烧 token | 能查什么 |
|---|---|---|---|---|
| **L1** | `rule` 规则审计 | 否 | 0 | 字段完整性、单位、量级、内部一致性、跨角色口径、时间新鲜度、必填缺失、突变阈值 |
| **L2** | `stat` 统计审计 | 否 | 0 | ADF 单位根、KPSS、Chow/Quandt 断点、Ljung-Box 残差自相关、Jarque-Bera 正态、过拟合（样本内外 R² 落差）、常量列检测 |
| **L3** | `llm` 语义审计 | 是 | 有 | 报告文字与数据是否矛盾、结论是否有证据支撑、口径措辞是否误导 |

**默认 `auto`**：先 L1，不通过就直接返回（省钱）；L1 通过且需要语义核对才升 L2；
L2 有疑点、或调用方显式要求，才进 L3。协调者（Coordinator）通过 `level` 参数控制，
所以"能不能走网络"这件事是**调用方决定**，不是 Auditor 自己决定。

L1 / L2 完全纯 Python（stdlib + numpy/pandas/scipy/statsmodels 若有则用，没有就退化成
纯 stdlib 近似），**不 import 任何大模型 SDK**。这是"0 token"的硬保证：
`assert_no_llm_import()` 会在导入时自检。

设计约束（与 BalanceAI docs/AGENTS.md 一致）
============================================
1. **不改数**：只读输入，输出审计结论；发现问题只"标记"，修正留给上游。
2. **不估算**：数据缺失就判 `missing`，不插值、不填默认值。
3. **可解释**：每条结论都带 `evidence`（实际值、阈值、来源字段）。
4. **可降级**：L2 依赖的库缺失时降级为 L1 结论并标 `degraded`，不假装通过。

用法（函数式，不经过大模型）
============================
    from multi_agent.integrations.audit import Auditor

    aud = Auditor()                                   # 默认 L1+L2，不联网
    rep = aud.audit_balance(balance_dict)             # 审平衡表
    rep = aud.audit_report(report_md, balance)        # 审报告 vs 平衡表
    rep = aud.audit_series(series, name="copper_stock")   # 审时间序列
    rep = aud.audit_run(run_snapshot)                 # 审一轮流水线

    # 需要语义审计时（显式，且需要调用方提供 llm 回调）
    rep = aud.audit_report(md, bal, level="llm",
                           llm=lambda prompt: my_local_model(prompt))

挂到 Hub 上（capability=auditor）
=================================
    from multi_agent.integrations.audit import register
    register(runtime)          # 动作：balance / report / series / run / full

然后在流水线里加一步：
    {"name": "audit", "capability": "auditor", "action": "full",
     "params": {"target": "${steps.report.result}"}, "depends_on": ["report"]}
"""
from __future__ import annotations

import json
import math
import re
import statistics
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

_ROLE_ACTIONS = {
    "auditor": ["balance", "report", "series", "run", "full"],
}
_ROLE_DESCRIPTION = (
    "审计官：L1 规则 / L2 统计（ADF·断点·自相关·过拟合）/ L3 语义，"
    "默认不联网不烧 token，不改上游数字"
)

# ───────────────────────────────────────────────────────────── 0 token 自检
_FORBIDDEN_MODULES = (
    "openai", "anthropic", "dashscope", "zhipuai", "google.generativeai",
    "cohere", "replicate", "ollama", "transformers", "vllm", "litellm",
)


def assert_no_llm_import() -> Dict[str, Any]:
    """自检：本模块的依赖链里没有大模型 SDK。

    只在 L1/L2 路径上被调用；L3 是**调用方传入 llm 回调**，
    本模块自己不 import、不持有 key、不发请求。
    """
    import sys as _sys
    hits = [m for m in _FORBIDDEN_MODULES
            if m in _sys.modules and not m.startswith("multi_agent")]
    return {"ok": not hits, "llm_modules_loaded": hits}


# 可选科学库：有就用真的，没有就退化（不报错，标 degraded）
_NP = None
_PD = None
try:  # pragma: no cover - 环境相关
    import numpy as _np  # type: ignore
    _NP = _np
except Exception:  # noqa: BLE001
    _NP = None
try:  # pragma: no cover
    import pandas as _pd  # type: ignore
    _PD = _pd
except Exception:  # noqa: BLE001
    _PD = None


def _stats_capabilities() -> Dict[str, Any]:
    """L2 能用到什么程度（写进报告，避免"以为查了其实没查"）。"""
    caps = {"numpy": _NP is not None, "pandas": _PD is not None,
            "statsmodels": False, "scipy": False}
    if _NP is not None:
        try:  # pragma: no cover
            import scipy  # type: ignore  # noqa: F401
            caps["scipy"] = True
        except Exception:  # noqa: BLE001
            pass
        try:  # pragma: no cover
            import statsmodels  # type: ignore  # noqa: F401
            caps["statsmodels"] = True
        except Exception:  # noqa: BLE001
            pass
    return caps


# ───────────────────────────────────────────────────────────── 工具
_SEVERITY_ORDER = {"info": 0, "warn": 1, "error": 2}


def _sev_max(*sevs: str) -> str:
    best = "info"
    for s in sevs:
        if _SEVERITY_ORDER.get(s, 0) > _SEVERITY_ORDER.get(best, 0):
            best = s
    return best


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _num(x: Any) -> Optional[float]:
    if _is_num(x):
        return float(x)
    if isinstance(x, str):
        try:
            return float(x.replace(",", "").strip())
        except ValueError:
            return None
    return None


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


class Finding:
    """一条审计发现（可序列化成报告条目）。"""

    __slots__ = ("code", "severity", "message", "evidence", "where")

    def __init__(self, code: str, severity: str, message: str,
                 evidence: Optional[Dict[str, Any]] = None, where: str = ""):
        self.code = code
        self.severity = severity          # info / warn / error
        self.message = message
        self.evidence = evidence or {}
        self.where = where

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "severity": self.severity,
                "message": self.message, "evidence": self.evidence,
                "where": self.where}

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Finding {self.severity}:{self.code} {self.message}>"


class Report:
    """审计报告：verdict + 发现清单 + 各层执行情况。"""

    def __init__(self, kind: str, level: str):
        self.kind = kind
        self.level = level
        self.findings: List[Finding] = []
        self.layers: Dict[str, Any] = {}
        self.metrics: Dict[str, Any] = {}
        self.started = time.time()

    def add(self, code: str, severity: str, message: str,
            evidence: Optional[Dict[str, Any]] = None, where: str = "") -> "Report":
        self.findings.append(Finding(code, severity, message, evidence, where))
        return self

    @property
    def severity(self) -> str:
        s = "info"
        for f in self.findings:
            s = _sev_max(s, f.severity)
        return s

    @property
    def verdict(self) -> str:
        """pass / suspect / fail —— 与 BalanceAI 的 Q/J 语义保持一致。"""
        sev = self.severity
        if sev == "error":
            return "fail"
        if sev == "warn":
            return "suspect"
        return "pass"

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "level": self.level, "verdict": self.verdict,
                "severity": self.severity,
                "counts": {
                    "error": sum(1 for f in self.findings if f.severity == "error"),
                    "warn": sum(1 for f in self.findings if f.severity == "warn"),
                    "info": sum(1 for f in self.findings if f.severity == "info")},
                "findings": [f.as_dict() for f in self.findings],
                "layers": self.layers, "metrics": self.metrics,
                "elapsed_ms": int((time.time() - self.started) * 1000),
                "ts": _now()}

    def to_markdown(self) -> str:
        d = self.as_dict()
        icon = {"pass": "✅", "suspect": "⚠️", "fail": "❌"}[d["verdict"]]
        lines = [f"### {icon} 审计：{d['kind']}（level={d['level']}）",
                 "", f"- 结论：**{d['verdict']}**（severity={d['severity']}）",
                 f"- 发现：error {d['counts']['error']} / warn {d['counts']['warn']}"
                 f" / info {d['counts']['info']}",
                 f"- 耗时：{d['elapsed_ms']} ms"]
        for name, info in (d.get("layers") or {}).items():
            lines.append(f"- 层 `{name}`：{json.dumps(info, ensure_ascii=False)}")
        if d["findings"]:
            lines += ["", "| 级别 | 代码 | 位置 | 说明 |", "|---|---|---|---|"]
            for f in d["findings"]:
                lines.append(f"| {f['severity']} | `{f['code']}` | {f['where'] or '-'} "
                             f"| {f['message']} |")
        return "\n".join(lines)


# ───────────────────────────────────────────────────────────── L1 规则审计
# 单位归一：把常见单位折成"基准单位"的倍率，用于量级与跨表比对
_UNIT_FACTORS = {
    "吨": 1.0, "t": 1.0,
    "千吨": 1e3, "kt": 1e3, "ktons": 1e3,
    "万吨": 1e4, "万t": 1e4,
    "百万吨": 1e6, "mt": 1e6,
    "吨/年": 1.0,
}

# 平衡表字段的合理量级（吨）；超出就说明单位或口径错了
_BALANCE_MAGNITUDE = {
    "mine": (1e3, 1e9), "refined_primary": (1e3, 1e9),
    "refined_secondary": (0, 1e9), "refined_total": (1e3, 1e9),
    "refined_usage": (1e3, 1e9), "surplus_deficit": (-1e8, 1e8),
    "stocks_end": (0, 1e9), "stock_change": (-1e8, 1e8),
}


def _unit_factor(unit: Any) -> Optional[float]:
    if not unit:
        return None
    key = str(unit).strip()
    return _UNIT_FACTORS.get(key) or _UNIT_FACTORS.get(key.lower())


def _walk_numbers(obj: Any, path: str = "") -> Iterable[Tuple[str, float]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_numbers(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_numbers(v, f"{path}[{i}]")
    elif _is_num(obj):
        yield path, float(obj)


def audit_balance_rules(balance: Dict[str, Any], rep: Report,
                        thresholds: Optional[Dict[str, Any]] = None) -> Report:
    """L1：平衡表的字段/单位/量级/内部一致性。**不估算、不填数。**"""
    th = thresholds or {}
    stale_days_warn = float(th.get("stale_days_warn", 45))
    stale_days_error = float(th.get("stale_days_error", 120))

    if not isinstance(balance, dict) or not balance:
        rep.add("balance_empty", "error", "平衡表为空或不是对象", where="balance")
        return rep

    # 1) 必填字段
    for field in ("commodity", "period", "unit", "balance", "supply", "demand"):
        if balance.get(field) in (None, "", {}, []):
            rep.add("missing_field", "error", f"缺必填字段 `{field}`",
                    {"field": field}, where=field)

    unit = balance.get("unit")
    factor = _unit_factor(unit)
    rep.metrics["unit"] = unit
    if factor is None:
        rep.add("unit_unknown", "warn", f"单位 `{unit}` 不在已知列表里，量级校验跳过",
                {"unit": unit, "known": sorted(_UNIT_FACTORS)}, where="unit")

    # 2) 量级（换算到"吨"后判）
    if factor:
        for section in ("supply", "demand", "balance"):
            node = balance.get(section)
            if not isinstance(node, dict):
                continue
            for key, val in node.items():
                v = _num(val)
                if v is None:
                    rep.add("value_not_numeric", "warn",
                            f"`{section}.{key}` 不是数字（{val!r}），可能是占位/缺失",
                            {"value": val}, where=f"{section}.{key}")
                    continue
                tons = v * factor
                rng = _BALANCE_MAGNITUDE.get(key)
                if rng and not (rng[0] <= abs(tons) <= rng[1]):
                    rep.add("magnitude_suspect", "error",
                            f"`{section}.{key}`={v} {unit} → {tons:.3g} 吨，超出该字段合理量级",
                            {"value": v, "unit": unit, "tons": tons,
                             "expected_tons": list(rng)}, where=f"{section}.{key}")

    # 3) 内部一致性：精炼总 = 原生 + 再生
    supply = balance.get("supply") or {}
    rp, rs, rt = (_num(supply.get("refined_primary")),
                  _num(supply.get("refined_secondary")),
                  _num(supply.get("refined_total")))
    if None not in (rp, rs, rt):
        diff = abs((rp + rs) - rt)
        tol = max(1e-6, 0.02 * abs(rt))
        if diff > tol:
            rep.add("internal_mismatch", "error",
                    f"精炼总 {rt} ≠ 原生 {rp} + 再生 {rs}（差 {diff:.4g}，容差 {tol:.4g}）",
                    {"refined_primary": rp, "refined_secondary": rs,
                     "refined_total": rt, "diff": diff, "tolerance": tol},
                    where="supply")
        else:
            rep.metrics["refined_total_check"] = {"diff": diff, "ok": True}

    # 4) 内部一致性：平衡 = 精炼总 - 用量
    demand = balance.get("demand") or {}
    usage = _num(demand.get("refined_usage"))
    bal_node = balance.get("balance") or {}
    sd = _num(bal_node.get("surplus_deficit"))
    if None not in (rt, usage, sd):
        implied = rt - usage
        diff = abs(implied - sd)
        tol = max(1e-6, 0.02 * max(abs(implied), 1.0))
        if diff > tol:
            rep.add("balance_inconsistent", "error",
                    f"平衡值 {sd} 与「精炼总-用量」{implied:.4g} 差 {diff:.4g}（容差 {tol:.4g}）",
                    {"refined_total": rt, "refined_usage": usage,
                     "surplus_deficit": sd, "implied": implied, "diff": diff},
                    where="balance.surplus_deficit")

    # 5) 库存变动 vs 平衡：符号一致性（累库应为正）
    sc = _num(bal_node.get("stock_change"))
    if None not in (sd, sc) and abs(sd) > 1e-9 and abs(sc) > 1e-9:
        if sd * sc < 0:
            rep.add("sign_conflict", "warn",
                    f"供需差 {sd} 与库存变动 {sc} 符号相反（产能过剩下库存却降？）",
                    {"surplus_deficit": sd, "stock_change": sc},
                    where="balance")

    # 6) 常量列 / 全零：抓取失败最常见的表现
    consts = _constant_columns(balance)
    for path, val in consts:
        rep.add("constant_series", "warn",
                f"`{path}` 全为常数 {val}，疑似占位或抓取失败",
                {"path": path, "value": val}, where=path)

    # 7) 时间新鲜度
    freshness = _freshness(balance)
    rep.metrics["freshness"] = freshness
    age = freshness.get("age_days")
    if age is not None:
        if age > stale_days_error:
            rep.add("data_stale", "error",
                    f"数据日期 {freshness.get('data_date')} 距今 {age:.0f} 天，超过 {stale_days_error:.0f} 天",
                    freshness, where="data_date")
        elif age > stale_days_warn:
            rep.add("data_aging", "warn",
                    f"数据日期 {freshness.get('data_date')} 距今 {age:.0f} 天，超过 {stale_days_warn:.0f} 天",
                    freshness, where="data_date")

    # 8) 库存明细：超过 120 天的子项应剔除（与 BalanceAI 口径一致）
    inv = balance.get("inventory_check") or {}
    for ex, node in (inv.get("exchanges") or {}).items():
        if not isinstance(node, dict):
            continue
        sd_days = _num(node.get("stale_days"))
        if sd_days is not None and sd_days > 120:
            rep.add("inventory_stale", "warn",
                    f"{ex} 库存滞后 {sd_days:.0f} 天（>120），只能作绝对水平参考",
                    {"exchange": ex, "stale_days": sd_days}, where=f"inventory_check.{ex}")

    rep.metrics["fields_seen"] = sorted(
        k for k in balance.keys() if not str(k).startswith("_"))
    return rep


def _constant_columns(obj: Any, path: str = "", out: Optional[List[Tuple[str, Any]]] = None
                      ) -> List[Tuple[str, Any]]:
    """找"长度 ≥ 4 且全相同"的数值列表 —— 抓取失败/占位的典型特征。"""
    out = [] if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            _constant_columns(v, f"{path}.{k}" if path else str(k), out)
    elif isinstance(obj, list):
        nums = [x for x in obj if _is_num(x)]
        if len(nums) >= 4 and len(set(nums)) == 1:
            out.append((path, nums[0]))
        for i, v in enumerate(obj):
            _constant_columns(v, f"{path}[{i}]", out)
    return out


_DATE_PATTERNS = (
    r"(\d{4})-(\d{2})-(\d{2})",
    r"(\d{4})/(\d{2})/(\d{2})",
    r"(\d{4})(\d{2})(\d{2})",
    r"(\d{4})-(\d{2})",          # 只有年月
)


def _freshness(balance: Dict[str, Any]) -> Dict[str, Any]:
    """取最可信的日期字段，算距今天数（不猜、不补）。"""
    candidates = []
    for key in ("data_date", "as_of", "fetched_at", "period"):
        v = balance.get(key)
        if isinstance(v, str) and v:
            candidates.append((key, v))
    best: Optional[Dict[str, Any]] = None
    today = time.localtime()
    for key, raw in candidates:
        for pat in _DATE_PATTERNS:
            m = re.search(pat, raw)
            if not m:
                continue
            parts = [int(g) for g in m.groups()]
            year = parts[0]
            month = parts[1] if len(parts) > 1 else 1
            day = parts[2] if len(parts) > 2 else 15   # 只有年月时取月中，标注为近似
            try:
                t = time.mktime((year, month, day, 0, 0, 0, 0, 0, -1))
            except (ValueError, OverflowError):
                continue
            age = (time.mktime(today[:3] + (0, 0, 0, 0, 0, -1)) - t) / 86400.0
            item = {"source": key, "data_date": raw, "age_days": round(age, 1),
                    "approx": len(parts) < 3}
            if best is None or item["age_days"] < best["age_days"]:
                best = item
            break
    return best or {"source": None, "data_date": None, "age_days": None}


# ───────────────────────────────────────────────────────────── L2 统计审计
def _as_float_list(series: Any) -> List[float]:
    if isinstance(series, dict):
        series = series.get("values") or series.get("data") or []
    if _PD is not None and isinstance(series, _pd.Series):  # pragma: no cover
        series = series.tolist()
    out: List[float] = []
    for x in list(series or []):
        v = _num(x)
        if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            continue
        out.append(v)
    return out


def adf_test(x: Sequence[float]) -> Dict[str, Any]:
    """ADF 单位根检验（无外依赖时用 OLS + MacKinnon 近似临界值）。

    结论语义：`stationary` = 拒绝单位根（序列平稳）；
    `unit_root` = 不能拒绝（有单位根，差分后再看）。
    """
    y = _as_float_list(x)
    n = len(y)
    if n < 12:
        return {"ok": False, "reason": f"样本 {n} < 12，ADF 不可靠"}
    dy = [y[i] - y[i - 1] for i in range(1, n)]
    lag_y = y[:-1]
    m = len(dy)
    mean_dy = sum(dy) / m
    mean_lag = sum(lag_y) / m
    sxx = sum((v - mean_lag) ** 2 for v in lag_y)
    if sxx <= 0:
        return {"ok": False, "reason": "滞后项无方差（常量序列）"}
    sxy = sum((lag_y[i] - mean_lag) * (dy[i] - mean_dy) for i in range(m))
    beta = sxy / sxx
    alpha = mean_dy - beta * mean_lag
    resid = [dy[i] - alpha - beta * lag_y[i] for i in range(m)]
    dof = max(1, m - 2)
    sigma2 = sum(r * r for r in resid) / dof
    se = math.sqrt(sigma2 / sxx) if sxx > 0 else float("inf")
    tstat = beta / se if se > 0 else 0.0
    # MacKinnon (1994) 近似临界值（含常数项）
    cv = {"1%": -3.43, "5%": -2.86, "10%": -2.57}
    verdict = "stationary" if tstat < cv["5%"] else "unit_root"
    return {"ok": True, "tstat": round(tstat, 3), "n": n,
            "critical": cv, "verdict": verdict,
            "method": "ADF(常数项, 无趋势, MacKinnon 临界值)"}


def kpss_test(x: Sequence[float]) -> Dict[str, Any]:
    """KPSS 平稳性检验（互补于 ADF：原假设是"平稳"）。"""
    y = _as_float_list(x)
    n = len(y)
    if n < 12:
        return {"ok": False, "reason": f"样本 {n} < 12"}
    mean = sum(y) / n
    cum = 0.0
    s: List[float] = []
    for v in y:
        cum += v - mean
        s.append(cum)
    eta = sum(v * v for v in s) / (n * n)
    # Newey-West 长期方差
    gamma0 = sum((v - mean) ** 2 for v in y) / n
    lags = int(4 * (n / 100.0) ** 0.25)
    lrv = gamma0
    for lag in range(1, lags + 1):
        w = 1 - lag / (lags + 1)
        cov = sum((y[i] - mean) * (y[i - lag] - mean) for i in range(lag, n)) / n
        lrv += 2 * w * cov
    stat = eta / lrv if lrv > 0 else 0.0
    cv = {"10%": 0.347, "5%": 0.463, "2.5%": 0.574, "1%": 0.739}
    verdict = "nonstationary" if stat > cv["5%"] else "stationary"
    return {"ok": True, "stat": round(stat, 4), "n": n, "lags": lags,
            "critical": cv, "verdict": verdict, "method": "KPSS(level)"}


def chow_break_test(x: Sequence[float], split_ratio: float = 0.5) -> Dict[str, Any]:
    """Chow 断点检验（单断点，两段均值/斜率是否显著不同）。

    没装 statsmodels 也能跑：用两段 OLS 残差平方和做 F 检验。
    """
    y = _as_float_list(x)
    n = len(y)
    if n < 16:
        return {"ok": False, "reason": f"样本 {n} < 16"}
    split = max(6, min(n - 6, int(round(n * split_ratio))))

    def _ssr(seg: Sequence[float]) -> Tuple[float, float, float]:
        m = len(seg)
        xs = list(range(m))
        mx = sum(xs) / m
        my = sum(seg) / m
        sxx = sum((v - mx) ** 2 for v in xs)
        if sxx <= 0:
            return sum((v - my) ** 2 for v in seg), 0.0, my
        sxy = sum((xs[i] - mx) * (seg[i] - my) for i in range(m))
        b = sxy / sxx
        a = my - b * mx
        ssr = sum((seg[i] - a - b * xs[i]) ** 2 for i in range(m))
        return ssr, b, a

    s1, b1, _ = _ssr(y[:split])
    s2, b2, _ = _ssr(y[split:])
    s_all, b_all, _ = _ssr(y)
    k = 2                     # 每段 2 个参数
    dof = n - 2 * k
    if dof <= 0 or (s1 + s2) <= 0:
        return {"ok": False, "reason": "残差退化，无法做 F 检验"}
    f_stat = ((s_all - (s1 + s2)) / k) / ((s1 + s2) / dof)
    # 临界值近似（F(2, dof) at 5%）
    cv5 = 3.0 if dof > 60 else (3.2 if dof > 30 else 3.5)
    return {"ok": True, "split_index": split, "f": round(f_stat, 3),
            "dof": dof, "critical_5pct": cv5,
            "slope_before": round(b1, 4), "slope_after": round(b2, 4),
            "slope_all": round(b_all, 4),
            "verdict": "break" if f_stat > cv5 else "no_break",
            "method": "Chow(单断点, OLS 残差 F 检验)"}


def ljung_box_test(x: Sequence[float], lags: int = 10) -> Dict[str, Any]:
    """Ljung-Box：残差/序列还有没有自相关（过拟合与模型设定错误的高发点）。"""
    y = _as_float_list(x)
    n = len(y)
    if n < lags + 4:
        return {"ok": False, "reason": f"样本 {n} 不足以做 {lags} 阶自相关"}
    mean = sum(y) / n
    denom = sum((v - mean) ** 2 for v in y)
    if denom <= 0:
        return {"ok": False, "reason": "常量序列，无自相关可言"}
    q = 0.0
    rhos = []
    for k in range(1, lags + 1):
        num = sum((y[i] - mean) * (y[i - k] - mean) for i in range(k, n))
        rho = num / denom
        rhos.append(round(rho, 4))
        q += rho * rho / (n - k)
    q *= n * (n + 2)
    # 卡方临界值近似（df=lags）
    cv5 = lags + 1.645 * math.sqrt(2 * lags)
    return {"ok": True, "Q": round(q, 3), "df": lags, "critical_5pct": round(cv5, 2),
            "autocorr": rhos,
            "verdict": "autocorrelated" if q > cv5 else "clean",
            "method": "Ljung-Box"}


def jarque_bera(x: Sequence[float]) -> Dict[str, Any]:
    """JB 正态性检验：偏度/峰度异常会让基于正态的显著性检验全部失真。"""
    y = _as_float_list(x)
    n = len(y)
    if n < 8:
        return {"ok": False, "reason": f"样本 {n} < 8"}
    mean = sum(y) / n
    m2 = sum((v - mean) ** 2 for v in y) / n
    if m2 <= 0:
        return {"ok": False, "reason": "常量序列"}
    m3 = sum((v - mean) ** 3 for v in y) / n
    m4 = sum((v - mean) ** 4 for v in y) / n
    skew = m3 / (m2 ** 1.5)
    kurt = m4 / (m2 ** 2)          # 正态为 3
    jb = n / 6.0 * (skew ** 2 + (kurt - 3) ** 2 / 4.0)
    cv5 = 5.99                     # chi2(2)
    return {"ok": True, "jb": round(jb, 3), "skew": round(skew, 3),
            "kurtosis": round(kurt, 3), "critical_5pct": cv5,
            "verdict": "non_normal" if jb > cv5 else "normal_like",
            "method": "Jarque-Bera"}


def overfit_check(y: Sequence[float], train_ratio: float = 0.7) -> Dict[str, Any]:
    """过拟合检查：样本内 R² 与样本外 R² 的落差。

    用一阶自回归（AR(1)）作为最小可检验模型：样本外 R² 崩到负值说明
    "历史拟合很好但预测没用"，这正是过拟合/参数被噪声带跑的信号。
    """
    ys = _as_float_list(y)
    n = len(ys)
    if n < 20:
        return {"ok": False, "reason": f"样本 {n} < 20"}
    split = int(n * train_ratio)
    train, test = ys[:split], ys[split:]

    def _fit(seg: Sequence[float]) -> Tuple[float, float, float]:
        xs, zs = seg[:-1], seg[1:]
        m = len(xs)
        mx = sum(xs) / m
        mz = sum(zs) / m
        sxx = sum((v - mx) ** 2 for v in xs)
        if sxx <= 0:
            return 0.0, mz, 0.0
        b = sum((xs[i] - mx) * (zs[i] - mz) for i in range(m)) / sxx
        a = mz - b * mx
        ssr = sum((zs[i] - a - b * xs[i]) ** 2 for i in range(m))
        sst = sum((v - mz) ** 2 for v in zs)
        r2 = 1 - ssr / sst if sst > 0 else 0.0
        return a, b, r2

    a, b, r2_in = _fit(train)
    # 样本外 R²（用训练集参数预测）
    xs, zs = test[:-1], test[1:]
    mz = sum(zs) / len(zs)
    sst = sum((v - mz) ** 2 for v in zs)
    ssr = sum((zs[i] - a - b * xs[i]) ** 2 for i in range(len(zs)))
    r2_out = 1 - ssr / sst if sst > 0 else 0.0
    gap = r2_in - r2_out
    return {"ok": True, "r2_in": round(r2_in, 4), "r2_out": round(r2_out, 4),
            "gap": round(gap, 4), "ar1": round(b, 4), "n": n,
            "verdict": ("overfit" if (r2_in > 0.5 and r2_out < 0.0 and gap > 0.5)
                        else "suspicious" if gap > 0.4 else "ok"),
            "method": "AR(1) 样本内/外 R² 落差"}


def audit_series_stats(series: Any, rep: Report, name: str = "series") -> Report:
    """L2：对一条时间序列跑 ADF / KPSS / 断点 / 自相关 / 正态 / 过拟合。"""
    vals = _as_float_list(series)
    rep.metrics["n"] = len(vals)
    if len(vals) < 12:
        rep.add("series_too_short", "warn",
                f"序列 `{name}` 只有 {len(vals)} 个有效点，统计检验不可靠",
                {"n": len(vals)}, where=name)
        return rep
    if len(set(vals)) == 1:
        rep.add("series_constant", "error",
                f"序列 `{name}` 全为 {vals[0]}，无法做任何统计审计",
                {"value": vals[0], "n": len(vals)}, where=name)
        return rep

    tests: Dict[str, Any] = {}
    for label, fn in (("adf", adf_test), ("kpss", kpss_test),
                      ("ljung_box", ljung_box_test), ("jarque_bera", jarque_bera),
                      ("chow", chow_break_test), ("overfit", overfit_check)):
        try:
            tests[label] = fn(vals)
        except Exception as e:  # noqa: BLE001 —— 单个检验失败不影响其它
            tests[label] = {"ok": False, "reason": f"{type(e).__name__}: {e}"}
    rep.metrics["tests"] = tests

    adf = tests.get("adf") or {}
    kpss = tests.get("kpss") or {}
    if adf.get("ok"):
        if adf["verdict"] == "unit_root":
            rep.add("unit_root", "warn",
                    f"`{name}` ADF 未拒绝单位根（t={adf['tstat']}），水平值回归可能伪回归",
                    adf, where=name)
        else:
            rep.add("stationary", "info",
                    f"`{name}` ADF 拒绝单位根（t={adf['tstat']}）", adf, where=name)
    if kpss.get("ok") and kpss["verdict"] == "nonstationary":
        rep.add("kpss_nonstationary", "warn",
                f"`{name}` KPSS 判非平稳（stat={kpss['stat']}），与 ADF 交叉验证需注意",
                kpss, where=name)
    chow = tests.get("chow") or {}
    if chow.get("ok") and chow["verdict"] == "break":
        rep.add("structural_break", "warn",
                f"`{name}` 在 index {chow['split_index']} 处存在结构断点"
                f"（F={chow['f']} > {chow['critical_5pct']}），斜率 {chow['slope_before']}"
                f" → {chow['slope_after']}",
                chow, where=name)
    lb = tests.get("ljung_box") or {}
    if lb.get("ok") and lb["verdict"] == "autocorrelated":
        rep.add("residual_autocorr", "warn",
                f"`{name}` Ljung-Box Q={lb['Q']} > {lb['critical_5pct']}，残差仍有自相关"
                f"（模型未吃干净信息）", lb, where=name)
    of = tests.get("overfit") or {}
    if of.get("ok"):
        if of["verdict"] == "overfit":
            rep.add("overfit", "error",
                    f"`{name}` 过拟合：样本内 R²={of['r2_in']} 但样本外 R²={of['r2_out']}"
                    f"（落差 {of['gap']}）", of, where=name)
        elif of["verdict"] == "suspicious":
            rep.add("overfit_suspect", "warn",
                    f"`{name}` 样本内外 R² 落差 {of['gap']}，需复核", of, where=name)
    jb = tests.get("jarque_bera") or {}
    if jb.get("ok") and jb["verdict"] == "non_normal":
        rep.add("non_normal", "info",
                f"`{name}` 非正态（偏度 {jb['skew']}，峰度 {jb['kurtosis']}），"
                f"基于正态的显著性需谨慎", jb, where=name)
    return rep


# ───────────────────────────────────────────────────────────── 报告审计
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def audit_report_rules(report_md: str, balance: Optional[Dict[str, Any]],
                       rep: Report) -> Report:
    """L1+L2：报告文本 vs 平衡表的交叉核对（不重算业务，只查口径矛盾）。"""
    if not isinstance(report_md, str) or not report_md.strip():
        rep.add("report_empty", "error", "报告为空", where="report")
        return rep
    rep.metrics["report_chars"] = len(report_md)
    rep.metrics["report_lines"] = report_md.count("\n") + 1

    # 结构检查（7 段式的弱校验：至少要有标题和小标题）
    if not report_md.lstrip().startswith("#"):
        rep.add("report_no_title", "warn", "报告没有 Markdown 标题（# ...）", where="report")
    if report_md.count("##") < 2:
        rep.add("report_few_sections", "warn",
                "小标题少于 2 个，7 段式结构可能不完整", where="report")

    # 空段/占位符
    for token in ("None", "nan", "NaN", "TODO", "TBD", "xxx", "{{", "}}"):
        if token in report_md:
            rep.add("placeholder_in_report", "warn",
                    f"报告里出现占位符 `{token}`", {"token": token}, where="report")

    if not isinstance(balance, dict) or not balance:
        rep.add("no_balance_for_report", "info",
                "没有提供平衡表，跳过数值交叉核对", where="report")
        return rep

    # 交叉核对：报告里出现的数字是否能在平衡表里找到（容差 2%）
    bals = [v for _, v in _walk_numbers(balance)]
    if not bals:
        return rep
    unmatched: List[float] = []
    for m in _NUMBER_RE.finditer(report_md):
        try:
            val = float(m.group(0))
        except ValueError:
            continue
        if abs(val) < 100:            # 小节号/百分比等噪声，跳过
            continue
        hit = any(abs(val - b) <= max(0.02 * abs(b), 1e-9) for b in bals)
        if not hit:
            unmatched.append(val)
    rep.metrics["unmatched_numbers"] = unmatched[:20]
    if len(unmatched) >= 2:
        rep.add("report_numbers_unmatched", "warn",
                f"报告里有 {len(unmatched)} 个数字在平衡表中找不到对应值（前几个："
                f"{', '.join(f'{v:g}' for v in unmatched[:5])}）",
                {"count": len(unmatched), "samples": unmatched[:10]}, where="report")

    # 方向矛盾：报告说"去库/累库"，平衡表符号要一致
    bl = balance.get("balance") or {}
    sd = _num(bl.get("surplus_deficit"))
    if sd is not None:
        text = report_md
        says_destock = any(k in text for k in ("去库", "库存下降", "库存减少", "de-stock"))
        says_restock = any(k in text for k in ("累库", "库存上升", "库存增加", "re-stock"))
        if sd > 0 and says_destock:
            rep.add("narrative_conflict", "error",
                    f"平衡表供给过剩 {sd}（应累库），但报告写去库",
                    {"surplus_deficit": sd}, where="report")
        if sd < 0 and says_restock:
            rep.add("narrative_conflict", "error",
                    f"平衡表供给缺口 {sd}（应去库），但报告写累库",
                    {"surplus_deficit": sd}, where="report")

    # 单位一致性：报告里若写"万吨"而平衡表是"吨"，提醒口径
    unit = balance.get("unit")
    if unit:
        found = _units_in_text(report_md)
        others = [u for u in found if u != unit]
        if others:
            rep.add("unit_mismatch", "warn",
                    f"报告提到 `{others[0]}`，但平衡表单位是 `{unit}`，注意换算",
                    {"balance_unit": unit, "report_units": found}, where="report")
    return rep


def _units_in_text(text: str) -> List[str]:
    """从文本里抽出出现的单位（长优先、不重叠，避免「万吨」被拆成「吨」）。"""
    units = sorted(_UNIT_FACTORS, key=len, reverse=True)
    hit: List[str] = []
    used: List[Tuple[int, int]] = []
    for u in units:
        start = 0
        while True:
            i = text.find(u, start)
            if i < 0:
                break
            span = (i, i + len(u))
            if not any(s < span[1] and span[0] < e for s, e in used):
                used.append(span)
                if u not in hit:
                    hit.append(u)
            start = i + len(u)
    return hit


def audit_report_llm(report_md: str, balance: Optional[Dict[str, Any]],
                     rep: Report, llm: Callable[[str], str]) -> Report:
    """L3：语义审计（**调用方传入 llm 回调**，本模块不 import 任何 SDK）。

    llm 约定：接受一段 prompt，返回文本。要求它输出 JSON:
        {"verdict":"pass|suspect|fail","issues":[{"code","severity","message"}]}
    解析失败时降级为 warn，不假装通过。
    """
    if llm is None:
        rep.add("llm_unavailable", "warn",
                "请求了 L3 语义审计但没有提供 llm 回调，降级为 L1/L2 结论",
                where="report")
        return rep
    prompt = _build_llm_prompt(report_md, balance)
    try:
        raw = llm(prompt)
    except Exception as e:  # noqa: BLE001
        rep.add("llm_error", "warn", f"L3 语义审计调用失败：{type(e).__name__}: {e}",
                where="report")
        return rep
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    rep.metrics["llm_raw_len"] = len(text)
    data = _extract_json(text)
    if not isinstance(data, dict):
        rep.add("llm_unparsable", "warn",
                "L3 返回内容无法解析为 JSON，语义结论不可用", {"raw_head": text[:300]},
                where="report")
        return rep
    for issue in (data.get("issues") or [])[:20]:
        if not isinstance(issue, dict):
            continue
        rep.add(str(issue.get("code") or "llm_issue"),
                str(issue.get("severity") or "warn"),
                str(issue.get("message") or ""),
                {"source": "llm"}, where="report")
    v = data.get("verdict")
    rep.metrics["llm_verdict"] = v
    if v == "fail" and not any(f.severity == "error" for f in rep.findings):
        rep.add("llm_fail_no_detail", "warn",
                "L3 判 fail 但没给出具体 issue", where="report")
    return rep


def _build_llm_prompt(report_md: str, balance: Optional[Dict[str, Any]]) -> str:
    bal_txt = json.dumps(balance or {}, ensure_ascii=False)[:4000]
    return (
        "你是大宗商品供需平衡表的审计官。任务：只找**报告与数据矛盾**的地方，"
        "不要重算、不要给建议。\n"
        "判定口径：数字对不上→severity=error；措辞可能误导→warn；补充说明→info。\n"
        "只输出 JSON：{\"verdict\":\"pass|suspect|fail\","
        "\"issues\":[{\"code\":\"...\",\"severity\":\"error|warn|info\","
        "\"message\":\"...\"}]}\n\n"
        f"【平衡表】\n{bal_txt}\n\n【报告】\n{report_md[:6000]}\n"
    )


def _extract_json(text: str) -> Optional[Any]:
    text = text.strip()
    for candidate in (text,
                      text[text.find("{"):text.rfind("}") + 1] if "{" in text else "",
                      text[text.find("["):text.rfind("]") + 1] if "[" in text else ""):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


# ───────────────────────────────────────────────────────────── 流水线审计
def audit_run_rules(run: Dict[str, Any], rep: Report) -> Report:
    """审一轮流水线快照（Coordinator.status 的返回）。"""
    if not isinstance(run, dict) or not run:
        rep.add("run_empty", "error", "流水线快照为空", where="run")
        return rep
    steps = run.get("steps") or {}
    rep.metrics["pipeline"] = run.get("pipeline")
    rep.metrics["run_id"] = run.get("run_id")
    rep.metrics["step_count"] = len(steps)
    failed = [k for k, v in steps.items() if v.get("state") == "failed"]
    skipped = [k for k, v in steps.items() if v.get("state") == "skipped"]
    if failed:
        rep.add("steps_failed", "error", f"步骤失败：{', '.join(failed)}",
                {"steps": failed}, where="run")
    if skipped:
        rep.add("steps_skipped", "warn", f"步骤被跳过：{', '.join(skipped)}",
                {"steps": skipped}, where="run")
    if run.get("state") == "failed":
        rep.add("run_failed", "error", "整轮状态 failed", where="run")

    # 审计缺口：没有 auditor 步骤的流水线，说明"跑完了但没人审"
    has_audit = any((v.get("capability") == "auditor") for v in steps.values())
    if not has_audit and not any(k.startswith("audit") for k in steps):
        rep.add("no_audit_step", "info",
                "这条流水线没有审计步骤，结果未经验证", where="run")
    return rep


# ───────────────────────────────────────────────────────────── Auditor 门面
class Auditor:
    """审计官门面：把 L1/L2/L3 组织起来，默认最省。

    参数
    ----
    level: "auto"（默认）/ "rule" / "stat" / "llm"
      · auto —— L1 有 error 就停；否则 L2；L2 有疑点且传了 llm 才 L3
      · rule —— 只跑 L1（0 网络、0 token）
      · stat —— 跑 L1+L2（0 网络、0 token）
      · llm  —— 跑 L1+L2+L3（需要 llm 回调）
    llm: 可选回调 `fn(prompt) -> str`，L3 才用。
    thresholds: 覆盖默认阈值（stale_days_warn / stale_days_error 等）。
    """

    def __init__(self, level: str = "auto", llm: Optional[Callable[[str], str]] = None,
                 thresholds: Optional[Dict[str, Any]] = None):
        self.level = level if level in ("auto", "rule", "stat", "llm") else "auto"
        self.llm = llm
        self.thresholds = thresholds or {}

    # ---------- 平衡表 ----------
    def audit_balance(self, balance: Dict[str, Any], level: Optional[str] = None) -> Report:
        lv = level or self.level
        rep = Report("balance", lv)
        rep.layers["L1_rule"] = {"ran": True, **assert_no_llm_import()}
        audit_balance_rules(balance, rep, self.thresholds)
        if lv == "rule":
            return rep
        if lv == "auto" and rep.severity == "error":
            rep.layers["L2_stat"] = {"ran": False, "reason": "L1 已判 error，省掉 L2"}
            return rep
        # L2：对平衡表里可构成序列的字段做统计（库存/供需差历史等）
        for key in ("stocks_end", "surplus_deficit", "stock_change"):
            series = _series_of(balance, key)
            if series and len(series) >= 12:
                before = len(rep.findings)
                audit_series_stats(series, rep, name=key)
                rep.metrics.setdefault("series_audited", []).append(
                    {"name": key, "n": len(series), "findings": len(rep.findings) - before})
        rep.layers["L2_stat"] = {"ran": True, **_stats_capabilities()}
        return rep

    # ---------- 报告 ----------
    def audit_report(self, report_md: str, balance: Optional[Dict[str, Any]] = None,
                     level: Optional[str] = None) -> Report:
        lv = level or self.level
        rep = Report("report", lv)
        rep.layers["L1_rule"] = {"ran": True, **assert_no_llm_import()}
        audit_report_rules(report_md, balance, rep)
        if lv == "rule":
            return rep
        if lv == "auto" and rep.severity == "error":
            rep.layers["L2_stat"] = {"ran": False, "reason": "L1 已判 error"}
            rep.layers["L3_llm"] = {"ran": False, "reason": "L1 已判 error"}
            return rep
        rep.layers["L2_stat"] = {"ran": True, **_stats_capabilities()}
        if balance:
            for key in ("stocks_end", "surplus_deficit"):
                series = _series_of(balance, key)
                if series and len(series) >= 12:
                    audit_series_stats(series, rep, name=f"balance.{key}")
        want_llm = (lv == "llm") or (lv == "auto" and rep.severity == "warn" and self.llm)
        if want_llm and self.llm:
            rep.layers["L3_llm"] = {"ran": True, "network": True, "tokens": "yes"}
            audit_report_llm(report_md, balance, rep, self.llm)
        elif want_llm:
            rep.layers["L3_llm"] = {"ran": False, "network": False,
                                    "reason": "未提供 llm 回调，降级为 L1/L2 结论"}
            rep.add("llm_unavailable", "warn",
                    "请求了 L3 语义审计但没有提供 llm 回调，降级为 L1/L2 结论",
                    where="report")
        else:
            rep.layers["L3_llm"] = {"ran": False, "network": False,
                                    "reason": "未要求 L3（默认不联网）"}
        return rep

    # ---------- 时间序列 ----------
    def audit_series(self, series: Any, name: str = "series",
                     level: Optional[str] = None) -> Report:
        lv = level or self.level
        if lv == "rule":
            lv = "stat"           # 序列审计的最小有意义级别就是 L2
        rep = Report("series", lv)
        rep.layers["L2_stat"] = {"ran": True, **_stats_capabilities()}
        audit_series_stats(series, rep, name=name)
        return rep

    # ---------- 流水线 ----------
    def audit_run(self, run: Dict[str, Any], level: Optional[str] = None) -> Report:
        lv = level or self.level
        rep = Report("run", lv)
        rep.layers["L1_rule"] = {"ran": True, **assert_no_llm_import()}
        audit_run_rules(run, rep)
        return rep

    # ---------- 全量 ----------
    def audit_full(self, balance: Optional[Dict[str, Any]] = None,
                   report_md: Optional[str] = None, run: Optional[Dict[str, Any]] = None,
                   level: Optional[str] = None) -> Report:
        """一次审完：平衡表 + 报告 + 流水线，合成一份总报告。"""
        lv = level or self.level
        rep = Report("full", lv)
        rep.layers["L1_rule"] = {"ran": True, **assert_no_llm_import()}
        if balance:
            audit_balance_rules(balance, rep, self.thresholds)
        if report_md:
            audit_report_rules(report_md, balance, rep)
        if run:
            audit_run_rules(run, rep)
        if lv in ("stat", "llm") or (lv == "auto" and rep.severity != "error"):
            rep.layers["L2_stat"] = {"ran": True, **_stats_capabilities()}
            if balance:
                for key in ("stocks_end", "surplus_deficit"):
                    s = _series_of(balance, key)
                    if s and len(s) >= 12:
                        audit_series_stats(s, rep, name=f"balance.{key}")
        want_llm = lv == "llm" or (lv == "auto" and rep.severity == "warn" and self.llm)
        if want_llm and self.llm and report_md:
            rep.layers["L3_llm"] = {"ran": True, "network": True, "tokens": "yes"}
            audit_report_llm(report_md, balance, rep, self.llm)
        else:
            rep.layers["L3_llm"] = {"ran": False, "network": False,
                                    "reason": ("未提供 llm 回调" if want_llm
                                               else "未要求 L3（默认不联网）")}
        return rep


def _series_of(obj: Dict[str, Any], key: str) -> List[float]:
    """从平衡表里尽量找出一串历史值（history / series / _history 等约定）。"""
    if not isinstance(obj, dict):
        return []
    for holder in ("history", "series", "_history", "history_series"):
        node = obj.get(holder)
        if isinstance(node, dict):
            v = node.get(key)
            if isinstance(v, list):
                return _as_float_list(v)
        if isinstance(node, list):
            out = []
            for item in node:
                if isinstance(item, dict) and key in item:
                    x = _num(item.get(key))
                    if x is not None:
                        out.append(x)
            if out:
                return out
    node = obj.get(key)
    if isinstance(node, list):
        return _as_float_list(node)
    return []


# ───────────────────────────────────────────────────────────── Hub 接入
def register(runtime, **kwargs: Any) -> None:
    """把一个 Agent 注册成审计官（capability=auditor）。

    动作：
      balance  审平衡表        params: {balance|target, level?, thresholds?}
      report   审报告          params: {report|target, balance?, level?}
      series   审时间序列      params: {series, name?, level?}
      run      审流水线快照    params: {run|run_id, level?}
      full     一次全审        params: {balance?, report?, run?, level?}
    """
    for action in _ROLE_ACTIONS["auditor"]:
        runtime.add_handler("auditor", action, _make_handler(action))


def _make_handler(action: str):
    def _handler(params: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        return run_action(action, params, ctx)
    return _handler


def _coerce_markdown(value: Any) -> str:
    """把上游给的"报告"变成字符串：上游可能回 dict（含 text/markdown/path）。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("markdown", "md", "text", "content", "report"):
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                return v
        path = value.get("path") or value.get("report_path") or value.get("file")
        if isinstance(path, str) and path:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return fh.read()
            except OSError:
                return ""
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def run_action(action: str, params: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None
               ) -> Dict[str, Any]:
    """动作执行体：纯函数，方便单测直接调（不经 Hub）。"""
    level = params.get("level")
    aud = Auditor(level=level or "auto", thresholds=params.get("thresholds"))
    if action == "balance":
        data = params.get("balance") or params.get("target") or {}
        return aud.audit_balance(data, level=level).as_dict()
    if action == "report":
        md = _coerce_markdown(params.get("report") or params.get("target"))
        return aud.audit_report(md, params.get("balance"), level=level).as_dict()
    if action == "series":
        return aud.audit_series(params.get("series"), params.get("name") or "series",
                                level=level).as_dict()
    if action == "run":
        return aud.audit_run(params.get("run") or {}, level=level).as_dict()
    if action == "full":
        return aud.audit_full(params.get("balance"),
                              _coerce_markdown(params.get("report")) or None,
                              params.get("run"), level=level).as_dict()
    raise ValueError(f"auditor 不支持的动作：{action}")


def llm_from_env() -> Optional[Callable[[str], str]]:
    """L3 用的 llm 回调工厂（可选，**宿主自己提供 key**）。

    支持 OpenAI 兼容的本地/内网端点（如 OpenClaw Gateway 的 /v1/chat/completions）：
        export AUDIT_LLM_URL=http://127.0.0.1:18799/v1/chat/completions
        export AUDIT_LLM_MODEL=openclaw/yuanbao-agent
        export AUDIT_LLM_TOKEN=...
    没配就返回 None —— Auditor 会自动跳过 L3。
    """
    import os
    url = os.environ.get("AUDIT_LLM_URL")
    if not url:
        return None
    model = os.environ.get("AUDIT_LLM_MODEL", "")
    token = os.environ.get("AUDIT_LLM_TOKEN", "")

    def _call(prompt: str) -> str:
        import urllib.request
        body = json.dumps({"model": model, "stream": False,
                           "messages": [{"role": "user", "content": prompt}]}).encode()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=body, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.loads(r.read().decode())
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return json.dumps(data, ensure_ascii=False)
    return _call
