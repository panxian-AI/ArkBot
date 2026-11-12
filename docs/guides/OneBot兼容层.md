# 兼容层：让「以 OneBot 为底层」的软件直接接入

> 一句话：ArkBot 以前只会**当客户端**（连 NapCat / Lagrange）；
> 现在它还能**当一个 OneBot 11 实现**——NoneBot2、Koishi、AstrBot、HoshinoBot、
> 群里跑了几年的自研脚本，**一行代码都不用改**就能接上来。

## 1. 两种「兼容」别搞混

| | 兼容什么 | 谁不用改 | 代码 |
|---|---|---|---|
| **业务兼容** | ArkBot 自己的旧插件 | `plugins/` 里的 `class Xxx(Plugin)` | `arkbot2/legacy.py`（影子导入） |
| **软件兼容**（本文） | 以 OneBot 为协议底层的**外部软件** | NoneBot2 / Koishi / AstrBot / 老脚本 | `arkbot/compat/` |

软件兼容解决的是另一个痛点：你有一整套跑得好好的上层生态（插件、面板、管理后台），
它们只认「OneBot 11 接口」。QQ 侧怎么接、明天换不换实现，跟它们无关。

## 2. 链路

```
   NoneBot2 / Koishi / AstrBot / 自研脚本 / 任何 OneBot SDK
                    │   OneBot 11：正向 WS、反向 WS、HTTP API、HTTP 上报
                    ▼
   ┌───────────────────────────────────────────┐
   │  arkbot/compat/  OneBot 11 兼容层（服务端） │  ← 本模块
   ├───────────────────────────────────────────┤
   │  arkbot/         事件·规则·消息链·调度      │
   ├───────────────────────────────────────────┤
   │  arkbot/adapters/  多通道（客户端）          │
   │   ├─ onebot11   → NapCat / Lagrange / ...   │
   │   └─ official_qq → QQ 官方开放平台           │
   └───────────────────────────────────────────┘
```

- **事件**：下游通道收到 QQ 消息 → ArkBot 分发（自家插件照常）→ 同时广播给兼容层客户端。
- **发送**：兼容层客户端调 `send_msg` → `bot.send_*` → 走当前最健康的通道 → 回传 `message_id`。
- **抗 ban 能力原样继承**：换实现、官方兜底、离线发件箱，上层软件完全感知不到。

## 3. 开启

```toml
[compat]
enabled = true
host = "127.0.0.1"        # 要给别的机器/容器连就写 0.0.0.0
port = 6800
access_token = ""          # 建议设一个，OneBot 生态通用鉴权
heartbeat_interval = 30    # 心跳间隔（秒），0 = 关闭
# reverse_urls = ["ws://127.0.0.1:6199/ws"]     # 反向 WS：主动连到对方
# http_report_urls = ["http://127.0.0.1:8080/onebot/event"]  # HTTP 上报
```

环境变量也可以：`ARKBOT_COMPAT_PORT`、`ARKBOT_COMPAT_TOKEN`。
`arkbot` 与 `arkbot2` 两套内核都读同一段配置。

## 4. 四种接法

### 4.1 正向 WS（最常用）

ArkBot 监听，对方连上来。地址就是 `ws://<host>:<port>/`，
`/event`、`/api`、`/ws`、`/onebot/v11/ws` 等路径全部接受（OneBot 实现们的通行做法）。

**NoneBot2**（OneBot v11 适配器，正向模式）——`.env`：

```dotenv
DRIVER=~fastapi
ONEBOT_WS_URLS=["ws://127.0.0.1:6800/"]
```

**Koishi**（`onebot` 适配器）：WS 地址填 `ws://127.0.0.1:6800/`，token 与 `access_token` 一致。

**自研脚本**：

```python
import asyncio, json, websockets

async def main():
    async with websockets.connect(
        "ws://127.0.0.1:6800/",
        additional_headers={"Authorization": "Bearer 你的token"},
    ) as ws:
        print(await ws.recv())                      # lifecycle 元事件
        await ws.send(json.dumps({
            "action": "send_group_msg",
            "params": {"group_id": 123456, "message": "早上好"},
            "echo": "1",
        }))
        print(await ws.recv())                      # {"retcode":0,"data":{"message_id":...}}

asyncio.run(main())
```

### 4.2 反向 WS

对方是服务端（AstrBot 的 aiocqhttp、NoneBot2 的反向模式），让 ArkBot 去连：

```toml
[compat]
enabled = true
port = 6800
reverse_urls = ["ws://127.0.0.1:6199/ws"]
```

握手会带 `X-Self-ID` / `X-Client-Role: Universal`，token 走 `Authorization: Bearer`。
断线自动指数退避重连（上限 60s）。

### 4.3 HTTP API

同一个端口，`POST /<action>`（JSON 体可为 `params` 平铺或 `{"params": {...}}`）：

```bash
curl -s http://127.0.0.1:6800/send_group_msg \
  -H 'Authorization: Bearer 你的token' -H 'Content-Type: application/json' \
  -d '{"group_id":123456,"message":"[CQ:at,qq=999] hi"}'
# {"status":"ok","retcode":0,"data":{"message_id":900002}}
```

`GET /` 可直接探活，返回 `app_name/self_id/clients/events`。
老 SDK 的 `GET /get_group_list?access_token=xxx` 写法也支持。

### 4.4 HTTP 上报

不适合长连接的环境（Serverless、PHP 后台），把事件 POST 出去：

