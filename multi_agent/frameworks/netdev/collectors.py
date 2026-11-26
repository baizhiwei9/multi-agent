"""bug 采集官接口：与 `projects/news.py` 同一份形状，换的是"采什么"。

为什么和新闻采集长一样
--------------------
期货要的是"供应链硬事件"，游戏服务端要的是"线上炸了哪一处"，两边规则完全不同；
但**对流水线的形状是一样的**：

    collect(params) -> {"bugs": [...], "scanned": int, "deduped": int,
                        "truncated": int, "degraded": bool, ...}

所以流水线只认这个形状，bug 源可以换：服务器文本日志、JSONL 崩溃上报、回放断言失败、
你自己的告警系统，都只要继承 `BugCollector`。

三条硬规矩（与新闻采集一致）
--------------------------
1. **不编造**：读不到日志就 `degraded=True` + `reason`，`bugs` 留空，不许造 bug；
2. **不判根因**：采集官只说"哪里炸了、炸了几次"，根因留给定位与审计；
3. **去重可复现**：同一个指纹（错误码 + 接口 + 栈顶帧）只出一条，出现次数合并累加。

指纹为什么这么取
--------------
**错误码 + 接口** 是主键：同一个码打在同一接口上，基本就是同一处代码同一个错。
不用栈顶帧做主键，是因为同一次故障往往只有第一条日志带 traceback（后续是同一错误的重复
上报），按栈帧取指纹会把"带栈"和"不带栈"拆成两条。

错误码为空时才退到"栈顶帧（文件:行:函数）"，再没有就退到消息前 60 字——总要有东西保证
去重可复现。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

#: 日志头：`2026-11-26T03:12:41Z ERROR service=shop endpoint=/v1/shop/buy code=...`
_RE_HEADER = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s]*)\s+"
    r"(?P<level>DEBUG|INFO|WARN|WARNING|ERROR|CRITICAL|FATAL)\s+(?P<rest>.*)$")
_RE_KV = re.compile(r"(\w+)=(\"[^\"]*\"|'[^']*'|\S+)")
#: 栈帧：`  File "server/shop.py", line 9, in buy_item`
_RE_FRAME = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)')

_SEVERITY = {"DEBUG": "info", "INFO": "info", "WARN": "warn", "WARNING": "warn",
             "ERROR": "error", "CRITICAL": "critical", "FATAL": "critical"}
_WEIGHT = {"critical": 3, "error": 2, "warn": 1, "info": 0}


class BugCollector:
    """bug 采集官基类：`collect()` 是唯一必须实现的方法。"""

    name: str = "bug"
    description: str = ""
    source: str = ""

    def collect(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raise NotImplementedError

    # ---------------- 给实现用的工具 ----------------
    @staticmethod
    def bug_id(error_code: str, endpoint: str = "",
               frame: Optional[Dict[str, Any]] = None,
               message: str = "") -> str:
        """指纹：错误码 + 接口为主；没有错误码才用栈顶帧，再没有用消息前 60 字。

        不以栈帧做主键：同一次故障通常只有第一条日志带 traceback，
        按栈帧取指纹会把带栈/不带栈的重复上报拆成两条。
        """
        if error_code:
            raw = f"{error_code}|{endpoint}"
        else:
            f = frame or {}
            raw = "|".join([str(endpoint or ""),
                            f"{f.get('file') or ''}:{f.get('line') or ''}:{f.get('func') or ''}",
                            str(message or "")[:60]])
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def top_frame(trace: Iterable[str]) -> Optional[Dict[str, Any]]:
        """取栈顶帧（Python traceback 里最后一条 File 行就是出错处）。"""
        last = None
        for line in trace or []:
            m = _RE_FRAME.search(str(line))
            if m:
                last = {"file": m.group("file"), "line": int(m.group("line")),
                        "func": m.group("func")}
        return last

    @staticmethod
    def cap_bugs(bugs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按 `bug.max_items_per_collector` 截断，先按严重度×出现次数排序。"""
        from ...limits import load_limits

        limit = int(load_limits().bug.max_items_per_collector)
        ordered = sorted(bugs, key=lambda b: (-_WEIGHT.get(str(b.get("severity")), 0),
                                              -int(b.get("occurrences") or 1),
                                              str(b.get("bug_id") or "")))
        return ordered[:limit]

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "source": self.source,
                "description": self.description}


