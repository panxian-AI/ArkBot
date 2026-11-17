# 接入 AstrBot：让 AstrBot 的插件、工具、人格跑在 ArkBot 上

> 一句话：AstrBot 用 `aiocqhttp` 平台接入 ArkBot，**AstrBot 侧一行代码不用改**；
> QQ 侧换 NapCat、走官方通道兜底、全通道掉线进发件箱，AstrBot 全都感知不到。
>
> 和 [AI聊天接入.md](AI聊天接入.md) 区分一下方向：
> 那份文档是「ArkBot 的 QQ 消息**送进** AstrBot 的 Agent」；
> 这份是「AstrBot 整个当**上层软件**，通过 OneBot 11 接管 ArkBot 的收发」。
> 两者可以同时开，但别让两边都对同一条消息自动回复（见文末「边界」）。

## 1. 为什么是「反向 WS」

AstrBot 的 `aiocqhttp` 适配器只有一种模式：`CQHttp(use_ws_reverse=True)`，
也就是 **AstrBot 自己当 WebSocket 服务端**，等 OneBot 实现连上来
（`ws_reverse_host` / `ws_reverse_port`，默认 `0.0.0.0:6199`，路径 `/ws`）。

所以接法是：**ArkBot 主动去连 AstrBot**，也就是兼容层配置里的 `reverse_urls`。

```
┌──────────────────────────────────────────┐
│ AstrBot（插件 / LLM / 工具调用 / 人格）    │
│   aiocqhttp 平台，监听 ws://0.0.0.0:6199  │
└──────────────────▲───────────────────────┘
                   │  反向 WS（ArkBot 主动连 /ws）
                   │  X-Self-ID: 机器人QQ号
                   │  X-Client-Role: Universal
                   │  Authorization: Bearer <token>
┌──────────────────┴───────────────────────┐
│ ArkBot  compat/  扮演 OneBot 11 实现      │
├──────────────────────────────────────────┤
│ ArkBot  插件 / 事件 / 调度 / 离线发件箱     │
├──────────────────────────────────────────┤
│ adapters/  NapCat ⇄ 官方 QQ ⇄ 任意通道     │
└──────────────────────────────────────────┘
```

## 2. 三步接上

### ① AstrBot 侧：加一个 aiocqhttp 平台

WebUI → 配置 → 消息平台 → 新增 `aiocqhttp`：

| 字段 | 填什么 | 说明 |
|---|---|---|
| `ws_reverse_host` | `127.0.0.1` | 只给本机 ArkBot 连就收窄到回环；容器里给 `0.0.0.0` |
| `ws_reverse_port` | `6199` | 默认值，和下面 ArkBot 的 URL 对上即可 |
| `ws_reverse_token` | 留空，或自己定一个 | 非空时 AstrBot 会校验 `Authorization: Bearer`；留空则只靠回环地址保护 |

### ② ArkBot 侧：`bot.toml`

```toml
[compat]
enabled = true
host = "127.0.0.1"
port = 6800                    # 正向 WS / HTTP API 用，AstrBot 用不到，但开着没坏处
access_token = ""              # 必须与 AstrBot 的 ws_reverse_token 完全一致（两边都空也行）
heartbeat_interval = 30

# 反向 WS 目标：AstrBot 的 aiocqhttp 端口
reverse_urls = ["astrbot://127.0.0.1:6199"]
# 等价写法：reverse_urls = ["ws://127.0.0.1:6199/ws"]

# 强烈建议显式填机器人 QQ 号（详见第 4 节坑 ③）
self_id = 123456789
nickname = "方舟"
```

`astrbot://host:port` 是给这个场景准备的简写，会自动补成 `ws://host:port/ws`
—— `aiocqhttp` 的 Universal 角色只认 `/ws` 这个路径，写全 URL 反而容易漏。

### ③ 启动顺序

先起 AstrBot（它得先把 6199 监听起来），再起 ArkBot：

```bash
python -m arkbot                  # 或 arkbot / arkbot2，两套内核都读同一段 [compat]
```

