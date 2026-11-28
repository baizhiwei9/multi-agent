#!/usr/bin/env python3
"""Trade Agent（元 Agent）：自己先调事件官，再开多空辩论。

它解决的是上一轮暴露的问题：辩论 Agent 只吃“人工喂的 evidence.calendar”。
本脚本把顺序反过来——

    1. events__scan   事件侦察官：爬虫按关键词搜「这一天」的新闻 → 事件包 + 未来日历
    2. 行情           注入优先（--market-cmd / --market-json）；没注入才自己抓：
                      东财（带重试）→ 新浪（兜底）；单个指数失败只记 missing，不中断
    3. debate__run    4 席 × N 轮辩论，context.evidence.calendar 由第 1 步注入
    4. report__build  出 Markdown 报告
    5. 落盘           event_scan.json / trade_result.json / trade_report.md

用法（先起 panel：`bash examples/detective_panel/start_panel.sh`）：

    python trade_agent.py --date 2026-11-27 \
        --keywords "国常会,资本市场,A股,MSCI,PMI,韩国央行,工业企业利润" \
        --topic "国常会后 A 股是否开始构筑底部" --rounds 5

行情自己算好时（推荐，与“行情自己算”的口径一致）：

    python trade_agent.py --date 2026-11-27 --market-cmd "python my_market.py"
    python trade_agent.py --date 2026-11-27 --market-json /tmp/market.json

命令/文件的契约（缺字段就当缺，不补 0）：

    {"as_of": "2026-11-27",
     "spot":   {"上证指数": {"close": 3606.49, "chg_pct": 3.73, "amount_yi": null}},
     "history": {"上证指数": {"ma40": 3500.0, "dev_ma40_pct": 3.0, "above_ma40": true,
                             "ret5": 1.2, "ret20": 1.9, "low20": 3400.0}}}

事件官是**自动调用**的：不需要调用方手工拼 calendar，也不允许它跳过。
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from multi_agent.observability.flow import FlowTrace

HERE = Path(__file__).resolve().parent
PY = sys.executable
DEFAULT_HUB = "http://127.0.0.1:8787"
DEFAULT_TOKEN = "dsktok"


# ───────────────────────────────────────────── 0. 行情（注入优先，抓取兜底）
#: 指数表：(展示名, 东财 secid, 新浪 symbol)
_NAMES = [("上证指数", "1.000001", "sh000001"),
          ("深证成指", "0.399001", "sz399001"),
          ("创业板指", "0.399006", "sz399006"),
          ("科创50", "1.000688", "sh000688")]

_UA = {"User-Agent": "Mozilla/5.0"}


def _read_json_url(url: str, timeout: float = 30) -> Any:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _eastmoney_series(secid: str) -> List[tuple]:
    """东财日线 → [(date, close, amount)]。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=" + secid +
           "&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
           "&klt=101&fqt=1&beg=20250101&end=20500101")
    data = _read_json_url(url)
    rows = [r.split(",") for r in ((data.get("data") or {}).get("klines") or [])]
    return [(r[0], float(r[2]), float(r[6])) for r in rows if len(r) >= 8]


def _sina_series(symbol: str, n: int = 130) -> List[tuple]:
    """新浪日线 → [(date, close, None)]；东财断连时的兜底源。"""
    url = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_s=/CN_MarketDataService."
           f"getKLineData?symbol={symbol}&scale=240&ma=no&datalen={n}")
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8", "replace")
    m = re.search(r"=\((.*?)\);?\s*$", raw, re.S)
    rows = json.loads(m.group(1)) if m else []
    return [(r["day"], float(r["close"]), None) for r in rows]


def _series(name: str, secid: str, sina_sym: str,
            retries: int = 2) -> "tuple[Optional[List[tuple]], str]":
    """先东财（重试），失败换新浪；都失败返回 (None, 原因)，不抛异常。"""
    reason = ""
    for _ in range(max(1, retries)):
        try:
            rows = _eastmoney_series(secid)
            if rows:
                return rows, "eastmoney"
            reason = "东财返回空"
        except Exception as e:  # noqa: BLE001 - 单源失败不该中断整局
            reason = f"东财 {type(e).__name__}"
        time.sleep(1)
    try:
        rows = _sina_series(sina_sym)
        if rows:
            return rows, "sina"
        reason = f"{reason} / 新浪返回空"
    except Exception as e:  # noqa: BLE001
        reason = f"{reason} / 新浪 {type(e).__name__}"
    return None, reason


