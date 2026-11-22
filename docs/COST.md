# 一次任务到底花多少：帧、字节与 token

> 结论先说：**打洞直连和中继都不消耗 token。** 执行阶段走的是程序之间的 JSON 帧，
> 中间没有 prompt、没有模型调用；全流程里唯一可能经过人或模型的，是开头交换的那一行
> 门票（约 40 字符），或者你显式打开的 `audit_level=llm` 语义审计。

这份文档给出**可以自己跑一遍**的数字，而不是一张截图。

---

## 1. 怎么复现

```bash
python3 scripts/measure_cost.py          # 表格
python3 scripts/measure_cost.py --json   # 机器可读
```

脚本做三件事，场景语义相同（一次请求 + 一次响应，1KB 量级载荷）：

| 场景 | 走什么 | 说明 |
| --- | --- | --- |
| `http` | Hub REST | 从 Worker 注册开始算：注册 + 长轮询取活 + 结果回传 + submit |
| `udp` | 打洞直连 | `HolePuncher` 双端直连，送 1KB |
| `relay` | rendezvous 中继 | 强制走中继（模拟打洞失败） |

计量口径：

- HTTP 按**请求体 + 响应体**的 UTF-8 字节计，不含 TCP/TLS 头、不含 HTTP header；
- UDP 按 `sendto` 的 datagram 计（含 `D:`/`PUNCH ` 前缀），不含 IP/UDP 头（20+8 字节）；
- 帧数按真实 `sendto` 次数计，打洞收尾的控制帧不剔除。

## 2. 本机实测（2026-11-22，loopback）

```
场景      帧数 字节数  说明
http         4   1627  REST 请求+响应（注册+轮询+回传+提交）
udp          1   1026  打洞直连（1024 载荷 + 2 字节 D: 前缀）
relay        3   2892  中继转发（信封 + base64 放大）
执行路径加载的大模型 SDK：无
```

读法：

- 直连一次 1KB 往返就是 **1026 字节**，多一个字节都没有——UDP 上没有 HTTP 头、没有 JSON 之外的东西；
- 中继比直连贵约 2.8 倍，贵在**信封 + base64**（1KB 载荷编成 ~1.4KB 文本再套 JSON），但仍然只是字节，不是 token；
- Hub REST 的 4 次调用是"注册 / 取活 / 回传 / 提交"各一次；调大 `poll_wait` 可减少空轮询次数，不影响正确性。

## 3. token 到底花在哪

| 环节 | 是否经过模型 | 说明 |
| --- | --- | --- |
| 交换门票（`A1\|ip\|port\|port\|id`） | **可能**（人转发 / 模型转发） | 全流程唯一一次，约 40 字符 |
| 打洞与直连收发 | ❌ | UDP JSON 帧，`HolePuncher` / `DirectLink` |
| 中继转发 | ❌ | rendezvous 或 ntfy 中继，程序转发字节 |
| Hub 派活、DB 读写、流水线 | ❌ | REST / TCP / JSON-RPC，纯程序 |
| 审计 L1/L2 | ❌ | 纯本地规则与统计（`assert_no_llm_import()` 兜底） |
| 审计 L3（`audit_level=llm`） | ✅ 显式 | 需要调用方注入 `llm` 回调；默认不启用 |

也就是说：**只要门票到手，后面跑一百个任务和跑一个任务的 token 成本都是 0**；
成本只随"字节"增长，不随"轮次"增长。真正会让账单动的是你自己决定用模型做规划或 L3 审计那几次调用。

## 4. 这些数字不能证明什么

- 不是抓包。脚本统计的是应用层调用（`sendto` / `urlopen`），没有 pcap；
- 不含 TLS。真要走公网且担心窃听，应在 WireGuard / TLS 前置代理上跑，这部分开销本脚本不算；
- 中继场景在本机用 rendezvous 直转模拟；公网 ntfy 中继会多一层 HTTP 封装，字节数更大，但**依然不产生 token**；
- 字节数随载荷线性增长，本表只覆盖 1KB 量级；大结果请自行调 `--json` 观察。

## 5. 顺带修掉的一个真问题

第一次跑这个脚本时直连场景偶发失败，查下去是 `HolePuncher._loop` **没有节流**：
打洞期间每轮循环都补一帧 `PUNCH`，把 socket 打满、丢包，`connect()` 误判超时退回中继。
现在按 `PUNCH_INTERVAL=0.3s` 限速、直连建立后立即停手（见 `multi_agent/nat.py`），
`tests/test_nat.py` 覆盖直连、中继、去重与鉴权。

---

复现命令再贴一次：

```bash
python3 scripts/measure_cost.py --json
python3 -m pytest tests/test_cost.py -q
```