```toml
http_report_urls = ["http://127.0.0.1:8080/onebot/event"]
```

请求头同样带 `X-Self-ID` / `X-Client-Role` 与 `Authorization`。

## 5. API 覆盖

**本地直接应答**（不依赖下游通道）：

| 动作 | 返回 |
|---|---|
| `get_login_info` | `{user_id, nickname}`（self_id 取当前登录号） |
| `get_status` | `{online, good}` |
| `get_version_info` | `{app_name: "ArkBot", protocol_version: "v11"}` |
| `can_send_image` / `can_send_record` | `{yes: true}` |
| `get_csrf_token` / `get_cookies` | 占位值（官方通道无此概念） |
| `set_restart` / `clean_cache` / `handle_quick_operation` | 空操作 |
| `send_msg` / `send_group_msg` / `send_private_msg` | `{message_id}` |
| `get_msg` | 优先回本地发送记录（发件箱场景拿不到下游回执） |
| `delete_msg` | 透传下游；通道不支持时 `retcode=1404` |

**全部透传**给下游通道：`get_group_list`、`get_group_info`、`get_group_member_list`、
`get_group_member_info`、`get_friend_list`、`get_stranger_info`、`set_group_ban`、
`set_group_kick`、`set_group_card`、`set_group_name`、`set_group_admin`、
`set_group_whole_ban`、`set_friend_add_request`、`set_group_add_request`、
`send_like`、`get_forward_msg`、`friend_poke`、`group_poke` ……

语义约定：

- 下游是 OneBot 实现 → 原生支持什么就支持什么；
- 下游是官方 QQ → 官方适配器尽力翻译，不支持的动作返回 `1404`（不是崩溃）；
- 没有任何通道在线时：`send_*` 返回 `retcode=0` + 本地 `message_id`，消息进离线发件箱；
  查询类动作返回 `retcode=100`（明确告知暂时不可用，不假成功）。

## 6. 事件长什么样

```json
{
  "time": 1762900000,
  "self_id": 10000,
  "post_type": "message",
  "message_type": "group",
  "sub_type": "normal",
  "message_id": 321,
  "group_id": 654321,
  "user_id": 999,
  "message": [{"type": "at", "data": {"qq": "10000"}},
              {"type": "text", "data": {"text": " 在吗"}}],
  "raw_message": "[CQ:at,qq=10000] 在吗",
  "font": 0,
  "sender": {"user_id": 999, "nickname": "小明", "card": "", "role": "member"}
}
```

- `message` 一律是**数组**格式（下游给的是 CQ 字符串也会转换），`raw_message` 保留 CQ 原文；
- `self_id` 用当前登录号归一，不会出现 0；
- `notice` / `request` / `meta_event` 原样透传，内部字段（`_official` 等）自动过滤；
- 连上即发 `lifecycle(connect)`，之后按 `heartbeat_interval` 推心跳。

## 7. 边界与注意事项

1. **别让插件和上层软件同时回一条消息**。事件会同时给 ArkBot 插件和兼容层客户端，
   两边都配了自动回复就会双份。迁移期建议：要么关掉 `plugins/` 里对应的自动回复，
   要么把 `[compat] enabled` 当开关切。
2. **`message_id` 有两种**：下游给了真实回执就用真实的；走离线发件箱时给本地唯一 ID
   （`1_000_000_000+`），此时 `delete_msg` / `get_msg` 只能命中本地缓存。
3. **通知类事件依赖下游通道**。官方 QQ 目前只翻译群 @ 与私聊消息，
   `group_increase`、`group_ban` 等只有 OneBot 实现在线时才有。
4. **多客户端并发**：可以同时接多个（面板 + 机器人 + 监控），事件广播给所有人，
   发送各自独立。鉴权 token 是全局一个。
5. **端口对外**时务必设 `access_token`，并只监听需要的网卡（默认 `127.0.0.1`）。

## 8. 排障

| 现象 | 原因 / 处理 |
|---|---|
| 连上立刻断开 | token 不匹配（关闭码 1008）；检查 `Authorization` 或 `?access_token=` |
| 收到 `retcode: 1404` | 该动作下游通道不支持（例如官方通道撤回）。换 OneBot 实现或忽略 |
| 收到 `retcode: 100` | 当前没有任何 QQ 通道在线；等通道恢复，或看 ArkBot 日志 |
| 事件不来 | 下游实现没连上：`ArkBot` 日志里应有「已连接 …」；或通知类事件本身不被官方通道翻译 |
| 发送成功但群里没消息 | 走的是离线发件箱，检查是否所有通道都掉线（`bot.toml` 日志有「已入离线发件箱」） |
| 中文乱码 | 请求体必须是 UTF-8 JSON；`Content-Type: application/json` |

## 9. 代码与测试

```
arkbot/compat/
├── ws.py         # 同端口 HTTP + WebSocket 服务端（标准库 asyncio 手写）
└── onebot11.py   # 协议层：鉴权 / 事件广播 / API 分发 / 反向 WS / HTTP 上报
```

```bash
python tests/e2e/compat_test.py          # 全链路：上层软件 → ArkBot → 假 OneBot 实现
pytest tests/unit/test_compat_onebot.py -q   # 握手、鉴权、事件格式、HTTP 语义
```

`compat_test.py` 里没有任何协议桩：上层用真实 `websockets` 客户端，
下游用真实 OneBot 实现（`tests/fakes/fake_onebot.py`），中间是真实的 ArkBot。