ArkBot 日志里应当出现：

```
OneBot 11 兼容层已就绪：ws://127.0.0.1:6800/  http://127.0.0.1:6800/
反向 WS 目标: ws://127.0.0.1:6199/ws
反向 WS 已连接: ws://127.0.0.1:6199/ws
```

AstrBot 侧对应一行：`aiocqhttp(OneBot v11) 适配器已连接。`

顺序反了也没关系：ArkBot 会指数退避重连（上限 60s），AstrBot 起来后自动接上。

## 3. AstrBot 每条消息会反问什么，ArkBot 怎么答

这一步是「兼容」的真正难点。AstrBot 不是被动收事件，它在处理一条消息的**同一时刻**
会**同步反查**若干 OneBot 接口，查不到就丢内容甚至丢整条消息。

| AstrBot 的动作 | 触发时机 | ArkBot 的应答 |
|---|---|---|
| `get_group_member_info`<br>`get_stranger_info` | **每条带 @ 的群消息**（问「被 @ 的是谁」） | 先查本地（从真实事件里学到的名片/昵称）→ 再透传下游 → 都没有就给结构合法的空档案，**不报错** |
| `get_msg` | 收到 `reply` 消息段（引用回复） | 先查本地缓存（自己发的 + 刚收到的），查不到再透传，最后给 `{}` |
| `send_group_msg` / `send_private_msg` | 回消息 | 透传给当前最健康的通道；全掉线时进发件箱并照常返回 `retcode=0` + 消息 ID |
| `get_group_info` / `get_group_member_list` | 会话管理、群成员列表 | 透传下游；下游不支持返回 `1404`（AstrBot 会降级，不影响聊天） |
| `send_group_forward_msg` | 合并转发 | 透传；官方通道不支持时 `1404` |
| `.handle_quick_operation_async` | 事件处理函数返回 dict 时 aiocqhttp 回灌 | 空操作，`retcode=0` |

## 4. 三个真实踩过的坑（都已修，写下来免得再踩）

### 坑 ① 群里 @ 机器人，AstrBot 完全没反应

AstrBot 的 `_convert_handle_message_event` 处理 `at` 段时会 `await get_group_member_info(...)`。
下游是 NapCat 时一切正常；但下游换成**官方 QQ 通道**（就是 ArkBot 抗 ban 的兜底路径），
这个接口不存在 → 返回 `1404` → AstrBot 的 handler 抛 `ActionFailed` → **整条消息被丢弃**。

ArkBot 的处理：成员信息「本地优先、下游兜底、最后降级」——
从真实收到的事件里学名片（`sender.card` / `sender.nickname`），
问不到就返回 `{user_id, nickname: "", card: "", role: "member", ...}` 这种
字段齐全、值未知的结构。AstrBot 拿到后能把 @ 段完整保留，`is_at_self` 判断不受影响。

> 注意：这只是「不知道名字」，不是编造身份。真实见过的数据才会被缓存。

### 坑 ② 引用回复变成了纯文本

AstrBot 收到 `reply` 段会拿 `message_id` 回调 `get_msg`，再用返回值
`Event.from_payload` 重建被引用的消息。ArkBot 原来只缓存**自己发出的**消息，
所以「引用别人说的话」必然查不到：`Event.from_payload` 拿到空数据 → 只留一个裸 `reply`，
AI 看不到被引用的内容。

修法：收到的每条消息也进缓存（上限 500 条，LRU 淘汰），`get_msg` 先查本地。

### 坑 ③ `self_id` 是 0，@ 判定全废

`self_id` 有两个用途：
1. 作为反向 WS 的 `X-Self-ID` 头，`aiocqhttp` 用它做 **API 路由键**（多账号）；
2. AstrBot 用它判断「这个 @ 是不是在 @ 我」（`is_at_self`）。

如果 ArkBot 还没拿到登录号（下游没连上就启动了兼容层），`self_id` 就是 0，
群里 @ 机器人会一直被当成「@ 了别人」。

