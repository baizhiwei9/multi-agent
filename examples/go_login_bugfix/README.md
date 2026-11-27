# Go 登录失败：error → 补丁 → 重新出包 → 审计 → 同步到目标服务器

```
python examples/go_login_bugfix/run_demo.py
```

跑完逐项打印 `PASS/FAIL`，全过退出码 0；任一不过退出码 1；没有 Go 工具链退出码 2。

和 `examples/login_bug_watch` 的区别：那边缺陷在 Python 脚本里，改完即生效；这边的
缺陷写在**已编译的二进制**里，改 `.go` 不够，必须重新 `go build` 出包，审计跑的是新包，
同步搬的也是新包。

## 这条链在测什么

error report 里出现：

```
2026-11-27T18:03:44Z error 登录失败 123456
```

日志把**口令**打出来了。正确口径是：

```
error 登录失败: bob
```

于是整条链是：

```
bug/collect      扫 error report → 命中「登录失败」，去重后 1 条
dev/locate       在 .go 里定位到 main.go:41 的 logf 行
dev/propose      出 edits：登录失败 %s,*pass  →  登录失败: %s,*user
dev/implement    find/replace 落地（路径在仓内、find 唯一命中）
dev/build        ★ function call：调 go build 真出包，回 sha256 + 字节数
auditor/verify   把新包放到审计目录真跑：失败一次、成功一次，读日志判 pass/fail
sync/push        ★ 审计 pass 才派给「同步服务器」Worker：搬包 + sha256 比对 + 远端自证
report/build     把凭据写成 out/<run>/bugfix_report.md
```

两个 ★ 就是 function call 的两处落点：

| 位置 | 绑定什么 | 代码 |
|---|---|---|
| `dev/build` | Go 编译器 | `multi_agent/integrations/go_toolchain.py`（`go_build`，另带 OpenAI function spec） |
| `sync/push` | 目标服务器 | `multi_agent/integrations/go_sync.py`（`sync_artifact`） |

`sync` 是独立的一个 capability / Agent，注册名 `sync`，描述「同步服务器」：
Coordinator 只在审计 `verdict=pass` 时按 `when` 条件派活；审计 FAIL 时这一步 `skipped`，
目标端一个字节都不动。

## 两个 run

| run | 输入 | 期望 |
|---|---|---|
| `run-fix` | 正常 | 新包日志 `error 登录失败: bob`，审计 pass，同步成功，远端日志同样正确 |
| `run-guard` | `sabotage_bin=事故包`（拿旧包冒充新包） | 审计 fail（口令又出现了）、`sync` skipped、目标端无发布、整条 run `degraded` |

`run-guard` 验的是闸门真的有效，而不是「审计结论挂在报告里没人管」。

## 判定项（16 条）

- 采集：命中 ≥2、去重 1
- 定位：落到 `main.go` 具体行
- 补丁：`find` 唯一命中并落地
- 编译：`go build` 出货、新包与事故包 sha256 不同
- 审计：**旧包确实泄露口令**（缺陷可复现）→ 新包日志为 `error 登录失败: bob` → 不含 `123456` → verdict pass
- 同步：远端 sha256 == 本地、远端日志正确
- 闸门：负向 run 审计 fail、sync skipped、目标端未被污染、run 降级

## 环境

- Go：`GO_BIN=/path/to/go` 可指定；否则按 PATH、`/usr/local/go/bin/go`、
  `~/toolchain/go/bin/go`、`~/../toolchain/go/bin/go` 依次找，找不到就报
  `go_not_found` 并退出 2，绝不假装编译成功。
- 产物目录 `out/`：

```
out/baseline/login-buggy           事故版本（fixture 原样编译，审计对照）
out/run-fix/repo/                  工作副本（补丁只改副本）
out/run-fix/bin/login               新包
out/run-fix/audit/{baseline,new}/   审计跑的日志
out/run-fix/bugfix_report.md        报告
out/target-b/host-b/releases/v2/    同步落点（模拟目标服务器）
out/target-b/host-b/logs/server.log 目标端日志
out/run-guard/                      负向 run
```

## 换成真实环境只改三处

1. `go_sync.sync_artifact` 第 3 步：`shutil.copy2` → `scp` / `rsync` / 发布 API，
   前后两步（sha256 验货、远端自证）不变；
2. `go_toolchain.go_build`：交叉编译时加 `GOOS/GOARCH/GOFLAGS`（`_go_env()` 里注）；
3. `run_demo.py` 的 `params`：`log_path` / `repo_root` / `target_root` / `host` 换成真实路径。

## 边界

- 不接模型：`propose` 走规则模板（0 token）；要接模型仍只产 `edits`，落地过三道门。
- 不连外网、不真远程 SSH：目标是本地目录模拟；真机部署按上面第 1 条替换。
- fixture 里的 `123456` 是示例口令，不是任何真实凭据。