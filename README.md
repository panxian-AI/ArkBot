# ArkBot（方舟）🚀

> 桥被 ban 了就上方舟 —— 抗封禁、可插拔适配器的 QQ 机器人框架。

[![CI](https://github.com/panxian-AI/ArkBot/actions/workflows/ci.yml/badge.svg)](https://github.com/panxian-AI/ArkBot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## 为什么是 ArkBot

QQ 机器人生态的现实：协议实现（OneBot 系：NapCat、Lagrange、LLOneBot、go-cqhttp……）
随时可能因为官方风控失效或项目停摆。如果业务逻辑和「连 QQ 的方式」绑死，
每一次封禁都意味着重写机器人。

ArkBot 的答案是**异构多通道 + 无感接管**：

```
┌───────────────────────────────────────────────┐
│  上层兼容  compat/     ★ 对外扮演 OneBot 实现 ★ │  NoneBot2 / Koishi / AstrBot
├───────────────────────────────────────────────┤
│  插件层  plugins/         业务逻辑，永不改动      │
├───────────────────────────────────────────────┤
│  核心层  arkbot/           事件·规则·消息链·调度  │  平台无关
├───────────────────────────────────────────────┤
│  适配层  adapters/        ★ 多通道并行 ★       │
│   ├─ onebot11   → NapCat / Lagrange / LLOneBot │
│   ├─ official_qq → QQ 官方开放平台（封禁兜底）   │
│   └─ (预留 native-qq：直连 NTQQ 的接口位)       │
└───────────────────────────────────────────────┘
```

三层防线，逐级兜底：

1. **换实现**：NapCat 被 ban？改一行 `url` 换 Lagrange，插件零改动。
2. **异构兜底**：QQ 修掉 OneBot 实现依赖的逆向协议（OneBot 系全灭）？
   `official_qq` 通道走 QQ 官方机器人开放平台——不依赖任何逆向，
   QQ 不可能封掉自己的官方 API。同一份插件代码继续服务。
3. **离线发件箱**：极端情况所有通道同时不可用，定时任务消息入队暂存
   （10 分钟 TTL），通道恢复后自动补发——用户「延迟收到」而不是「丢失」。
4. **主动消息不中断**：官方通道支持不带 `msg_id` 的主动发送（内置
   30/qpm 限速，超限自动降级到其他通道或发件箱补发）；openid ↔ 数字 ID
   自动映射，插件层照旧用数字 group_id/user_id 调用，零改动。
5. **持久化**：发件箱与 openid 映射落盘（SQLite，纯标准库，见
   `arkbot/persist.py`）——进程重启后未补发的消息自动恢复、官方通道
   无需等新事件即可向见过的群/用户主动发消息；磁盘不可用时自动退化为
   内存模式，机器人照常跑。数据目录由 `[bot] data_dir` 配置（默认 `data/`）。

**用户体验无感**是硬性验收标准（见 `tests/e2e/failover_test.py`）：
同一条命令在两个通道行为完全一致，通道切换对 QQ 用户不可见。

## 兼容层：现成的 OneBot 软件直接接入（`arkbot/compat/`）

抗 ban 管的是「怎么连 QQ」；这一层管**「已经写好的东西怎么继续用」**。

ArkBot 原来只是 OneBot **客户端**（连 NapCat）。现在它还能**扮演一个 OneBot 11 实现**：
NoneBot2、Koishi、AstrBot、HoshinoBot、群里跑了三年的自研脚本，
**换一行连接地址就接上来，业务代码零改动**。

```toml
[compat]
enabled = true
host = "127.0.0.1"
port = 6800
access_token = "your-token"
heartbeat_interval = 30
# reverse_urls = ["ws://127.0.0.1:6199/ws"]                    # 反向 WS（AstrBot / NoneBot 反向）
# http_report_urls = ["http://127.0.0.1:8080/onebot/event"]    # HTTP 上报
```

四种接法，一个端口全包：

| 接法 | 谁连谁 | 典型用户 |
|---|---|---|
| 正向 WS | 对方连 `ws://host:port/` | NoneBot2 正向、Koishi、自研脚本 |
| 反向 WS | ArkBot 连对方的 `/ws` | AstrBot(aiocqhttp)、NoneBot2 反向 |
| HTTP API | `POST /send_msg` 等 | curl、各种 SDK、面板 |
| HTTP 上报 | ArkBot POST 事件到 URL | Serverless、PHP 后台 |

链路始终是：**上层软件 → 兼容层 → 统一事件/API → 当前最健康的 QQ 通道**。
所以底层换实现、走官方兜底、甚至全通道掉线进发件箱，上层软件都感知不到
（`send_*` 照常返回 `retcode=0` + 消息 ID，恢复后自动补发）。

细节、API 覆盖表、NoneBot2/Koishi/AstrBot 的具体配置、排障：
**[docs/guides/OneBot兼容层.md](docs/guides/OneBot兼容层.md)**。

## ArkBot NG —— 全新微内核（`arkbot2/`）

基于 2026-11 依赖调研（`python -m arkbot2 --capabilities` 可查看候选清单）重写的运行时，
**plugins/ 目录下原有插件零改动可用**（`tests/e2e/arkbot2_compat_test.py` 18/18 验证）。

NG 内核新能力：

| 能力 | 说明 |
|------|------|
| 结构化并发 | 非消息事件用 `asyncio.TaskGroup` 并发分发，监听器互不阻塞 |
| 中间件管道 | 事件先穿 middleware 链再进匹配器，可做限流 / 审计 / 改写 |
| 依赖注入 | 新式处理器按类型注解拿依赖：`async def h(bot: Bot, ev: MessageEvent, state: dict)` |
| entry_points 插件 | pip 包声明 `[project.entry-points."arkbot2.plugins"]` 即可被自动发现 |
| 旧插件兼容 | 影子导入：`from arkbot import Plugin` 自动切换到 NG 内核注册 |

兼容机制（`arkbot2/legacy.py`）：NG 启动时先把真实 `arkbot` 包作为传输层正常导入
（构建通道），随后把 `sys.modules['arkbot']` 换成影子模块——旧插件 import 到的
`Plugin` / 规则函数注册进 NG 内核而非旧分发器，而消息链、事件模型、适配器等
稳定组件原样复用。**内核换了，桥还在。**

```bash
pip install .                      # 源码安装（或 pip install arkbot）
arkbot2                            # 按 bot.toml 启动（支持 [adapter] 与 [[channels]]）
arkbot2 --capabilities             # 依赖候选清单
# 或模块方式：python -m arkbot2
```

旧式 `(event, state)` 处理器与 NG 式 DI 处理器可在同一插件中混用。

## 快速开始

```bash
pip install websockets        # 唯一依赖
python -m arkbot              # 读取 bot.toml 并运行
```

1. 启动任意 OneBot 11 实现并打开正向 WS（默认 6700 端口）；
2. 配置 `bot.toml` 的 `[adapter]` 段；
3. 把插件丢进 `plugins/` 目录。

## 上层：接上 AstrBot 与 DSH「养鱼」

底层解决「怎么连上 QQ」，上层解决「跟谁聊」。`plugins/ai_chat.py` + `arkbot/integrations/chat.py`
把外部 Agent 接成一个可切换的聊天后端——**同一份代码，`/ai backend fish` 就换一个灵魂**。

```toml
[plugins.ai_chat]
default_backend = "astr"
wake_words = ["大鱼", "肥鱼"]     # 群里提到就自动接话（养鱼模式）

# AstrBot：走它内建的 Agent（插件、工具调用、知识库、人格全部生效）
[plugins.ai_chat.backends.astr]
type = "astrbot"
base_url = "http://127.0.0.1:6185"
api_key = "abk_xxx"               # WebUI → 设置 → API Key（需 chat scope）

# DSH 养鱼：mode = aurbot / bridge / openai 三选一
[plugins.ai_chat.backends.fish]
type = "dsh"
mode = "aurbot"
base_url = "http://127.0.0.1:8770"
```

QQ 里直接用：

| 命令 | 作用 |
|------|------|
| `/ai 你好` | 和当前后端聊一句 |
| `/ai backend fish` | 当前会话切到 DSH（别名 `dsh`/`大鱼`/`养鱼`） |
| `/ai on` / `/ai off` | 开关本群养鱼模式（被 @ 或唤醒词自动接话） |
| `/ai reset` | 开新会话 |
| `/ai who` | 看会话号 / 后端 / 冷却状态 |

细节（三种 DSH 模式怎么选、会话语义、排障）见 **[docs/guides/AI聊天接入.md](docs/guides/AI聊天接入.md)**。

## 写一个插件

```python
from arkbot import MessageSegment, Plugin

class Echo(Plugin):
    name = "echo"

    async def onLoad(self):
        @self.on_command("echo", "复读")
        async def echo(event, state):
            await event.reply(state["args"])

        @self.on_command("kick", rule=only_admin() & only_group())
        async def kick(event, state): ...

        @self.on_notice("group_increase")
        async def welcome(event): ...

        @self.schedule_cron(minute="0", hour="8")
        async def morning(): ...
```

### 能力一览

| 能力 | 用法 |
| --- | --- |
| 命令 | `@self.on_command("签到")` → `state["args"]` |
| 前缀 / 关键词 / 正则 | `on_startswith` / `on_keyword` / `on_regex` |
| 规则组合 | `only_admin() & only_group() & from_user(123)` |
| 通知/请求/元事件 | `on_notice("group_increase")` / `on_request("friend")` / `on_meta("heartbeat")` |
| 定时任务 | `schedule_every(60)` / `schedule_cron(minute="0", hour="8")` |
| 消息链 | `MessageSegment.image(...) + MessageSegment.text(...)` |
| 快捷回复 | `await event.reply(msg, at_sender=True, quote=True)` |

## 端到端验证

```bash
pytest tests/unit -q                    # 109 项单测（消息/规则/调度/持久化/聊天集成/兼容层/适配器加载器）

python tests/e2e/e2e_test.py            # 单通道（OneBot）全链路
python tests/e2e/failover_test.py       # 双通道抗 ban：OneBot 被 ban → 官方通道无感接管
python tests/e2e/compat_test.py         # 上层 OneBot 软件 → ArkBot → 下游 OneBot 实现
python tests/e2e/arkbot2_compat_test.py # 旧插件零改动跑在新内核
python tests/e2e/persist_test.py        # 持久化：重启后发件箱与 openid 映射恢复
python tests/e2e/ai_chat_test.py        # AstrBot / DSH 聊天链路（含 SSE 拼接、养鱼模式、冷却、分段）
```

全部测试使用假服务端（`tests/fakes/`），**不需要真实 QQ 号、不需要连外网**。
脚本自带 `sys.path` 引导，所以在仓库根目录直接 `python tests/e2e/xxx.py` 即可。

failover 测试模拟真实事故链：OneBot 实现在线服务 → 瞬间死亡 →
用户从官方通道发同一命令 → 回复正常送达 → OneBot 恢复后自动重连、
发件箱消息自动补发。

## 文档

| 文档 | 内容 |
|------|------|
| [docs/guides/OneBot兼容层.md](docs/guides/OneBot兼容层.md) | 让 NoneBot2 / Koishi / AstrBot / 老脚本直连 ArkBot：四种接法、API 表、排障 |
| [docs/guides/AI聊天接入.md](docs/guides/AI聊天接入.md) | AstrBot / DSH 养鱼接入、命令、配置全表、自己加后端、排障 |
| [docs/guides/共创指南.md](docs/guides/共创指南.md) | 分层契约、代码地图、插件/适配器写法、测试与 PR 清单、good first issue |
| [bot.toml.example](bot.toml.example) | 所有配置项的注释示例 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 最短贡献路径：环境、自测命令、改动放哪、PR 约定 |

## 协议外挂层：[ArkLink](https://github.com/panxian-AI/ArkLink)

内核不绑定任何协议。第三方仓库通过 **entry point** 或**点分路径**把新协议挂进来，
插件一行不改：

```toml
[[adapters]]
type = "milky"                              # pip install arklink 后即可用
base_url = "http://127.0.0.1:3000"
access_token = "xxx"

# 或者不装包，直接指模块
# type = "arklink.milky:MilkyAdapter"
```

ArkLink 已实现：**Milky 1.3**（NapCat / Lagrange / Acidify）、
**Satori v1**（Koishi / Chronocat）、以及 **QQ NT 原生协议的逆向工具箱**
（TEA / SSO 封包 / protobuf / qsign，研究态）。

加载规则在 `arkbot/adapters/loader.py`：内置类型 → entry point group
`arkbot.adapters` → `"pkg.mod:Class"`，都不匹配时给出可用列表与写法提示。

## 目录结构

```
arkbot/
├── app.py            # 应用入口与生命周期（多通道并行）
├── config.py         # bot.toml + 环境变量（[[adapters]] 多通道）
├── bot.py            # 平台无关 API 门面 + 通道路由 + 离线发件箱
├── event.py          # 统一事件模型（事件带来源通道）
├── message.py        # 消息链 / CQ 码互转
├── dispatch.py       # 规则引擎与事件分发
├── plugin.py         # 插件基类与管理器
├── scheduler.py      # every/cron 调度
├── persist.py        # SQLite KV：发件箱 / openid 映射落盘
├── integrations/
│   └── chat.py       # ★ 上层聊天集成（AstrBot / DSH / OpenAI 兼容）
├── compat/
│   ├── ws.py         # 同端口 HTTP + WebSocket 服务端（标准库）
│   └── onebot11.py   # ★ 兼容层：对外扮演 OneBot 11 实现
└── adapters/
    ├── base.py       # ★ 适配器抽象接口 + 回复路由契约
    ├── loader.py     # ★ 适配器加载器（内置 / entry point / 点分路径）
    ├── onebot11.py   # OneBot 11 正向 WS 适配器
    └── official_qq.py# QQ 官方开放平台适配器（封禁兜底）
arkbot2/              # NG 微内核（旧插件零改动兼容）
plugins/              # 插件（echo / groupkit / status / ai_chat）
docs/
└── guides/           # 接入文档：OneBot 兼容层 / AI 聊天 / 协议与共创指南
tests/
├── unit/             # pytest 单测（纯逻辑）
├── e2e/              # 链路脚本（假服务端 + 全流程断言）
└── fakes/            # 假 OneBot / 官方 QQ / 聊天后端
tools/                # 维护脚本（无 workflow scope 时的 API 推送）
```

## 写一个新适配器

两条路，都不需要改内核：

1. **同一个仓库**：继承 `Adapter`，然后在 `adapters/loader.py` 里 `register("你的名字", 工厂)`；
2. **独立仓库**（推荐，参考 ArkLink）：声明 entry point

```toml
[project.entry-points."arkbot.adapters"]
my-proto = "my_pkg.adapter:create"
```

适配器只需要实现两件事：`connect()` 里把协议事件翻译成统一事件后 `_emit()`，
`call_api()` 里把 OneBot 动作名映射到你的协议（不支持的抛 `1404`）。

在 `Config.build_adapter()` 注册即可。插件世界毫无波澜。