修法：兼容层启动时若 `self_id` 未知，会向下游问一次 `get_login_info` 补上；
仍然拿不到就在日志里明确告警。生产环境建议直接在 `[compat] self_id` 写好。

### 顺带修掉的两个小坑

- **`self_id` / `no_cache` 污染下游**：`aiocqhttp` 每个动作都会带 `self_id`，
  AstrBot 还会带 `no_cache`。这两个字段 OneBot 规范里没有，透传下去会让下游适配器
  `TypeError`。现在统一在下发前剔掉。
- **串行应答导致的连带超时**：ArkBot 原来在反向 WS 循环里 `await` 每条 API，
  一条慢发送会把后面的请求全卡住（`aiocqhttp` 的 `api_timeout` 是 180s，
  够它连锁超时）。现在每条请求单独建任务。

## 5. 验证

不装 AstrBot 也能验：仓库里有一份**真实 `aiocqhttp` 当对端**的端到端测试。

```bash
pip install aiocqhttp quart          # 真实协议对端，只测这个用例需要
python tests/e2e/astrbot_test.py     # 18 项断言，全绿说明 AstrBot 那条链路是通的
```

它验的不是「ArkBot 自己觉得对」，而是完整链路：

```
真实 aiocqhttp(CQHttp(use_ws_reverse=True))
   ▲ 反向 WS        ← 握手头 / echo{seq} 应答配对 / X-Self-ID 路由，全由库自己校验
ArkBot 兼容层
   │ OneBot 11 正向 WS
   ▼
假 OneBot 实现（扮演 NapCat）
```

断言覆盖：握手被接受、self_id 正确、`message` 是数组、`sender` 可下标、
@ 段不被丢弃、引用消息可回查、`poke` 通知可解析、慢请求不阻塞后续、AstrBot 侧零转换异常。

没装 `aiocqhttp` 时该脚本会打印 SKIP 并以 0 退出，不影响 CI。

## 6. 排障

| 现象 | 原因 / 处理 |
|---|---|
| ArkBot 日志反复「反向 WS ... 断线/失败」 | 端口不对或 AstrBot 没起；确认 AstrBot 的 `ws_reverse_port` 与 `reverse_urls` 一致 |
| 连上立刻断开 | `access_token` 与 `ws_reverse_token` 不一致（`aiocqhttp` 回 401/403） |
| AstrBot 显示已连接，但收不到群消息 | 看 ArkBot 日志有没有「已连接」；再确认下游通道本身在收事件 |
| 群里 @ 机器人不回 | ① `[compat] self_id` 是否为真实 QQ 号；② AstrBot 侧触发方式是否设成「需要 @」 |
| 引用回复丢内容 | 被引用的消息早于 ArkBot 启动（不在 500 条缓存窗口内），属预期降级 |
| 同一条消息回两遍 | ArkBot 的 `plugins/` 里也有自动回复插件，和 AstrBot 撞了（见下） |
| 中文乱码 | 请求体必须 UTF-8 JSON；`Content-Type: application/json` |

## 7. 边界

1. **别让两边同时自动回复**。事件会同时给 ArkBot 插件和 AstrBot。
   迁移期二选一：关掉 `plugins/` 里对应的自动回复插件，或者把 `[compat] enabled` 当开关。
2. **AstrBot 只能当服务端**。反过来「AstrBot 监听、ArkBot 当服务端」不成立；
   要走正向 WS 的是 NoneBot2 / Koishi，见 [OneBot兼容层.md](OneBot兼容层.md)。
3. **通知类事件依赖下游**。官方 QQ 目前只翻译群 @ 与私聊消息，
   `group_increase`、`group_ban`、`poke` 这些只有 OneBot 实现在线时才有。
4. **多客户端共存**。AstrBot 可以和面板 / 监控同时接进来（正向 WS 那个端口），
   事件广播给所有人，发送各自独立。
5. **AstrBot 的流式输出走它自己的会话接口**，不经过 OneBot，这里不涉及。