def _snapshot_from(series_by_name: Dict[str, List[tuple]]) -> Dict[str, Any]:
    spot: Dict[str, Any] = {}
    hist: Dict[str, Any] = {}
    as_of = ""
    for name, rows in series_by_name.items():
        closes = [r[1] for r in rows]
        if len(closes) < 2:
            continue
        last_date, close, amount = rows[-1]
        prev = closes[-2]
        as_of = max(as_of, str(last_date))
        ma40 = sum(closes[-40:]) / 40 if len(closes) >= 40 else sum(closes) / len(closes)
        spot[name] = {"close": round(close, 2),
                      "chg_pct": round((close / prev - 1) * 100, 2) if prev else None,
                      "amount_yi": round(amount / 1e8, 2) if amount is not None else None}
        hist[name] = {"ma40": round(ma40, 2),
                      "dev_ma40_pct": round((close / ma40 - 1) * 100, 2) if ma40 else None,
                      "above_ma40": bool(ma40 and close > ma40),
                      "ret5": round((close / closes[-6] - 1) * 100, 2) if len(closes) > 6 else None,
                      "ret20": round((close / closes[-21] - 1) * 100, 2) if len(closes) > 21 else None,
                      "low20": round(min(closes[-20:]), 2)}
    return {"as_of": as_of, "spot": spot, "history": hist}


def market_snapshot(retries: int = 2) -> Dict[str, Any]:
    """四个指数：能取几个取几个；取不到的写进 `missing`，不抛异常。

    以前这里任一指数失败就整局崩（第②步），事件、辩论、报告全不产出。
    """
    got: Dict[str, List[tuple]] = {}
    missing: Dict[str, str] = {}
    sources: Dict[str, str] = {}
    for name, secid, sina_sym in _NAMES:
        rows, source = _series(name, secid, sina_sym, retries=retries)
        if not rows:
            missing[name] = source
            continue
        got[name] = rows
        sources[name] = source
    out = _snapshot_from(got)
    out["missing"] = missing
    out["sources"] = sources
    out["source"] = "snapshot"
    return out


def _run_json_cmd(cmd: str) -> Dict[str, Any]:
    """跑一条命令，读 stdout 的 JSON（契约见文件头）。"""
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip()[-500:]
        raise RuntimeError(f"行情命令失败（rc={proc.returncode}）：{tail}")
    text = (proc.stdout or "").strip()
    if not text:
        raise RuntimeError("行情命令没有输出 JSON")
    try:
        return json.loads(text)
    except ValueError as e:
        raise RuntimeError(f"行情命令 stdout 不是 JSON：{e}")


def load_market(market_cmd: str = "", market_json: str = "") -> Dict[str, Any]:
    """行情来源：命令 > JSON 文件 > 自己抓。注入的来源只校验形状，不补数。"""
    if market_cmd:
        data, source = _run_json_cmd(market_cmd), "cmd"
    elif market_json:
        data, source = json.loads(Path(market_json).read_text(encoding="utf-8")), "json"
    else:
        return market_snapshot()
    if not isinstance(data, dict) or not isinstance(data.get("spot"), dict):
        raise RuntimeError("行情缺少 spot（契约：{as_of, spot{name:{close,chg_pct,amount_yi}}}）")
    out = dict(data)
    out.setdefault("history", {})
    out.setdefault("missing", {})
    out["source"] = source
    return out


# ───────────────────────────────────────────── 1. MCP stdio 客户端
class MCP:
    def __init__(self, hub: str = DEFAULT_HUB, token: str = DEFAULT_TOKEN) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(HERE)
        self.p = subprocess.Popen(
            [PY, "-u", "-m", "multi_agent.cli", "mcp-server",
             "--hub", hub, "--token", token, "--name", "trade-agent"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, cwd=str(HERE), env=env)
        self.q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        threading.Thread(target=self._pump_err, daemon=True).start()
        self.seq = 0

    def _pump(self) -> None:
        for line in self.p.stdout:
            line = line.strip()
            if line.startswith("{"):
                try:
                    self.q.put(json.loads(line))
                except ValueError:
                    pass

    def _pump_err(self) -> None:
        for line in self.p.stderr:
            sys.stderr.write("[mcp] " + line)

    def request(self, method: str, params: Dict[str, Any], timeout: float) -> Any:
        self.seq += 1
        mid = self.seq
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": mid,
                                       "method": method, "params": params}) + "\n")
        self.p.stdin.flush()
        end = time.time() + timeout
        while time.time() < end:
            try:
                msg = self.q.get(timeout=max(1, end - time.time()))
            except queue.Empty:
                break
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"])
                return msg["result"]
        raise TimeoutError(f"{method} 超时 {timeout}s")

    def tool(self, name: str, args: Dict[str, Any], timeout: float = 600) -> Dict[str, Any]:
        res = self.request("tools/call", {"name": name, "arguments": args}, timeout)
        task = json.loads(res["content"][0]["text"])
        if task.get("state") != "succeeded":
            raise RuntimeError(f"{name} 失败：{task.get('error') or task.get('state')}")
        return task.get("result") or {}

    def close(self) -> None:
        try:
            self.p.terminate()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


