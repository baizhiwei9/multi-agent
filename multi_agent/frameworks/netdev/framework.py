"""互联网开发采集框架：**关键词扫日志 → 命中行 → bug（带 group / target）**。

和 `LogBugCollector` 的分工
-------------------------
`LogBugCollector` 认结构化日志（`level=... endpoint=... code=...` + traceback 块），
好处是字段齐、能直接定位；坏处是格式一变就得改解析器。

`BugWatchFramework` 反过来：**只认关键词**。日志长什么样都行，加了关键词就能采到，
命中行连同后几行（traceback）一起往上送。用户要的"加关键词、过滤整行、往上传"
就是这个。命中行的 `target` 说明该交给谁（`dev/locate` 还是 `server_risk/capacity`）。

两种采集器可以并存：格式规范的服务用 `LogBugCollector`，杂牌日志用这个框架。
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Dict, List, Optional

from ..keywords import KeywordSet, judge_via_llm
from .collectors import BugCollector

_RE_CODE = re.compile(r"\bcode=([A-Za-z0-9_.\-]+)")
_RE_ENDPOINT = re.compile(r"\b(?:endpoint|path)=([^\s,;\"]+)")
_RE_SERVICE = re.compile(r"\bservice=([A-Za-z0-9_.\-]+)")


class BugWatchFramework(BugCollector):
    """关键词驱动的 bug 采集官。

    · `keyword_set`：关键词表；缺省按名字去 `config/keywords/<name>.json` 找；
    · `path` / `fetch`：日志来源（二选一）；
    · `judge`：`mode="llm"` 的复核函数，缺省走 `llm/chat`；
    · `context_lines`：命中行后面带几行（traceback 一般 5-8 行够）。
    """

    name = "bug-watch"
    description = "关键词扫日志：命中行 + 上下文 → bug（带 group/target）"
    source = "keywords"

    def __init__(self, keyword_set: Optional[KeywordSet] = None,
                 path: Optional[str] = None,
                 fetch: Optional[Callable[[float], str]] = None,
                 judge: Optional[Callable[[Dict[str, Any]], bool]] = None,
                 keyword_name: str = "netdev",
                 context_lines: int = 6) -> None:
        self.keywords = keyword_set if keyword_set is not None \
            else KeywordSet.load(keyword_name)
        self.path = path
        self._fetch = fetch
        self._judge = judge
        self.context_lines = max(1, int(context_lines))

    def judge(self) -> Optional[Callable[[Dict[str, Any]], bool]]:
        if self._judge is None:
            self._judge = judge_via_llm("你在筛线上服务日志。")
        return self._judge

    def collect(self, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        p = params or {}
        empty = {"bugs": [], "scanned": 0, "matched": 0, "by_group": {},
                 "collector": self.name, "degraded": True}
        if self.keywords is None:
            return {**empty, "reason": "没找到关键词表（config/keywords/netdev.json）"}

        text = ""
        path = p.get("log_path") or self.path
        if self._fetch is not None:
            try:
                text = str(self._fetch(float(p.get("hours") or 24)) or "")
            except Exception as e:  # noqa: BLE001 - 取数失败走 degraded
                return {**empty, "reason": f"取日志失败：{type(e).__name__}: {e}"}
        elif path:
            if not os.path.isfile(str(path)):
                return {**empty, "reason": f"日志文件不存在：{path}（不编造 bug）"}
            try:
                with open(str(path), "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError as e:
                return {**empty, "reason": f"读日志失败：{e}"}
        else:
            return {**empty, "reason": "未提供 log_path 或 fetch（不编造 bug）"}

        lines = text.splitlines()
        scan = self.keywords.scan_lines(lines, judge=self.judge())

        by_id: Dict[str, Dict[str, Any]] = {}
        for hit in scan["hits"]:
            start = max(0, int(hit["line_no"]) - 1)
            window = lines[start:start + self.context_lines]
            frame = BugCollector.top_frame(window)
            code = (_RE_CODE.search(hit["line"]) or [None, ""])[1] if _RE_CODE.search(hit["line"]) else ""
            endpoint = (_RE_ENDPOINT.search(hit["line"]) or [None, ""])[1] if _RE_ENDPOINT.search(hit["line"]) else ""
            service = (_RE_SERVICE.search(hit["line"]) or [None, ""])[1] if _RE_SERVICE.search(hit["line"]) else ""
            key = str(BugCollector.bug_id(code or hit["group"], endpoint, frame, hit["line"]))
            if key in by_id:
                by_id[key]["occurrences"] = int(by_id[key].get("occurrences") or 1) + 1
                continue
            by_id[key] = {
                "bug_id": key,
                "severity": hit["level"],
                "group": hit["group"],
                "keyword": hit["keyword"],
                "target": dict(hit.get("target") or {}),
                "service": service,
                "endpoint": endpoint,
                "error_code": code or hit["group"],
                "message": hit["line"][:300],
                "line": hit["line"],
                "line_no": hit["line_no"],
                "trace": window,
                "frame": frame,
                "occurrences": 1,
                "first_seen": "", "last_seen": "",
                "source": str(path or "fetch"),
                "hint_files": sorted({frame["file"]}) if frame and frame.get("file") else [],
            }

        bugs = list(by_id.values())
        kept = BugCollector.cap_bugs(bugs)
        by_group: Dict[str, int] = {}
        for b in kept:
            by_group[b["group"]] = by_group.get(b["group"], 0) + 1
        return {"bugs": kept, "scanned": scan["lines"], "matched": len(bugs),
                "by_group": by_group, "truncated": max(0, len(bugs) - len(kept)),
                "collector": self.name, "degraded": False, "reason": "",
                "source": str(path or "fetch"), "keyword_set": self.keywords.name,
                "lines_scanned": scan["lines"], "hits": scan["hits"][:len(kept)]}


class BugWatchFrameworkError(BugWatchFramework):
    """示例变体：只关心错误行（错误/崩溃两组），命中即交开发。"""

    def __init__(self, path: Optional[str] = None, **kwargs: Any) -> None:
        from .examples import ERROR_WATCH

        super().__init__(keyword_set=ERROR_WATCH, path=path, **kwargs)