"""看板取数角色（capability = `data`）：把 stock-dashboard 的结构指标接进底座。

为什么单独一个实现
------------------
`data_feed.py` 是通用默认实现，只从本地日线缓存算**价格派生**指标（动量/回撤/位置/
波动分位）。但"这条逻辑有没有硬伤"往往要看**结构**：

· 缠论有没有出现买卖点、中枢是不是在扩张
· DFA 的长程相关性有没有从趋势态转成随机态
· 期权是不是在给尾部定价（IV 分位、25D 偏度、PCR）
· 期货是升水还是贴水，期限结构是 contango 还是 backwardation

这些指标 stock-dashboard 那边已经按统一契约算好了（`data_hub.tech` 指标库 + 定时
producer），没必要在 Agent 侧重算一遍。本模块只把它们**读出来、打包成证据条**，零 token。

与 data_feed 的分工
-------------------
| 模块 | 动作 | 数据 |
| --- | --- | --- |
| `data_feed` | catalog / metrics / evidence / valuation / inventory / moneyflow | 价格派生、估值、资金流、库存 |
| **本模块** | tech_catalog / tech_structure / tech_option / tech_evidence / board_status | 缠论、DFA、RMT、期权、调度状态 |

动作名带 `tech_` / `board_` 前缀是刻意的：`data` 这个 capability 下已经注册了
`catalog` / `evidence`，重名会互相覆盖。

数据从哪来
----------
· `data_hub.tech` 的指标库（缠论 / DFA / RMT / 期权 的定稿指标点）
· 看板的 HTTP 管理接口 `/api/state`（模块调度与数据新鲜度）

本模块**不发网络请求取行情**，只读本地库 + 读本机 HTTP。取不到就说取不到，
不补零、不拿旧值顶替。

环境
----
    export DATAHUB_PATH=/path/to/datahub-multi-tier    # 必填，data_hub 包所在仓库根
    # stock-dashboard 用的是 DATA_HUB_PATH，两个变量名都认，配一个就行
    export STOCK_DASHBOARD_URL=http://127.0.0.1:8767   # 可选，看板管理接口
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

#: data_hub 包所在仓库根（datahub-multi-tier）。不写死默认值：不同机器的 checkout
#: 位置不一样，写死会让"读不到"伪装成"没数据"。
DEFAULT_HUB = (os.environ.get("DATAHUB_PATH")
               or os.environ.get("DATA_HUB_PATH")   # 与 stock-dashboard 的变量名对齐
               or "")

#: 看板管理接口（scheduler.py 起的 HTTP 服务）
DEFAULT_BOARD = os.environ.get("STOCK_DASHBOARD_URL") or "http://127.0.0.1:8767"

#: 域 → 人话（catalog 回给编排层，模型不必猜 key 前缀含义）
DOMAIN_NOTE: Dict[str, str] = {
    "chanlun": "缠论：笔/线段/中枢/买卖点结构",
    "dfa": "DFA 去趋势波动分析：长程相关性、趋势态 vs 随机态",
    "rmt": "随机矩阵理论：相关性矩阵去噪、共振度、风险集中度",
    "option": "期权定价：平值IV、25D偏度、PCR、VIX",
    "trend": "趋势：均线排列与动量结构",
    "futures": "期货：期限结构与基差",
    "macro": "宏观：增长/通胀/信用",
    "liquidity": "流动性：资金价格与量",
    "margin": "两融：杠杆资金",
}

#: 分位最少样本数（低于这个数不给分位，避免"1 个样本=100% 分位"的假精确）
MIN_PCT_SAMPLES = 60


def _tech(params: Dict[str, Any]):
    """拿 `data_hub.tech`；路径不对就明确报错，不静默返回空。"""
    root = str(params.get("datahub_path") or DEFAULT_HUB)
    if not root:
        raise RuntimeError("没有配置 data_hub 路径：设置环境变量 DATAHUB_PATH，"
                           "或传 params.datahub_path 指向 datahub-multi-tier 仓库根")
    if not os.path.isdir(root):
        raise RuntimeError(f"data_hub 仓库不存在：{root}")
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        import data_hub.tech as tech  # noqa: F401
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"data_hub.tech 不可用（{root}）：{type(e).__name__}: {e}") from e
    return tech


def _f(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _domain_of(metric_key: str) -> str:
    return metric_key.split(".", 1)[0] if "." in metric_key else ""


def _domain_list(domain: Optional[str]) -> List[str]:
    """把 `chanlun,dfa` 或 `["chanlun"]` 归一成列表。"""
    if not domain:
        return []
    if isinstance(domain, (list, tuple)):
        return [str(d).strip() for d in domain if str(d).strip()]
    return [d.strip() for d in str(domain).replace("|", ",").split(",") if d.strip()]


def tech_catalog(params: Dict[str, Any]) -> Dict[str, Any]:
    """能取什么：全部指标 key、按域分组、每个域的人话说明。

    编排层拿这个决定"要哪几项"，不用猜字段名。
    """
    try:
        tech = _tech(params)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    keys = list(tech.list_metrics())
    by_domain: Dict[str, List[str]] = {}
    for k in keys:
        by_domain.setdefault(_domain_of(k), []).append(k)
    domains = {d: {"note": DOMAIN_NOTE.get(d, ""), "n": len(ks), "metrics": sorted(ks)}
               for d, ks in sorted(by_domain.items())}
    return {"ok": True, "hub": str(params.get("datahub_path") or DEFAULT_HUB),
            "n_metrics": len(keys), "domains": domains,
            "securities": list(getattr(tech, "INDEX_ORDER", []) or []),
            "note": "指标由 data_hub.tech 统一契约定义，定时 producer 写入；"
                    "本模块只读定稿点，不发网络请求"}


def tech_structure(params: Dict[str, Any], domains: Optional[List[str]] = None
                   ) -> Dict[str, Any]:
    """单个标的的结构指标原值（缠论/DFA/RMT/期权…）。

    `domains` 不给就取全部域；只回**有记录**的指标，缺的不补零。
    """
    code = str(params.get("code") or params.get("security_id") or "").strip()
    if not code:
        return {"ok": False, "error": "tech_structure 需要 code（指数代码，如 000300.SH）"}
    try:
        tech = _tech(params)
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    want = _domain_list(params.get("domains") or params.get("domain") or domains)
    keys = [k for k in tech.list_metrics()
            if not want or _domain_of(k) in want]
    if not keys:
        return {"ok": False, "error": f"没有匹配域 {want} 的指标"}
    got = tech.read_latest(keys, [code])
    if not got:
        return {"ok": False, "code": code,
                "error": f"指标库里没有 {code} 的定稿点",
                "hint": "先跑对应 producer（chanlun_daily / dfa_daily / rmt_daily / option_daily）"}
    out: Dict[str, Any] = {}
    stamps: List[str] = []
    for (metric_key, sid), row in got.items():
        d = tech.get_def(metric_key)
        val = _f(row.get("value_num"))
        out[metric_key] = {
            "value": val if val is not None else row.get("value_text"),
            "name": getattr(d, "name", metric_key),
            "unit": getattr(d, "unit", ""),
            "direction": getattr(d, "direction", ""),
            "as_of": row.get("obs_date"),
            "quality": row.get("quality"),
        }
        if row.get("obs_date"):
            stamps.append(str(row["obs_date"]))
    return {"ok": True, "code": code, "name": tech.security_name(code),
            "as_of": max(stamps) if stamps else None,
            "n": len(out), "metrics": out,
            "metrics_source": "datahub:tech"}


def tech_option(params: Dict[str, Any]) -> Dict[str, Any]:
    """指数视角的期权快照：平值IV / 25D偏度 / PCR / VIX + 近 250/500 日分位。

    口径：一个指数可能有多套期权（沪深300 既有 IO 股指期权也有 510300 ETF 期权），
    只取「现货与指数同一只」的那套。IV/Skew 库内是小数，这里 ×100 转 %。
    分位样本不足 MIN_PCT_SAMPLES 就返回 None，不给假精确。
    """
    try:
        tech = _tech(params)
        snap = tech.opt_snapshot()
    except RuntimeError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"期权缓存不可用：{type(e).__name__}: {e}"}
    if not snap:
        return {"ok": False,
                "error": "期权缓存为空",
                "hint": "先跑 option_metrics_daily 把 IV/Skew/PCR 灌进 parquet 缓存"}
    code = str(params.get("code") or "").strip()
    if code:
        one = snap.get(code)
        if not one:
            return {"ok": False, "code": code,
                    "error": f"{code} 没有对应期权标的（registry.OPT_TARGET 未配或源未覆盖）",
                    "covered": sorted(snap)}
        return {"ok": True, "as_of": tech.opt_asof(snap), "code": code,
                "name": tech.security_name(code), **one}
    return {"ok": True, "as_of": tech.opt_asof(snap), "n": len(snap),
            "rows": {k: {"name": tech.security_name(k), **v} for k, v in snap.items()},
            "metrics_source": "datahub:option_metrics"}


def tech_evidence(params: Dict[str, Any]) -> Dict[str, Any]:
    """把结构指标打包成证据条，直接喂 adversary / equity_pick。

    格式与 `data_feed.evidence` 同构：`{名字: {指标: {value, signal, weight, ...}}}`。
    **signal 和 weight 留 None**：方向由指标契约里的 `direction` 给出，最终是多是空
    要辩论侧自己判断——取数层不给结论，这是分工，不是偷懒。
    """
    codes = params.get("codes") or ([params["code"]] if params.get("code") else [])
    codes = [str(c).strip() for c in codes if str(c).strip()]
    if not codes:
        return {"ok": False, "error": "tech_evidence 需要 code 或 codes"}
    names = params.get("names") if isinstance(params.get("names"), dict) else {}
    out: Dict[str, Any] = {}
    failed: Dict[str, str] = {}
    stamps: List[str] = []
    for code in codes:
        one = tech_structure(dict(params, code=code))
        if not one.get("ok"):
            failed[code] = str(one.get("error"))
            continue
        label = str(names.get(code) or one.get("name") or code)
        ev: Dict[str, Any] = {}
        for key, m in (one.get("metrics") or {}).items():
            ev[key] = {"value": m.get("value"), "unit": m.get("unit"),
                       "direction": m.get("direction"), "as_of": m.get("as_of"),
                       "signal": None, "weight": None}
        out[label] = ev
        if one.get("as_of"):
            stamps.append(str(one["as_of"]))
    if not out:
        return {"ok": False, "error": "没有任何标的取到结构指标", "failed": failed}
    return {"ok": True, "evidence": out, "n": len(out),
            "as_of": max(stamps) if stamps else None, "failed": failed,
            "note": "signal/weight 留空：方向见每项 direction，多空判断归辩论侧"}


def board_status(params: Dict[str, Any]) -> Dict[str, Any]:
    """看板自身的运行状态：各 task 上次跑的时间、成功与否、数据新鲜度。

    走看板管理接口 `GET /api/state`（无鉴权只读快照）。看板没起就明说没起，
    不假装拿到了状态。
    """
    url = str(params.get("board_url") or DEFAULT_BOARD).rstrip("/") + "/api/state"
    timeout = float(params.get("timeout") or 3.0)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"看板没起或地址不对：{url}（{e.reason}）",
                "hint": "在看板目录跑 python3 scheduler.py fg"}
    except (ValueError, OSError) as e:
        return {"ok": False, "error": f"读 /api/state 失败：{type(e).__name__}: {e}"}
    mods = payload.get("modules") or []
    only = params.get("module")
    if only:
        mods = [m for m in mods if m.get("key") == only]
    return {"ok": True, "url": url, "n_modules": len(mods), "modules": mods,
            "sections": payload.get("sections") or [],
            "note": "modules[].last_run / ok / enabled 反映调度实况；数据新鲜度看 last_obs"}


# ───────────────────────────────────────────── Hub 接入
def run_action(action: str, params: Dict[str, Any],
               ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if action == "tech_catalog":
        return tech_catalog(params or {})
    if action == "tech_structure":
        return tech_structure(params or {})
    if action == "tech_option":
        return tech_option(params or {})
    if action == "tech_evidence":
        return tech_evidence(params or {})
    if action == "board_status":
        return board_status(params or {})
    raise ValueError(f"data 不支持的动作：{action}（本模块提供 tech_* 与 board_status）")


def register(runtime) -> None:
    """把看板接成取数角色（零 token）。

    ```python
    from multi_agent.integrations.stock_dashboard import register
    register(runtime)
    # 动作：data/tech_catalog、data/tech_structure、data/tech_option、
    #       data/tech_evidence、data/board_status
    ```

    与 `data_feed.register` 可以同时注册：动作名不重叠，`data` 域下各管各的。
    """
    for action in ("tech_catalog", "tech_structure", "tech_option",
                   "tech_evidence", "board_status"):
        runtime.add_handler("data", action, lambda p, c, a=action: run_action(a, p, c))
