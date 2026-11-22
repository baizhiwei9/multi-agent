# 成本模型：这套东西到底烧不烧 token

> 常见疑问：「中继/打洞这套是不是还要过一遍大模型？那开发它有什么意义？」
>
> 结论先给：**全流程只有一个环节需要人或模型介入 —— 交换那一行门票（约 40 字符）。
> 其余全部是程序调用，不拼接 prompt、不调用模型。**

本文不给截图，给的是**可复跑的度量**。

---

## 1. 三步复现（任何人都能验证）

```bash
# 完整度（含真实 UDP 打洞 + 真实 ntfy 中继，约 30 秒）
python3 tools/measure_cost.py

# 只跑本地，不联网
python3 tools/measure_cost.py --skip-relay

# JSON 输出（便于进 CI 做回归比对）
python3 tools/measure_cost.py --json
```

脚本做四件事，每件都是机器算出来的：

| # | 度量什么 | 为什么这样度量 |
|---|---|---|
| 1 | UDP 直连：条数 / 字节 / 每条耗时 / ACK 帧数 | 直接数帧，不靠推测 |
| 2 | HTTP 中继：POST/GET 次数、上行字节、**访问的 URL 去重列表** | URL 列表能看出有没有碰 LLM 端点 |
| 3 | 静态扫描：执行路径源码里有没有 `import openai` 等 | 这是"零 token"的**真正证明**（不是抓包） |
| 4 | 运行时：`sys.modules` 里有没有已加载的模型 SDK | 防止间接依赖偷偷引入 |

> **为什么不用抓包当证据**：抓包只能证明"这次没向 LLM 端点发请求"，
> 证明不了"执行路径里没有模型调用"。要证明后者，只能看**代码依赖**和
> **运行时模块**。所以脚本 3、4 才是关键，1、2 只是数据量参考。

---

## 2. 实测输出（本机，2026-11-22）

```
==================================================================
跨 Agent 通道开销度量（任何人都可复跑）
==================================================================

[1] UDP 直连
    往返 5 条：发出 1105B，收到 1105B
    耗时 251ms（50.22ms/条），ACK 帧 10 个
    LLM 调用：0 次

[2] HTTP 中继（ntfy）
    HTTP 请求 16 次（POST 4 / GET 12）
    上行字节 576B，端到端耗时 11144ms
    访问的 URL（去重）：
      https://ntfy.sh/cost-33e20f46
      https://ntfy.sh/cost-33e20f46/json
    LLM 端点命中：0 次

[3] 静态依赖扫描（执行路径源码）
    扫描 9 个文件：未发现模型 SDK ✅

[4] 运行时已加载模型 SDK：无 ✅

[5] 成本模型（哪些环节需要人或模型）
    交换门票    人 / 大模型一句话       🔥需要模型  ≈ 一条短消息（~40 字符）
    发布/领取门票 HTTP POST/GET 到 ntfy  程序调用  0
    打洞        UDP PUNCH/PONG        程序调用  0
    派指令       UDP 直连 或 HTTP 中继    程序调用  0
    执行脚本     本地 subprocess        程序调用  0
    回传结果      UDP 直连 或 HTTP 中继    程序调用  0
```

**注意第 2 项的中继耗时 11 秒**：这是 ntfy 的 poll 有 ~3s 缓存延迟，
每次收发要等一轮。所以中继适合"发几条指令"，不适合高频交互 —— 这是
**延迟**问题，不是 token 问题。

---

## 3. 三条通道对比

| 通道 | 技术 | 需要模型 | token | 延迟 | 适用 |
|---|---|---|---|---|---|
| **UDP 直连** | 打洞 + seq/ack/去重 | ❌ | **0** | 0–2ms | 两边 NAT 都较宽松 |
| **HTTP 中继** | POST/GET 到 ntfy.sh | ❌ | **0** | ~1.5–11s/次 | 对称 NAT / 严格防火墙 |
| ~~大模型 @~~ | 群里 @ 对方 | ✅ | 有 | 十几秒 | 只用来**启动**对方程序 |

---

## 4. 成本模型（数据化，不写散文）

```python
from tools.measure_cost import cost_model
cost_model()["steps"]
# → [
#   {"step": "交换门票",     "channel": "人 / 大模型一句话",   "needs_llm": True,
#    "tokens": "≈ 一条短消息（~40 字符）", "note": "全流程唯一需要人或模型介入的点"},
#   {"step": "发布/领取门票", "channel": "HTTP POST/GET 到 ntfy", "needs_llm": False, "tokens": 0},
#   {"step": "打洞",         "channel": "UDP PUNCH/PONG",      "needs_llm": False, "tokens": 0},
#   {"step": "派指令",       "channel": "UDP 直连 或 HTTP 中继", "needs_llm": False, "tokens": 0},
#   {"step": "执行脚本",      "channel": "本地 subprocess",      "needs_llm": False, "tokens": 0},
#   {"step": "回传结果",      "channel": "UDP 直连 或 HTTP 中继", "needs_llm": False, "tokens": 0},
# ]
```

这张表就是整套设计的**取舍声明**：

- 用一次模型（交换门票），换来之后 N 次指令全部走程序
- N 越大，省得越多；N=1 时这套没有意义（直接 @ 一次更省事）
- 生产场景里 N 通常是几十到几百（派活、查状态、取结果、跑审计）

---

## 5. 审计官的 token 与本文的关系

`auditor` 默认 `level=rule`：**0 网络、0 token**（纯规则检查）。
`level=stat` 也是 0 token（纯本地统计检验）。
`level=llm` 是**显式可选档** —— 只有调用方传了 `llm` 回调才会跑，
而且回调由调用方注入（模块自己不 import 任何 SDK）。

```python
Auditor(level="rule").audit_balance(bal)          # 0 token
Auditor(level="stat").audit_balance(bal)          # 0 token
Auditor(level="llm", llm=my_fn).audit_balance(bal)  # 烧 token（显式选了才烧）
```

这条也由 `assert_no_llm_import()` 在运行时代为自检，结果写进审计报告的
`layers.L1_rule`。

---

## 6. 静态扫描覆盖哪些文件

`tools/measure_cost.py` 的 `_EXECUTION_MODULES`：

```
multi_agent/bootstrap.py
multi_agent/signal.py
multi_agent/integrations/audit.py
multi_agent/integrations/audit_profiles.py
multi_agent/integrations/runner.py
multi_agent/router.py
multi_agent/shared_data.py
multi_agent/db_policies.py
multi_agent/dbmanager.py
```

这 9 个文件是"执行路径"的全部。新增执行路径模块时**必须**加进这个列表，
否则扫描会漏（有测试 `test_measure_cost_scans_known_modules` 守着）。

标记清单 `_SDK_MARKERS` 覆盖：openai / anthropic / dashscope / litellm /
cohere / google.generativeai / transformers / vllm / ollama / replicate。