class NullBugCollector(BugCollector):
    """没有 bug 源时的明确占位：不假装成功，也不阻断流水线。"""

    name = "none"
    description = "未配置 bug 源（返回空列表 + degraded）"

    def collect(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {"bugs": [], "scanned": 0, "deduped": 0, "truncated": 0,
                "collector": self.name, "degraded": True,
                "reason": "本项目未配置 bug 采集官（bugs() 返回 None）",
                "by_severity": {}}


class StaticBugCollector(BugCollector):
    """测试/演示用：给定固定列表，仍走去重、排序与形状归一。"""

    name = "static"
    description = "固定列表（测试/演示）"

    def __init__(self, items: Optional[List[Dict[str, Any]]] = None) -> None:
        self.items = list(items or [])

    def collect(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return _aggregate(self.items, collector=self.name, source="static")


class LogBugCollector(BugCollector):
    """从服务器日志采 bug：文本日志（含 traceback）或 JSONL 崩溃上报。

    调用方二选一：
    · `path=` 直接给日志文件；
    · `fetch=` 注入 `fetch(hours) -> str`（远端日志拉取、对象存储取件都行）。

    文本格式约定（没有就返回 degraded，不猜）：

        2026-11-26T03:12:41Z ERROR service=shop endpoint=/v1/shop/buy code=SHOP_TX_FAILED msg="..."
        Traceback (most recent call last):
          File "server/shop.py", line 9, in buy_item
        TypeError: 'NoneType' object is not subscriptable
    """

    name = "server-log"
    description = "服务器日志（文本 traceback / JSONL 崩溃上报）→ bug 列表"
    source = "log"

    def __init__(self, path: Optional[str] = None,
                 fetch: Optional[Callable[[float], str]] = None) -> None:
        self.path = path
        self._fetch = fetch

    def collect(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        p = params or {}
        path = p.get("log_path") or self.path
        text = ""
        if self._fetch is not None:
            try:
                text = str(self._fetch(float(p.get("hours") or 24)) or "")
            except Exception as e:  # noqa: BLE001 - 取数失败就走 degraded，不炸流水线
                return {"bugs": [], "scanned": 0, "deduped": 0, "truncated": 0,
                        "collector": self.name, "degraded": True,
                        "reason": f"取日志失败：{type(e).__name__}: {e}", "by_severity": {}}
        elif path:
            if not os.path.isfile(str(path)):
                return {"bugs": [], "scanned": 0, "deduped": 0, "truncated": 0,
                        "collector": self.name, "degraded": True,
                        "reason": f"日志文件不存在：{path}（不编造 bug）", "by_severity": {}}
            try:
                with open(str(path), "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError as e:
                return {"bugs": [], "scanned": 0, "deduped": 0, "truncated": 0,
                        "collector": self.name, "degraded": True,
                        "reason": f"读日志失败：{e}", "by_severity": {}}
        else:
            return {"bugs": [], "scanned": 0, "deduped": 0, "truncated": 0,
                    "collector": self.name, "degraded": True,
                    "reason": "未提供 log_path 或 fetch（不编造 bug）", "by_severity": {}}

        raw = self.parse(text)
        out = _aggregate(raw, collector=self.name, source=str(path or "fetch"))
        out["bytes"] = len(text.encode("utf-8"))
        return out

    # ---------------- 解析 ----------------
    def parse(self, text: str) -> List[Dict[str, Any]]:
        """把日志文本拆成"原始事件"列表（还没去重）。"""
        lines = (text or "").splitlines()
        if _looks_like_jsonl(lines):
            return [e for e in (_parse_json_line(x) for x in lines if x.strip()) if e]
        return _parse_text_lines(lines)


def _looks_like_jsonl(lines: List[str]) -> bool:
    for line in lines[:5]:
        s = line.strip()
        if not s:
            continue
        if s.startswith("{"):
            try:
                data = json.loads(s)
            except ValueError:
                return False
            return isinstance(data, dict) and any(
                k in data for k in ("level", "severity", "error_code", "code", "msg", "message"))
    return False


def _parse_json_line(line: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    level = str(data.get("level") or data.get("severity") or "error").upper()
    trace = data.get("traceback") or data.get("trace") or []
    if isinstance(trace, str):
        trace = trace.splitlines()
    return {
        "level": level,
        "severity": _SEVERITY.get(level, "error"),
        "ts": data.get("ts") or data.get("time") or data.get("timestamp") or "",
        "service": str(data.get("service") or data.get("app") or ""),
        "endpoint": str(data.get("endpoint") or data.get("path") or ""),
        "error_code": str(data.get("error_code") or data.get("code") or ""),
        "message": str(data.get("message") or data.get("msg") or ""),
        "trace": [str(x) for x in (trace or [])],
    }


def _parse_text_lines(lines: List[str]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None

    def flush() -> None:
        nonlocal cur
        if cur is not None:
            events.append(cur)
            cur = None

    for line in lines:
        m = _RE_HEADER.match(line)
        if m:
            flush()
            rest = m.group("rest") or ""
            kv = {k.lower(): v.strip("\"'") for k, v in _RE_KV.findall(rest)}
            level = m.group("level").upper()
            cur = {
                "level": level,
                "severity": _SEVERITY.get(level, "error"),
                "ts": m.group("ts"),
                "service": kv.get("service", ""),
                "endpoint": kv.get("endpoint") or kv.get("path") or "",
                "error_code": kv.get("code") or kv.get("error_code") or "",
                "message": kv.get("msg") or kv.get("message") or rest,
                "trace": [],
            }
            continue
        if cur is None:
            continue
        stripped = line.strip()
        if not stripped:
            if cur["trace"]:
                flush()
            continue
        cur["trace"].append(line)
    flush()
    return events


def _aggregate(raw: Iterable[Dict[str, Any]], collector: str,
               source: str = "") -> Dict[str, Any]:
    """原始事件 → 去重后的 bug 列表（同指纹合并出现次数）。

    `INFO` 不是 bug：只计入 `ignored`，不进 `bugs`——采集官不该把正常请求当日志噪音上报。
    """
    all_events = [e for e in (raw or []) if isinstance(e, dict)]
    ignored = sum(1 for e in all_events if str(e.get("severity")) == "info")
    events = [e for e in all_events if str(e.get("severity")) != "info"]
    by_id: Dict[str, Dict[str, Any]] = {}
    for e in events:
        trace = [str(x) for x in (e.get("trace") or [])]
        frame = BugCollector.top_frame(trace)
        code = str(e.get("error_code") or "")
        endpoint = str(e.get("endpoint") or "")
        bug_id = str(e.get("bug_id") or BugCollector.bug_id(
            code, endpoint, frame, str(e.get("message") or "")))
        if bug_id in by_id:
            cur = by_id[bug_id]
            cur["occurrences"] = int(cur.get("occurrences") or 1) + 1
            cur["last_seen"] = str(e.get("ts") or cur.get("last_seen") or "")
            if len(trace) > len(cur.get("trace") or []):
                cur["trace"] = trace
                cur["frame"] = frame
            continue
        by_id[bug_id] = {
            "bug_id": bug_id,
            "severity": str(e.get("severity") or "error"),
            "service": str(e.get("service") or ""),
            "endpoint": endpoint,
            "error_code": code,
            "message": str(e.get("message") or ""),
            "trace": trace,
            "frame": frame,
            "occurrences": 1,
            "first_seen": str(e.get("ts") or ""),
            "last_seen": str(e.get("ts") or ""),
            "source": source,
            "hint_files": sorted({frame["file"]}) if frame and frame.get("file") else [],
        }

    bugs = list(by_id.values())
    kept = BugCollector.cap_bugs(bugs)
    by_sev: Dict[str, int] = {}
    for b in kept:
        by_sev[b["severity"]] = by_sev.get(b["severity"], 0) + 1
    return {"bugs": kept, "scanned": len(events),
            "deduped": max(0, len(events) - len(bugs)),
            "truncated": max(0, len(bugs) - len(kept)),
            "ignored": ignored,
            "collector": collector, "degraded": False, "reason": "",
            "source": source, "by_severity": by_sev}


def as_json(result: Dict[str, Any]) -> str:
    return json.dumps(result, ensure_ascii=False)


def summarize(bugs: Sequence[Dict[str, Any]]) -> str:  # noqa: F821 - 兼容旧 Python 注解
    """给报告用的一行摘要：`error 3 / warn 2，最高 critical`。"""
    items = list(bugs or [])
    if not items:
        return "无 bug"
    counts: Dict[str, int] = {}
    for b in items:
        counts[str(b.get("severity"))] = counts.get(str(b.get("severity")), 0) + 1
    top = max(items, key=lambda b: _WEIGHT.get(str(b.get("severity")), 0))
    return (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) +
            f"，最高 {top.get('severity')}（{top.get('error_code') or top.get('bug_id')}）")