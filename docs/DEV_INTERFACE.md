# 开发接口（dev）：收到一个 bug，怎么处理

采集把命中行往上送，经理派单之后，真正动代码的是 `dev`。它有三个动作，
**契约固定、实现可换**——换成一个会写代码的模型 Agent，只要按这张表收发即可。

## 1. 三个动作的契约

| 动作 | 入参（关键字段） | 出参（关键字段） | 成本 |
|---|---|---|---|
| `dev/locate` | `bugs`、`repo_root` | `findings[]`、`unlocated[]`、`counts` | 0 token |
| `dev/propose` | `bugs`、`triage`、`locate`、`repo_root`、`llm` | `workstreams[]`、`deferred[]`、`mode` | 0（默认） |
| `dev/implement` | `workstream`、`repo_root`、`out_dir` | `patch`、`files[]`、`tests[]`、`applied` | 0 |

### locate 的输入输出

```python
# 入参
{
  "bugs": [{"bug_id": "a1", "error_code": "BAN_FORBIDDEN",
            "endpoint": "/v1/player/ban", "severity": "error",
            "frame": {"file": "server/routes.py", "line": 18, "func": "ban"}}],
  "repo_root": "/path/to/work/repo"
}

# 出参
{
  "ok": True, "degraded": False,
  "findings": [{"bug_id": "a1", "file": "server/routes.py", "line": 18,
                "func": "ban", "confidence": "high", "how": "traceback"}],
  "unlocated": [],
  "counts": {"located": 1, "unlocated": 0, "routes": 8}
}
```

置信度只有三档，且**找不到就是找不到**：`high`（栈顶帧且文件真实存在）、
`medium`（路由表命中）、`low`（关键词 grep）。`unlocated` 里的条目会进报告"未决项"。

### propose 的输入输出

```python
# 入参
{"bugs": [...], "triage": {...}, "locate": {...},
 "repo_root": "/path/to/work/repo", "llm": False}

# 出参
{
  "ok": True, "mode": "rule",          # rule / rule+llm
  "workstreams": [{
      "id": "fix-v1-player-ban",
      "name": "修复 /v1/player/ban",
      "files": ["server/routes.py"],
      "edits": [{"file": "server/routes.py",
                 "find": "@route(\"POST\", \"/v1/player/ban\", auth=False)",
                 "replace": "@route(\"POST\", \"/v1/player/ban\", auth=True)",
                 "why": "接口声明要鉴权、实现没鉴权（auth_missing）"}],
      "acceptance": ["/v1/player/ban 相关缺陷已修复且语法通过"]
  }],
  "deferred": [{"endpoint": "/v1/match/join", "reason": "没有可套用的修复模板"}],
  "dropped": [], "llm": {"used": False}
}
```

`edits` 是**唯一**能改代码的载体：`file` + `find` + `replace`。模型的输出也只能长这样，
落地前要过三道门（路径在仓库内、`find` 真命中、语法通过），过不了就丢弃并记 `dropped`。

### implement 的输入输出

```python
# 入参
{"workstream": {上面 workstream 那一条}, "repo_root": "...", "out_dir": "release/bugfix"}

# 出参
{
  "ok": True, "degraded": False, "workstream": "fix-v1-player-ban", "applied": True,
  "patch": "release/bugfix/patches/ticket/fix-v1-player-ban.patch",
  "files": [{"path": "server/routes.py", "status": "modified",
             "sha256_before": "...", "sha256_after": "...", "bytes": 512}],
  "tests": [{"name": "syntax:server/routes.py", "status": "passed", "detail": ""}],
  "notes": [{"file": "server/routes.py", "status": "applied", "count": 1}],
  "summary": "分片：修复 /v1/player/ban（fix-v1-player-ban）..."
}
```

## 2. 一个 bug 走完全程

```python
from multi_agent.integrations.game_server_bugfix import locate, propose
from multi_agent.integrations.game_server_dev import implement

bugs = [{"bug_id": "a1", "error_code": "BAN_FORBIDDEN",
         "endpoint": "/v1/player/ban", "severity": "error",
         "message": "封禁接口越权：auth=False 也能调用", "frame": None}]

loc = locate({"bugs": bugs, "repo_root": "/repo"})          # ① 定位
tri = {"clusters": [{"id": "v1-player-ban", "endpoint": "/v1/player/ban",
                     "bug_ids": ["a1"], "occurrences": 1, "error_codes": ["BAN_FORBIDDEN"]}]}
prop = propose({"bugs": bugs, "triage": tri, "locate": loc,
                "repo_root": "/repo", "llm": False})         # ② 出补丁方案
for ws in prop["workstreams"]:
    out = implement({"workstream": ws, "repo_root": "/repo",
                     "out_dir": "/tmp/out"})                    # ③ 落地
    assert out["ok"], out
```

