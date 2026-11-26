"""互联网开发（netdev）采集框架的三个例子：**关键词多少、命中后调哪个**。

这三个例子就是"给游戏/互联网开发这头补框架"的样板：用户说这头之前没实现，
所以框架里把最常见的三类事件写成开箱可用的关键词组，加一组关键词就能多一类触发。

    from multi_agent.frameworks.netdev import BugWatchFramework, examples
    fw = BugWatchFramework(examples.ERROR_WATCH)       # 只关心错误行
    fw = BugWatchFramework(examples.CRASH_WATCH)       # 只关心崩溃/断言
    fw = BugWatchFramework(examples.PERF_WATCH)        # 只关心超时/5xx，交风控

每组的 `target` 就是"调哪个"：`{"capability": "dev", "action": "locate"}`
表示命中后由 coordinator 派给 dev 的 locate 动作。
"""
from __future__ import annotations

from ..keywords import KeywordSet

#: 例 1：通用错误行 → 交开发定位（最常见，成本最低）
ERROR_WATCH = KeywordSet.from_dict({
    "name": "netdev-error",
    "max_hits": 50,
    "groups": [{
        "group": "error",
        "description": "ERROR/CRITICAL/FATAL/Exception → 开发定位",
        "keywords": ["ERROR", "CRITICAL", "FATAL", "Exception", "error:"],
        "level": "error",
        "mode": "program",
        "target": {"capability": "dev", "action": "locate"},
    }],
})

#: 例 2：崩溃/断言失败 → 最高优先级，直接交开发
CRASH_WATCH = KeywordSet.from_dict({
    "name": "netdev-crash",
    "max_hits": 20,
    "groups": [{
        "group": "crash",
        "description": "Traceback / panic / 段错误 / 断言失败",
        "keywords": ["Traceback (most recent call last)", "panic:", "Segmentation fault",
                     "AssertionError", "core dumped"],
        "level": "critical",
        "mode": "program",
        "target": {"capability": "dev", "action": "locate"},
    }],
})

#: 例 3：超时 / 5xx → 先当容量问题交风控，不急着改代码
PERF_WATCH = KeywordSet.from_dict({
    "name": "netdev-perf",
    "max_hits": 30,
    "groups": [{
        "group": "timeout",
        "description": "超时 / 504 / 502 / 503 → 风控容量核查",
        "keywords": ["timeout", "timed out", "超时", "504", "502", "503"],
        "level": "warn",
        "mode": "program",
        "target": {"capability": "server_risk", "action": "capacity"},
    }],
})

#: 例 4：模糊可疑行 → 规则粗筛 + 模型复核（展示 mode="llm" 怎么配）
SUSPECT_WATCH = KeywordSet.from_dict({
    "name": "netdev-suspect",
    "max_hits": 20,
    "groups": [{
        "group": "suspect",
        "description": "slow/retry/降级：规则先挑，模型说算才算",
        "keywords": ["slow", "retry", "降级", "抖动"],
        "level": "info",
        "mode": "llm",
        "target": {"capability": "dev", "action": "locate"},
    }],
})

EXAMPLES = {"error": ERROR_WATCH, "crash": CRASH_WATCH,
            "perf": PERF_WATCH, "suspect": SUSPECT_WATCH}