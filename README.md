# ArkBot（方舟）🚀

> 桥被 ban 了就上方舟 —— 抗封禁、可插拔适配器的 QQ 机器人框架。

## 为什么是 ArkBot

QQ 机器人生态的现实：协议实现（OneBot 系：NapCat、Lagrange、LLOneBot、go-cqhttp……）
随时可能因为官方风控失效或项目停摆。如果业务逻辑和「连 QQ 的方式」绑死，
每一次封禁都意味着重写机器人。

ArkBot 的答案是**异构多通道 + 无感接管**：

```
┌───────────────────────────────────────────────┐
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

**用户体验无感**是硬性验收标准（见 `tests/failover_test.py`）：
同一条命令在两个通道行为完全一致，通道切换对 QQ 用户不可见。

## ArkBot NG —— 全新微内核（`arkbot2/`）

基于 2026-11 依赖调研（`python -m arkbot2 --capabilities` 可查看候选清单）重写的运行时，
**plugins/ 目录下原有插件零改动可用**（`tests/arkbot2_compat_test.py` 18/18 验证）。

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
python -m arkbot2                  # 按 bot.toml 启动（支持 [adapter] 与 [[channels]]）
python -m arkbot2 --capabilities   # 依赖候选清单
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
python tests/e2e_test.py        # 单通道（OneBot）全链路
python tests/failover_test.py   # 双通道抗 ban：OneBot 被 ban → 官方通道无感接管
```

failover 测试模拟真实事故链：OneBot 实现在线服务 → 瞬间死亡 →
用户从官方通道发同一命令 → 回复正常送达 → OneBot 恢复后自动重连、
发件箱消息自动补发。

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
└── adapters/
    ├── base.py       # ★ 适配器抽象接口 + 回复路由契约
    ├── onebot11.py   # OneBot 11 正向 WS 适配器
    └── official_qq.py# QQ 官方开放平台适配器（封禁兜底）
plugins/              # 你的插件
tests/                # 假服务端 + 单通道/双通道端到端测试
```

## 写一个新适配器（例如未来的 native-qq）

```python
class NativeQQAdapter(Adapter):
    async def connect(self): ...      # 连 QQ，收到事件包装成 Event 后 _emit()
    async def call_api(self, action, params): ...  # 返回与 OneBot 同构的 data
```

在 `Config.build_adapter()` 注册即可。插件世界毫无波澜。
