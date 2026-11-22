"""最小示例项目：证明"只声明也能接进框架"。

它不写任何业务实现，只有两件事：
1. 两个角色（collect / render）与各自动作；
2. 一条两步流水线（collect → render），不带审计。

跑法：

    multi-agent project show min-report
    multi-agent project pipelines          # 把它的流水线注册进 PIPELINES

真实项目照这个改：加角色、加步骤、填 audit_project，再决定要不要覆写 handlers。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .base import Project, RoleSpec
from .reporter import MarkdownReporter, Reporter

MIN_PIPELINE: Dict[str, Any] = {
    "name": "min_report",
    "project": "min-report",
    "description": "最小示例：采集 → 渲染（无审计，用来验证扩展点）",
    "steps": [
        {"name": "collect", "capability": "collect", "action": "run",
         "params": {"topic": "${params.topic}"}},
        {"name": "render", "capability": "render", "action": "md",
         "params": {"topic": "${steps.collect.result}"},
         "depends_on": ["collect"]},
    ],
}


class MinimalReporter(MarkdownReporter):
    name = "min-md"
    project = "min-report"
    title = "最小报告"

    def sections(self, context):
        return [("内容", [f"- {context.get('topic') or '（空）'}",
                          f"- 上游结果：{context.get('collected') or '—'}"]),
                ("说明", ["- 这是最小示例，用来展示只声明也能接进流水线"])]


class MinimalReportProject(Project):
    name = "min-report"
    title = "最小示例项目"
    description = "两个角色 + 两步流水线，不写业务实现"
    audit_project = None
    pipeline = MIN_PIPELINE
    default_params = {"topic": "demo"}

    roles = [
        RoleSpec("collect", ["run"], "采集：把输入整理成结构化结果"),
        RoleSpec("render", ["md"], "渲染：输出 Markdown"),
    ]

    def __init__(self, reporter: Optional[Reporter] = None) -> None:
        self._reporter = reporter or MinimalReporter()

    def handlers(self):
        return {
            "collect": {"run": lambda p, c: {"topic": p.get("topic"), "items": []}},
            "render": {"md": lambda p, c: self._reporter.build({"topic": p.get("topic")})},
        }

    def reporter(self):
        return self._reporter