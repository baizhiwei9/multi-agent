# 零 IP 打洞引导（STUN → 换票 → 直连）

> 大佬 2026-11-22 17:43 的原话：
> **「我也不知道它的公网 IP，他们都是私有的 IP，他们也得打洞。你得先通过大模型通讯，
> 让他们把某个程序执行起来，然后再打洞。减少 TOKEN 的消耗。」**
>
> 这一层就是照这个做的：**只花一次短消息的钱，之后全程直连、零 token。**

---

## 1. 问题：三方都不知道对方在哪

三个 agent 分别在私有网络里：

```
    openclaw                    hermes                     coze
  192.168.1.156              10.5.86.193               （私有）
   NAT 后面                   NAT 后面                   NAT 后面
       │                          │                          │
       └──────── 彼此不可达，也不知道对方公网地址 ────────────┘
```

手工填 IP 会踩三个坑：

| 坑 | 后果 |
|---|---|
| 不知道对方公网 IP | 没法连 |
| 就算知道 IP，NAT 映射的**端口**也是临时的 | 填一次过一阵就失效 |
| 填错一个字符 | 静默连不上，很难查 |

---

## 2. 解法：三段式（token 成本 ≈ 一条短消息）

```
① 探测     每台机器自己用 STUN 问出「我在公网看起来是什么样」
            ↓ 不需要任何人知道 IP
② 换票     生成一张 ~40 字符的门票，**只发一次**
            ↓ 这就是全流程唯一的 token 成本
③ 打洞     双方各自往对方公网地址发 PUNCH，NAT 放行后直连
            ↓ 之后所有指令/结果走这条直连，**零 token**
```

### 2.1 探测（自己问，不问人）

```
$ python3 -m multi_agent.bootstrap stun
{
  "mapped": ["120.245.114.140", 50237],
  "attempts": [{"server": "stun.cloudflare.com:3478",
                "mapped": ["120.245.114.140", 50237], "ms": 187}],
  "symmetric_hint": false
}
```

实测（本机）：Cloudflare 187ms、小米 STUN 10ms 都返回；Google/腾讯的 STUN
在这台机器上不通（容器网络限制）。所以内置了多个 STUN 服务器轮询。

**`symmetric_hint: true`** 说明不同 STUN 返回不同端口 → 疑似对称 NAT，
打洞成功率低，应该直接走中继。

### 2.2 换票（唯一的一次 token）

```
$ python3 -m multi_agent.bootstrap ticket --id openclaw
A1|120.245.114.140|50237|8792|openclaw
```

格式：`A1|<公网IP>|<公网端口>|<本地端口>|<agent_id>`

- **公网端口**：NAT 临时映射的，必须现测现用，不能缓存
- **本地端口**：本机 socket 绑的端口。对端必须往公网端口打，我才能在本地收到
  （少了这个字段，对端不知道要打哪个公网端口）

> 试过 base64+JSON 的"短格式"，结果 95 字符 vs 纯文本 38 字符 —— **更长了，已废弃**。
> 门票越短越好，因为它要过一次聊天。

### 2.3 打洞（之后零 token）

```
# B（执行端）
$ python3 -m multi_agent.bootstrap join --id hermes \
    --peer-ticket 'A1|120.245.114.140|50237|8792|openclaw' \
    --exec-on-msg --listen 300
A1|203.0.113.7|44123|8793|hermes          ← 把自己的票回给对方
[link] punching 120.245.114.140:50237 (peer=openclaw)
[link] ✅ direct 120.245.114.140:50237 rtt≈2ms
```

```
# A（发起端）
$ python3 -m multi_agent.bootstrap join --id openclaw \
    --peer-ticket 'A1|203.0.113.7|44123|8793|hermes' \
    --send '{"action":"run","id":"t1","script":"/srv/jobs/build_sheet.py"}' \
    --wait-result 20
[link] ✅ direct 203.0.113.7:44123 rtt≈0ms
[result] from hermes: {"ok": true, "returncode": 0,
  "stdout": "{\"role\": \"A\", \"action\": \"build_sheet\", ...}",
  "duration_ms": 89, "host": "DH4300PLUS"}
```

