# 审计接口（auditor）：审什么、怎么判、怎么写单元用例

采集和开发把东西交上来之后，审计这一环管两件事：**判**（`code`）和**出用例**（`test_plan`）。
两者都是只读、零 token 优先，判不动的地方才升级到模型。

## 1. `auditor/code`：输入输出

```python
# 入参
{"repo_root": "/path/to/repo", "doc_root": "/path/to/repo/docs", "level": "rule"}

# 出参（节选）
{
  "ok": True, "verdict": "fail",          # pass / warn / fail
  "counts": {"error": 7, "warn": 7, "info": 0},
  "findings": [
    {"code": "auth_missing", "severity": "error",
     "message": "接口声明要鉴权、实现没鉴权",
     "file": "server/routes.py", "line": 18,
     "evidence": {"path": "/v1/player/ban", "declared_auth": True}}
  ],
  "metrics": {"files": 12, "lines": 860, "endpoints": 8}
}
```

`level` 三档，和 `audit_level` 同一个口径：

| level | 跑什么 | 成本 |
|---|---|---|
| `rule` | 协议↔代码、迁移、配置、并发/幂等、代码卫生 | 0 token |
| `stat` | 再跑统计检验 | 0 token（需 numpy） |
| `llm` | 再跑语义审计（`config/llm.json` 的 `profiles.audit`） | 烧 token |

**判定优先规则**：有任一条 `error` → `fail`；只有 `warn` → `warn`；全空 → `pass`。
开发把补丁交上来后重跑一次，`counts.error` 下降才算真的修好——这就是"审计验收"。

## 2. `auditor/test_plan`：自己写单元用例

这是"审计侧自己写用例"的入口：它吃接口清单，按接口 × 场景生成用例，交给风控/QA。

```python
# 入参
{"endpoints": [{"id": "P-04", "method": "POST", "path": "/v1/player/ban",
                "auth": "是（服务名）", "idem": "request_id",
                "errors": ["FORBIDDEN"]}],
 "llm": False, "as_of": "2026-11-26 09:00"}

# 出参
{
  "markdown": "# 测试计划与用例（2026-11-26 09:00）…",
  "cases": [{"id": "P-04-01", "interface": "P-04", "method": "POST",
             "path": "/v1/player/ban", "scenario": "鉴权缺失",
             "priority": "P0", "expected": ["返回 FORBIDDEN", "不产生副作用"]}],
  "count": 4, "by_scenario": {"鉴权缺失": 1, "幂等重放": 1, "参数边界": 1, "正常流": 1},
  "llm_used": False
}
```

每个接口固定出四类场景：**鉴权缺失、幂等重放、参数边界、正常流**。
`llm=True` 时让模型把用例写得更细（走 `config/llm.json` 的 `profiles.test_plan`），
端点没配就退回模板并标 `degraded`，不会卡住发布。

### 落地成 pytest：可以直接生成

传 `pytest_path`（或 `emit_pytest: true` + `out_dir`）就把 cases 直接落成测试文件：

```python
res = test_plan({"endpoints": [...], "pytest_path": "tests/test_generated_cases.py"})
# res["pytest_path"] -> 绝对路径；res["pytest_cases"] -> 用例数
```

跑法：`API_BASE=http://127.0.0.1:8080 API_TOKEN=xxx pytest -q tests/test_generated_cases.py`。
不设 `API_BASE` 时全部 skip，不会误报。鉴权缺失 / 越权 / 参数边界 / 重放四类直接断言，
并发、超时注入、业务错误码保留用例与 skip 原因，等按业务打桩后再打开断言。

不想用生成器时，也可以按下面的手写模板落地（把 `cases` 转成 pytest 参数化）：

```python
# tests/test_generated_cases.py —— 由 auditor/test_plan 的 cases 生成
import pytest

CASES = [
    {"id": "P-04-01", "method": "POST", "path": "/v1/player/ban",
     "scenario": "鉴权缺失", "priority": "P0",
     "expected": ["返回 FORBIDDEN", "不产生副作用"]},
]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_api_case(case, client):
    resp = client.request(case["method"], case["path"], with_auth=False)
    assert resp.status in (401, 403), f"{case['id']} 未拒绝未授权请求"
    assert resp.no_side_effect, f"{case['id']} 产生了副作用"
```

写法要点：

- **用例 ID 与审计 finding 的 code 对齐**（`auth_missing` ↔ `鉴权缺失`），
  这样"审计报了什么、哪条用例在验"能一一对上；
- **P0 用例必须先过**：未授权、越权、重放这类，失败就 `block`，不看总量；
- **每条用例都要有"不产生副作用"的断言**，否则只验证了状态码。

## 3. 风控（server_risk）怎么接

审计出结论之后是闸门，`server_risk/gate` 吃审计结果 + 容量/回放/回滚/灰度/经济：

```python
{"audit": {...}, "capacity": {"expected_ccu": 4200, "ccu_limit": 5000},
 "replay": {"engine": "server/sim.py", "scenario": "scenario.json", "allow_exec": True},
 "rollback": {"plan": "回滚镜像", "script": "ops/rollback.sh", "tested": True},
 "canary_pct": 5, "economy": {"inflation_daily_pct": 0.6}, "strict": True}
# → {"gate": "pass|warn|block", "blocked_by": [...]}
```

闸门不看"用例写了多少条"，只看：审计 error 有没有、回放哈希是否一致、
回滚有没有验证过。回放会**执行被审仓库的代码**，所以必须显式 `allow_exec=True`。

## 4. 三者的分工

| 角色 | 问的问题 | 产出 |
|---|---|---|
| `auditor/code` | 代码和文档/协议对不对得上 | `findings` + `verdict` |
| `auditor/test_plan` | 该验哪些场景、预期是什么 | `cases` + Markdown |
| `server_risk/gate` | 能不能上线 | `pass/warn/block` |

开发修完 → 审计重跑（`error` 是否下降）→ 用例覆盖 → 闸门放行，这个顺序不要跳。

## 5. 接模型只改一处

语义审计和用例生成都从 `config/llm.json` 取端点：

```json
{
  "profiles": {
    "audit":     {"url": "http://127.0.0.1:18799/v1/chat/completions",
                  "model": "openclaw/yuanbao-agent",
                  "token": "${ENV:AUDIT_LLM_TOKEN}"},
    "test_plan": {"url": "http://127.0.0.1:18799/v1/chat/completions",
                  "model": "openclaw/yuanbao-agent",
                  "token": "${ENV:LLM_TOKEN}"}
  }
}
```

Key 用 `${ENV:...}` 引用，仓库里不落明文。没配就自动退回规则级，不烧 token、不卡链路。