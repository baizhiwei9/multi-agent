"""采集框架的共同底座：**加一行关键词，就多一条触发**。

新闻采集和 bug 采集要的是同一件事：在一大堆文本里，按关键词把"要紧的那几行"挑出来，
带上"命中什么、属于哪一组、该交给谁"，然后往上送。差别只在文本长什么样：

    新闻：一段段标题/摘要（一行一条）
    日志：一行一条，但可能跟着 traceback 好几行

所以引擎只管"按关键词挑行、分组、给出触发目标"，怎么分组、命中后调谁，由业务框架
（`frameworks/news`、`frameworks/netdev`）在配置里写。

两条判定路径（`mode`）
------------------
| mode | 怎么判 | 成本 |
|---|---|---|
| `program`（默认） | 关键词/正则匹配，命中即算 | 0 token |
| `llm` | 先按关键词挑出候选，再让 `llm/chat` 判"这条算不算" | 烧 token，可关 |

`llm` 模式下没有注入判定函数就自动降级为程序命中，并标 `judged=False`——
"想用模型但端点没配"不能变成"什么都没采到"。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

#: 单次扫描最多留几条命中（防止一个刷屏的日志把下游淹没）
DEFAULT_MAX_HITS = 50
_LEVELS = ("info", "warn", "error", "critical")
_MODES = ("program", "llm")


@dataclass
class KeywordGroup:
    """一组关键词：命中后带什么级别、交给哪个 capability/action。"""

    group: str
    keywords: List[str] = field(default_factory=list)
    level: str = "warn"
    target: Dict[str, str] = field(default_factory=dict)
    mode: str = "program"
    description: str = ""
    regex: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {"group": self.group, "keywords": list(self.keywords),
                "level": self.level, "target": dict(self.target),
                "mode": self.mode, "description": self.description,
                "regex": self.regex}


class KeywordSet:
    """一套关键词表：从 `config/keywords/*.json` 读，也可以在代码里直接给。"""

    def __init__(self, name: str, groups: Iterable[KeywordGroup],
                 max_hits: int = DEFAULT_MAX_HITS) -> None:
        self.name = name
        self.groups = [g for g in groups if g.keywords]
        self.max_hits = int(max_hits or DEFAULT_MAX_HITS)
        self._compiled: Optional[List[Tuple[KeywordGroup, Any]]] = None
        self.source = "inline"

    # ---------------- 构造 ----------------
    @classmethod
    def from_dict(cls, data: Dict[str, Any], name: Optional[str] = None) -> "KeywordSet":
        raw_groups = data.get("groups") or []
        groups: List[KeywordGroup] = []
        for item in raw_groups:
            if not isinstance(item, dict):
                continue
            level = str(item.get("level") or "warn").lower()
            mode = str(item.get("mode") or "program").lower()
            groups.append(KeywordGroup(
                group=str(item.get("group") or "default"),
                keywords=[str(k) for k in (item.get("keywords") or []) if str(k)],
                level=level if level in _LEVELS else "warn",
                target={str(k): str(v) for k, v in (item.get("target") or {}).items()},
                mode=mode if mode in _MODES else "program",
                description=str(item.get("description") or ""),
                regex=bool(item.get("regex")),
            ))
        out = cls(name or str(data.get("name") or "keywords"), groups,
                  max_hits=int(data.get("max_hits") or DEFAULT_MAX_HITS))
        out.source = str(data.get("source") or "inline")
        return out

    @classmethod
    def from_file(cls, path: str) -> "KeywordSet":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        out = cls.from_dict(data)
        out.source = path
        return out

    @classmethod
    def load(cls, name: str, *,
             search: Optional[Iterable[str]] = None) -> Optional["KeywordSet"]:
        """按名字找配置：`MULTI_AGENT_KEYWORDS` 目录 → 仓库 `config/keywords/`。"""
        dirs: List[str] = []
        env = os.environ.get("MULTI_AGENT_KEYWORDS")
        if env:
            dirs.append(env)
        dirs.extend(search or [])
        here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        dirs.append(os.path.join(here, "config", "keywords"))
        for d in dirs:
            path = os.path.join(d, f"{name}.json")
            if os.path.isfile(path):
                return cls.from_file(path)
        return None

    # ---------------- 匹配 ----------------
    def _pats(self) -> List[Tuple[KeywordGroup, Any]]:
        if self._compiled is None:
            compiled: List[Tuple[KeywordGroup, Any]] = []
            for g in self.groups:
                if g.regex:
                    compiled.append((g, re.compile("|".join(g.keywords), re.IGNORECASE)))
                else:
                    compiled.append((g, tuple(k.lower() for k in g.keywords)))
            self._compiled = compiled
        return self._compiled

    def match(self, text: str) -> List[Dict[str, Any]]:
        """一行文本 → 命中的组（每组最多一条，带命中的关键词）。"""
        low = (text or "").lower()
        if not low.strip():
            return []
        hits: List[Dict[str, Any]] = []
        for g, pat in self._pats():
            if g.regex:
                m = pat.search(low)
                if not m:
                    continue
                word = m.group(0)
            else:
                word = next((k for k in pat if k and k in low), "")
                if not word:
                    continue
            hits.append({"group": g.group, "keyword": word, "level": g.level,
                         "target": dict(g.target), "mode": g.mode,
                         "description": g.description})
        return hits

    def scan_lines(self, lines: Iterable[str],
                   judge: Optional[Callable[[Dict[str, Any]], Any]] = None,
                   max_hits: Optional[int] = None) -> Dict[str, Any]:
        """逐行扫：命中就给"整行 + 行号 + 该交给谁"。

        `judge(hit) -> bool`：给 `mode="llm"` 的组用，判断这条是否真算数。
        没注入就降级为程序命中（`judged=False`），不丢数据。
        """
        cap = int(max_hits or self.max_hits)
        hits: List[Dict[str, Any]] = []
        total = 0
        judged_calls = 0
        llm_failed = 0

        for i, raw in enumerate(lines, start=1):
            total += 1
            text = str(raw).rstrip("\n")
            for h in self.match(text):
                if h["mode"] == "llm" and judge is not None:
                    judged_calls += 1
                    try:
                        ok = bool(judge({**h, "line": text, "line_no": i}))
                    except Exception:  # noqa: BLE001 - 判定失败按程序命中处理
                        ok, llm_failed = True, llm_failed + 1
                    if not ok:
                        continue
                    hit = {**h, "line": text, "line_no": i, "judged": True}
                else:
                    hit = {**h, "line": text, "line_no": i,
                           "judged": h["mode"] != "llm"}
                hits.append(hit)
                if len(hits) >= cap:
                    break
            if len(hits) >= cap:
                break

        by_group: Dict[str, int] = {}
        for h in hits:
            by_group[h["group"]] = by_group.get(h["group"], 0) + 1
        return {"hits": hits, "lines": total, "matched": len(hits),
                "by_group": by_group, "keyword_set": self.name,
                "judged_calls": judged_calls, "llm_failed": llm_failed,
                "degraded": False, "reason": ""}

    def describe(self) -> Dict[str, Any]:
        return {"name": self.name, "source": self.source, "groups": [
            {"group": g.group, "keywords": list(g.keywords), "level": g.level,
             "mode": g.mode, "target": dict(g.target)} for g in self.groups]}


def judge_via_llm(prompt_prefix: str = "") -> Callable[[Dict[str, Any]], bool]:
    """把 `llm/chat` 包成 `judge(hit)`：模型说"是"才算命中。

    只在 `mode="llm"` 的组上用；端点没配时 `chat` 返回 degraded，这里按"不否决"处理
    （宁可多留一条，也不把已经按关键词命中的丢掉）。
    """
    from ..integrations.llm import chat

    def _judge(hit: Dict[str, Any]) -> bool:
        prompt = (f"{prompt_prefix}\n"
                  f"这行日志/新闻是否真的属于「{hit.get('group')}」这一类、值得往上报警？\n"
                  f"只回答 YES 或 NO。\n\n{hit.get('line') or ''}")
        res = chat({"prompt": prompt, "system": "只回答 YES 或 NO"})
        if res.get("degraded"):
            return True
        text = str(res.get("text") or "").strip().upper()
        return text.startswith("YES") or text.startswith("Y")

    return _judge