直接调是"本地跑一遍"；走 Hub 就是把它交给另一个进程：

```python
client.submit("dev", "implement", {"workstream": ws, "repo_root": "/repo",
                                   "out_dir": "/tmp/out"}, wait=True)
```

## 3. 换实现：只要三个动作对上

写一个新的 `dev` Agent，不必 import 上面的实现：

```python
class MyDev:
    def locate(self, p, c):   ...
    def propose(self, p, c):  ...
    def implement(self, p, c): ...

rt.add_handler("dev", "locate", my_dev.locate)
rt.add_handler("dev", "propose", my_dev.propose)
rt.add_handler("dev", "implement", my_dev.implement)
```

要接一个会写代码的编码 Agent（Claude Code / Cursor / 自研），最省事的做法是用
`multi-agent mcp-agent --command ... --prefix dev`，把它暴露的工具挂成 `dev` 能力；
框架只负责派单与验收，不关心它是规则还是模型。

## 4. 三条边界（实现方也必须守）

1. **不猜位置**：`findings` 的 `confidence` 要能解释（栈帧/路由/关键词），
   找不到就进 `unlocated`，别给一个假行号；
2. **不越权改**：只认 `file/find/replace`，路径限制在 `repo_root` 内；
3. **不假装改过**：`find` 未命中记 `miss`，`applied=False`，`reason` 写清
   "没有产生任何代码变更"——上游据此决定要不要人工介入。

## 5. 框架基类：DevHandler

三个动作的默认实现已经收在一个类里。要接新的开发角色（规则 / 模型 / 外部编码 Agent），
继承它、只覆盖要换的方法即可，编排不用改：

```python
from multi_agent.integrations.game_server_dev import DevHandler, register_framework

class MyDev(DevHandler):
    def propose(self, params, ctx=None):
        # 入参同 §1；必须返回 {workstreams: [{files, edits, acceptance}]}
        return {"ok": True, "workstreams": [...]}

register_framework(runtime, handler=MyDev())   # 挂 locate / propose / implement
register(runtime)                              # 兼容旧入口：仍只挂 implement
```

`DevHandler.locate / propose / implement` 的默认实现全部零 token：定位走
栈帧 → 路由表 → 关键词，出方案走模板，落地走 find/replace + 语法检查。
只想换一个动作就只覆盖那一个，其余继承默认。

## 6. 带工具的开发：模型先查代码，再出 edits

`dev/propose` 传 `tools=True` 时，`llm/chat` 会走 function call：模型可以调
`read_file` / `grep_code` / `list_dir` 自己找代码，再返回 `edits`。工具是**只读**的，
写仍然只走 `edits` + 三道门（路径、find 命中、语法）。

```python
from multi_agent.integrations.dev_tools import build

# build(repo_root) -> (tool_specs, impls)，交给 llm/chat 或 dev/propose 用
specs, impls = build("/path/to/repo")
res = chat({"prompt": "修 /v1/login 的登录失败", "tools": specs, "tool_impls": impls})
# res["tool_trace"] 记录每轮调了谁、参数、成败、返回长度
```

`dev/propose` 直接开：

```python
propose({"bugs": bugs, "triage": tri, "locate": loc, "repo_root": repo,
         "llm": True, "tools": True, "max_tool_rounds": 4})
# 返回 llm.tool_trace / llm.tool_rounds；超轮次 → degraded + truncated
```

完整例子见 `examples/login_bug_watch/`：同一份日志跑 program / llm / tools 三种形态，
逐项判定"采集对不对、定位对不对、补丁对不对"。

## 7. 结果交给谁

`implement` 的产出不是终点：

- 有 `patch` → `supervisor/pack` 压送审件 → `auditor/code` 审计；
- `tests` 里有 `failed` → 直接回给经理（`supervisor/assign`）重派，不往下游走；
- 语法通过但审计仍报同类问题 → 进报告的"未决项"，由人决定。

审计侧怎么接、怎么自己写单元用例，见 `docs/AUDIT_INTERFACE.md`。