---

## 3. 实测结果（同 NAT 后两个进程模拟两台机器）

**打洞**：
```
=== A:
[link] waiting… 29s left
[link] ✅ direct 120.245.114.140:50261 rtt≈0ms
{"ok": true, "state": "direct", "peer": {...agent-b...}, "rtt_ms": 0}
=== B:
[link] direct established with 120.245.114.140:50237
[link] ✅ direct 120.245.114.140:50237 rtt≈2ms
{"ok": true, "state": "direct", "peer": {...agent-a...}, "rtt_ms": 2}
```

**连上后直接执行程序**：
```
A: [link] ✅ direct 120.245.114.140:50357 rtt≈0ms
A: [result] from B: {"ok": true, "returncode": 0,
     "stdout": "{\"role\": \"A\", \"action\": \"build_sheet\", \"commodity\": \"copper\",
                 \"host\": \"DH4300PLUS\"}\n",
     "stderr": "", "duration_ms": 89, "host": "DH4300PLUS"}
```

A 发一条 run 指令 → B 在本机执行 → 结果（stdout/返回码/耗时/主机名）原路返回。
**全程除了一次门票交换，没有任何大模型参与。**

---

## 4. 协议细节

### 4.1 为什么探测和打洞必须复用同一个 socket

NAT 给的是「**socket 五元组 → 公网端口**」的映射。换一个 socket → 换一条映射 →
STUN 探测出来的端口和实际发包的端口不一样 → 对端打的是旧端口，永远打不通。

`DirectLink` 内部就只创建一个 socket（`self.sock`），STUN、PUNCH、业务数据全走它。

### 4.2 最小可靠层

UDP 不保证送达，所以加了一层极简协议（不是 TCP，够用就行）：

| 帧 | 含义 |
|---|---|
| `PUNCH <id>` | 打洞探测，收到就回 PONG |
| `PONG <id>` | 打洞确认 → 状态置 `direct` |
| `A\|<seq>\|<json>` | 应用消息，需要 ack |
| `ACK\|<seq>` | 确认收到 |

`send()` 最多重试 4 次、每次等 1s；超时返回 `False`（**不假装成功**）。
测试 `test_send_reports_failure_when_peer_gone` 锁住了这个行为：对端没了必须返回 False。

### 4.3 状态机

```
idle → discovered → punching → direct
                        └────→ failed（明确说"需要中继"）
```

失败原因写清楚：`打洞超时：双方没能互相穿透（可能对称 NAT / 严格防火墙）→ 需要中继`

---

## 5. 和现有框架怎么接

这一层只负责**建立通道**，通道之上可以跑任何东西：

```python
from multi_agent.bootstrap import DirectLink, run_local

# ① 探测 + 出票（打印给对方）
link = DirectLink("openclaw", 8792)
link.discover()
print(link.ticket())        # 发给对方

# ② 拿到对方票后打洞
link.punch(peer_ticket)

# ③ 之后就是普通收发，零 token
link.send({"action": "run", "script": "/srv/jobs/build_sheet.py"})
msg = link.recv(timeout=30)
```

想直接跑流水线：把它当传输层挂到 Hub 上（或把 `hub.submit` 的指令序列化过去）：

```python
link.send({"capability": "auditor:bond", "action": "balance",
           "params": {"balance": bond, "project": "bond"}})
result = link.recv(30)
```

---

## 5.5 两种打洞方向（重要）

大佬 18:17：
> **「让他那边往这往这边打，就他那边是出口，因为这边的 IP 是固定的。」**

拓扑决定用哪种模式：

| 模式 | 谁发起 | 适用场景 | API |
|---|---|---|---|
| **主动** `punch()` | 我打给对方 | 两边都相对宽松 | `sig.connect(peer_ticket)` |
| **被动** `wait_punch()` | 对方打给我 | **我方 IP 固定 / 够得着，对方只能出站** | `sig.wait_connect()` |

被动模式下：我先 announce 自己的票 → 对方拿票主动 PUNCH 过来 →
我收到 PUNCH 回 PONG 并把对方源地址记成 peer → 直连建立。
**之后收发代码完全一样。**

