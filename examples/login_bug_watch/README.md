# 登录失败：一条 error 怎么变成补丁

```
python examples/login_bug_watch/run_demo.py
```

跑完会逐个打印 `PASS/FAIL`，退出码 0 表示全过。

## 三种框架形态

| 形态 | 触发方式 | 成本 | 这个例子里看到什么 |
|---|---|---|---|
| A `program` | 关键词 `登录失败` / `LOGIN_FAILED` | 0 token | 采到 2 条 `/v1/login` 失败，定位到 `server/auth.py`；规则模板跳过公开接口鉴权改造，对该缺陷无解，如实 deferred |
| B `llm` | `retry` 先粗筛，模型复核 | 1 次判定 | 多采到 `login retry count=6` 这条模糊行 |
| C `tools` | 模型先 `grep_code` + `read_file`，再出 `edits` | 2 次工具调用 + 1 次回复 | 补丁落到 `server/auth.py`，`bump_fail(username)` 真的写进去，审计 error 不增加 |

## 文件

```
keywords.json                 注册「登录失败」关键词组（加一条就多一条触发）
fixture/logs/server.log       收到的日志：两条登录失败 + 一条 retry 可疑行
fixture/repo/server/auth.py   有缺陷的登录实现（失败不计数）
run_demo.py                   三形态对照 + 逐项判定
out/                          每次运行重新生成的工作副本与补丁
```

## 换成真模型

把 `config/llm.json` 的 `profiles.chat.url/model` 填上、Key 写 `config/llm.local.json`，
然后去掉 `run_demo.py` 里的 `tool_caller=mock_model`，`dev/propose` 就会走真实端点。
工具集是只读的，模型改不了文件，只能返回 `edits`，落地仍过 find/语法/回放三道门。