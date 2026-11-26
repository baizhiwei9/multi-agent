"""bugfix 编排：bug 采集 → 分诊 → 定位 → 补丁 → 开发 → 送审 → 审计 → 闸门 → 报告。

这里只写 capability/action，不写"谁来干"。默认对应：

    bug      → projects/roles/bug_collector.py
    triage   → projects/roles/bug_triage.py
    dev      → projects/roles/bugfix_dev.py
    report   → projects/roles/bugfix_reporter.py
    supervisor / auditor / server_risk / db → 框架内置角色

任何一个都能换成别的实现（Sentry 采集官、外部编码 Agent 当 dev），编排不动。
"""
from __future__ import annotations

from typing import Any, Dict

BUGFIX_FLOW: Dict[str, Any] = {
    "name": "game_server_bugfix",
    "description": "线上 bug → 分诊 → 定位 → 补丁方案 → 开发 → 审计 → 闸门 → 修复报告",
    "audit_project": "game_server",
    "steps": [
        # 1) 采集：日志/崩溃上报 → 去重后的 bug 列表（取不到就 degraded，不编造）
        {"name": "bugs", "capability": "bug", "action": "collect",
         "params": {"log_path": "${params.log_path}", "hours": "${params.hours}"}},

        # 2) 分诊：按接口聚簇（零 token）；没有 bug 就整条链跳过
        {"name": "triage", "capability": "triage", "action": "classify",
         "params": {"bugs": "${steps.bugs.result.bugs}",
                    "repo_root": "${params.repo_root}",
                    "max_clusters": "${params.max_clusters}"},
         "depends_on": ["bugs"],
         "when": {"step": "bugs", "path": "result.bugs", "op": "gt", "value": 0}},

        # 3) 定位：栈顶帧 → 路由表 → 关键词（零 token）
        {"name": "locate", "capability": "dev", "action": "locate",
         "params": {"bugs": "${steps.bugs.result.bugs}",
                    "repo_root": "${params.repo_root}"},
         "depends_on": ["triage"], "timeout_s": 300},

        # 4) 补丁方案：规则模板优先；use_llm 时才问 llm/chat（只产 edits，不落地）
        {"name": "propose", "capability": "dev", "action": "propose",
         "params": {"bugs": "${steps.bugs.result.bugs}",
                    "triage": "${steps.triage.result}",
                    "locate": "${steps.locate.result}",
                    "repo_root": "${params.repo_root}",
                    "llm": "${params.use_llm}"},
         "depends_on": ["locate"], "timeout_s": 300},

        # 5) 经理派单：把补丁方案分给开发（谁干、干什么、验收什么）
        {"name": "dispatch", "capability": "supervisor", "action": "assign",
         "params": {"workstreams": "${steps.propose.result.workstreams}",
                    "capability": "dev", "action": "implement"},
         "depends_on": ["propose"]},

        # 6) 开发：应用 edits 到工作副本，出补丁 + 语法检查
        {"name": "fix", "capability": "dev", "action": "implement",
         "foreach": {"from": "${steps.propose.result.workstreams}", "as": "ws", "max": 8},
         "params": {"workstream": "${ws}",
                    "repo_root": "${params.repo_root}",
                    "out_dir": "${params.out_dir}"},
         "depends_on": ["dispatch"], "timeout_s": 600},

        # 7) 经理：压送审件
        {"name": "pack", "capability": "supervisor", "action": "pack",
         "params": {"payload": "${steps.fix.result}", "kind": "bugfix_bundle"},
         "depends_on": ["fix"]},

        # 8) 审计官：读文档/协议/代码/迁移/配置（规则级，零 token）
        {"name": "audit", "capability": "auditor", "action": "code",
         "params": {"repo_root": "${params.repo_root}",
                    "doc_root": "${params.doc_root}",
                    "level": "${params.audit_level}"},
         "depends_on": ["pack"], "timeout_s": 600},

        # 9) 风控官：容量/回放/回滚/灰度/经济
        {"name": "gate", "capability": "server_risk", "action": "gate",
         "params": {"audit": "${steps.audit.result}",
                    "capacity": "${params.capacity}",
                    "replay": "${params.replay}",
                    "rollback": "${params.rollback}",
                    "canary_pct": "${params.canary_pct}",
                    "economy": "${params.economy}",
                    "strict": "${params.strict}"},
         "depends_on": ["audit"], "timeout_s": 300},

        # 10) 经理台账
        {"name": "ledger", "capability": "supervisor", "action": "ledger",
         "params": {"pack": "${steps.pack.result}",
                    "entries": [
                        {"round": 1, "state": "${steps.audit.result.verdict}",
                         "chars": "${steps.pack.result.chars}", "delivered": True},
                        {"round": 2, "state": "${steps.gate.result.gate}",
                         "chars": 0, "delivered": True}]},
         "depends_on": ["gate"]},

        # 11) 报告官：修复报告 + 补丁 + 证据（没有 bug 时也要出"未发现 bug"的报告）
        {"name": "report", "capability": "report", "action": "build",
         "params": {"bugs": "${steps.bugs.result.bugs}",
                    "triage": "${steps.triage.result}",
                    "locate": "${steps.locate.result}",
                    "propose": "${steps.propose.result}",
                    "dev": "${steps.fix.result}",
                    "pack": "${steps.pack.result}",
                    "audit": "${steps.audit.result}",
                    "gate": "${steps.gate.result}",
                    "ledger": "${steps.ledger.result}",
                    "repo_root": "${params.repo_root}",
                    "out_dir": "${params.out_dir}"},
         "depends_on": ["ledger"], "run_on_skipped": True},

        # 12) 修复记录入库（唯一 DB 入口）
        {"name": "store", "capability": "db", "action": "write",
         "params": {"collection": "game_bugfixes",
                     "key": "${params.ticket_id}:latest",
                     "value": {"markdown": "${steps.report.result.markdown}",
                               "out_dir": "${steps.report.result.out_dir}",
                               "gate": "${steps.report.result.gate}",
                               "bugs": "${steps.bugs.result.by_severity}",
                               "fixed": "${steps.propose.result.counts}"}},
         "depends_on": ["report"]},
    ],
}