```bash
# 我这侧（被动等，对方打过来）
python3 -m multi_agent.signal wait --topic mergen-7f3a91 --id openclaw \
    --peer coze --timeout 120 --allow /srv/jobs

# 对方那侧（主动打）
python3 -m multi_agent.signal connect --topic mergen-7f3a91 --id coze \
    --peer openclaw --run /srv/jobs/scan_news.py
```

`wait_punch(peer_ticket)` 传对端的票可做**串台校验**：只接受期待的那个
agent 的 PUNCH，别的直接忽略。

> ⚠️ 但如果对方是**对称 NAT**（同一 socket 问不同 STUN 得到不同公网 IP），
> 它主动打出去的映射我这边永远预测不到 —— 这种拓扑下打洞仍然不通，
> 应该直接走中继。

## 6. 打不通怎么办（对称 NAT / 严格防火墙）

实测发现两种失败模式，都已明确处理：

| 情况 | 表现 | 处理 |
|---|---|---|
| 同 NAT 后 / 全锥 NAT / 端口限制 NAT | `state: direct`，rtt 0~2ms | 正常直连 ✅ |
| 对称 NAT | 两个 STUN 返回不同端口 → `symmetric_hint: true` | 打洞多半失败 |
| 严格防火墙 / UDP 被封 | `punch_timeout` 后 `state: failed` | 明确报"需要中继" |

**降级路径**（已有现成组件）：
- `multi_agent/nat.py` 的 `RendezvousServer` —— 公网 UDP 协调者，打不通时回退中继
- `bin/agent_relay.py`（workspace 里）—— ntfy.sh 公网中继，双方都不需要公网 IP

顺序约定：**先直连（省 token），失败再中继（慢但不挑网络）**。

---

## 7. 自检

```bash
python3 -m pytest tests/test_bootstrap.py -q      # 15 个
python3 -m multi_agent.bootstrap stun             # 看自己能探到公网地址
python3 -m multi_agent.bootstrap ticket --id x    # 出票
```

检查点：
- 门票格式错（缺字段/端口非数字）→ 抛 `ValueError` 并给出正确格式示例
- 打不通 → `ok: false` + `reason` 含"中继"，**不静默成功**
- 直连后 `send()` 有 ack；对端消失时返回 `False`
- 端到端：打洞 → 发 run → 对端执行真实程序 → 结果回来（`test_end_to_end_punch_and_execute_program`）

---

## 8. 安全边界（必读）

打洞省 token 的代价是：**这条链路没有密码学身份**。下面几条不是建议，是使用前提。

1. **门票就是凭证。** `A1|公网IP|公网端口|本地端口|agent_id` 没有签名、没有过期时间。
   拿到它的人可以向你的 socket 打洞、投递消息。只点对点转发，不贴群、不进公开仓库、
   不发朋友圈——转发给谁，就等于把这条通道交给谁。

2. **数据面是明文。** UDP 帧没有加密、没有身份校验（`PUNCH`/`PONG`/`D:` 只是前缀）。
   跨公网使用时，应放在 WireGuard / TLS 隧道或专线里；只在可信内网直接裸跑。

3. **`--exec-on-msg` 是信任开关，不是功能开关。** 默认关闭；开启后对端可以让你在
   本机执行脚本（`bootstrap` 的本地执行没有白名单）。需要"受控执行"请改用
   `runner`（`AGENT_RUN_ALLOW` 白名单、默认拒绝、`shell=False`、强制超时）。

4. **一次性使用。** 票对应的公网端口是 NAT 临时映射，任务跑完就关掉进程（或换 socket），
   映射失效后旧票自动作废；长期挂着等于长期开门。

5. **`RendezvousServer --token` 只防误连，不防窃听。** 它能挡住不知道 token 的 hello，
   但中继转发的载荷仍是明文，别把它当鉴权+加密方案。

需要"可认证 + 加密"的正式通道时，走 Hub 的 `X-Hub-Token` + HTTPS 反向代理，或
`serve --tcp-port` 外面套 TLS 隧道；打洞直连定位是**省 token 的加速通道**，不是安全通道。