# ───────────────────────────────────────────── 2. 主流程
PARTICIPANTS = [
    {"capability": "bull_policy", "action": "argue", "role": "bull",
     "name": "正方·政策与基本面"},
    {"capability": "bull_flow", "action": "argue", "role": "bull",
     "name": "正方·资金与技术"},
    {"capability": "bear_valuation", "action": "argue", "role": "bear",
     "name": "反方·估值与盈利"},
    {"capability": "bear_memory", "action": "argue", "role": "bear",
     "name": "反方·历史与情绪反证"},
]


def _brief_text(scan: Dict[str, Any], market: Dict[str, Any]) -> str:
    """给 Supervisor 的送审件：事件 + 日历 + 行情，按 audit 限额截断。"""
    lines = ["## 当日事件（爬虫）", scan.get("digest") or "（无）", "", "## 行情"]
    spot = (market or {}).get("spot") or {}
    if not spot:
        lines.append("- （无行情：本次只用事件与日历）")
    for name, v in spot.items():
        chg = v.get("chg_pct")
        chg_txt = f"{chg:+.2f}%" if isinstance(chg, (int, float)) else "—"
        lines.append(f"- {name}: close={v.get('close')}，"
                     f"chg={chg_txt}，成交={v.get('amount_yi')}亿")
    lines.append("")
    lines.append("## 未来日历")
    for c in scan.get("calendar") or []:
        lines.append(f"- {c.get('date')} {c.get('event')}")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    m = MCP(args.hub, args.token)
    try:
        m.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                 "clientInfo": {"name": "trade-agent", "version": "1"}}, 30)
        tools = [t["name"] for t in m.request("tools/list", {}, 30)["tools"]]
        print("MCP 工具:", tools, flush=True)
        if "events__scan" not in tools:
            raise RuntimeError("panel 没有注册 events__scan；先重启 panel")

        # 1) 事件官：爬虫搜当天 + 事件官补未来日历
        print(f"① events__scan：{args.date} / {args.keywords}", flush=True)
        scan = m.tool("events__scan", {
            "date": args.date, "keywords": args.keywords,
            "horizon_days": args.horizon, "pages": args.pages,
            "llm": args.llm,
            "_timeout_s": 900,
        }, timeout=700)
        print(f"   当日事件 {len(scan.get('events') or [])} 条；"
              f"未来日历 {len(scan.get('scheduled') or [])} 条", flush=True)
        for e in (scan.get("scheduled") or []):
            print(f"   - {e['date']} {e['event']}", flush=True)

        # 2) 行情：注入优先；抓取失败只降级，不中断
        print(f"② 行情（{args.market_cmd or args.market_json or '自动抓取'}）…", flush=True)
        try:
            market = load_market(args.market_cmd, args.market_json)
        except Exception as e:  # noqa: BLE001 - 行情缺了也要出事件与辩论
            print(f"   行情不可用：{type(e).__name__}: {e}", flush=True)
            market = {"as_of": "", "spot": {}, "history": {},
                      "missing": {"全部": str(e)}, "source": "unavailable"}
        spot = market.get("spot") or {}
        if spot:
            spot_txt = ", ".join(
                f"{k} {v.get('close')}({(v.get('chg_pct') or 0):+.2f}%)"
                for k, v in spot.items())
            print(f"   as_of {market.get('as_of')}：{spot_txt}", flush=True)
        else:
            print("   行情为空：后续辩论只用事件与日历（不补 0、不编数）", flush=True)
        for name, why in (market.get("missing") or {}).items():
            print(f"   缺 {name}：{why}", flush=True)
        if market.get("sources"):
            print(f"   源：{market['sources']}", flush=True)

        # 3) 监理：把事件 + 行情压成送审件（零 token，超长截断）
        brief = ""
        sup: Dict[str, Any] = {}
        if "handoff__pack" in tools:
            print("③ handoff__pack：事件 + 行情 → 送审件…", flush=True)
            sup = m.tool("handoff__pack", {
                "payload": _brief_text(scan, market),
                "kind": "evidence_brief", "target": "debate",
                "project": "detective_panel", "_timeout_s": 120,
            }, timeout=180)
            brief = sup.get("payload") or ""
            print(f"   送审件 {len(brief)} 字；截断={sup.get('truncated')}；"
                  f"提示={sup.get('notes')}", flush=True)
        else:
            print("③ panel 未注册 handoff__pack，跳过送审", flush=True)

        # 4) 辩论：事件日历 + 送审件自动注入 evidence
        evidence = {
            "as_of": args.date,
            "market": market,
            "events_digest": scan.get("digest") or "",
            "supervisor_brief": brief,
            "calendar": scan.get("calendar") or [],
        }
        params = {
            "topic": args.topic,
            "max_rounds": args.rounds,
            "min_rounds": args.rounds,
            "participants": PARTICIPANTS,
            "judge": {"capability": "supervisor", "action": "verdict",
                      "name": "Supervisor（经理/终审）"},
            "judge_every_round": True,
            "judge_stop_on_converge": False,   # 3 轮都要复核，不因 APPROVE 提前停
            "stop_on_consensus": False,
            "max_tokens": 400000,
            "timeout_s": 600,
            "_timeout_s": 2400,
            "context": {
                "evidence": evidence,
                "models": {
                    "bull_policy": "detective_fast",
                    "bull_flow": "detective_fast",
                    "bear_valuation": "detective_deep",
                    "bear_memory": "detective_deep",
                },
            },
        }
        print(f"④ debate__run：{args.rounds} 轮 × 4 席（calendar 已注入 "
              f"{len(evidence['calendar'])} 条）…", flush=True)
        result = m.tool("debate__run", params, timeout=2500)
        print(f"   轮次 {result.get('rounds')} / 发言 {len(result.get('transcript') or [])} / "
              f"决策 {result.get('decision')}", flush=True)

        # 5) 报告
        print("⑤ report__build…", flush=True)
        rep = m.tool("report__build", {"topic": args.topic, "panel": result,
                                       "evidence": evidence}, timeout=180)
        md = rep.get("markdown") or ""

        out = Path(args.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "event_scan.json").write_text(
            json.dumps(scan, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "trade_result.json").write_text(
            json.dumps({"scan": scan, "market": market,
                        "handoff": sup, "debate": result},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "trade_report.md").write_text(md, encoding="utf-8")
        print(f"\n报告：{out / 'trade_report.md'}（{len(md)} 字）", flush=True)

        # 每次运行都出消息传递图：角色 + 消息 + 载荷摘要
        if not args.no_diagram:
            trace = FlowTrace.from_bundle(scan, market, result,
                                          report_chars=len(md), topic=args.topic,
                                          brief_chars=len(brief),
                                          brief_truncated=bool(sup.get("truncated")))
            trace.save_json(out / "flow_trace.json")
            try:
                trace.render(out / "flow.jpg")
                print(f"消息传递图：{out / 'flow.jpg'}", flush=True)
            except Exception as exc:  # noqa: BLE001 - 画图失败不影响报告
                print(f"消息传递图跳过：{exc}（pip install pillow / 设置 FLOW_FONT）",
                      flush=True)
        judge = (result.get("judge") or {})
        print("裁决：", (judge.get("text") or "")[:500].replace("\n", " "), flush=True)
        return 0
    finally:
        m.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Trade Agent：先事件官，再辩论")
    ap.add_argument("--date", required=True, help="事件日 YYYY-MM-DD")
    ap.add_argument("--keywords",
                    default="国常会,资本市场,A股,MSCI,PMI,美联储,韩国央行,统计局,四大行,工业企业利润",
                    help="逗号分隔的事件关键词")
    ap.add_argument("--topic", default="国常会后 A 股是否开始构筑底部")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--llm", default="auto", choices=("auto", "always", "never"),
                    help="大模型兜底：auto=爬虫/规则不足才用")
    ap.add_argument("--hub", default=DEFAULT_HUB)
    ap.add_argument("--token", default=DEFAULT_TOKEN)
    ap.add_argument("--out-dir", default=str(HERE / "trade_out"))
    ap.add_argument("--market-cmd", default="",
                    help="行情取数命令：stdout 回 JSON（契约见文件头），优先于 --market-json")
    ap.add_argument("--market-json", default="",
                    help="行情 JSON 文件路径；不传则自己抓（东财重试 → 新浪兜底）")
    ap.add_argument("--no-diagram", action="store_true",
                    help="不生成 flow.jpg（默认每次运行都生成）")
    args = ap.parse_args(argv)
    args.keywords = ",".join(x for x in args.keywords.split(",") if x.strip())
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
