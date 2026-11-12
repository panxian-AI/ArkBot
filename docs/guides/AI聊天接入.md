# 让 ArkBot 接上 AstrBot 与 DSH「养鱼」

> 本文说明 ArkBot 的**上层聊天集成**：怎么把外部 Agent（AstrBot、DSH 大肥鱼、任意 OpenAI 兼容模型）接进 QQ，
> 以及怎么自己加一个后端。代码在 `arkbot/integrations/chat.py` + `plugins/ai_chat.py`。

---

## 1. 它在哪一层

```
┌──────────────────────────────────────────────┐
│ 插件层  plugins/ai_chat.py   ← 编排：会话/触发/冷却/分段 │
├──────────────────────────────────────────────┤
│ 集成层  arkbot/integrations/chat.py          │  ← 文本进、文本出，不碰协议
│   AstrBotBackend / DshBackend / ChatHub       │
├──────────────────────────────────────────────┤
│ 框架核心 事件·规则·消息链·调度                  │
├──────────────────────────────────────────────┤
│ 适配器层 onebot11 / official_qq（可插拔）      │
└──────────────────────────────────────────────┘
```

关键约定：**集成层只认「一段文本 → 一段文本」**。

- 它不知道消息来自 QQ 还是别的平台；
- 它不关心 OneBot 被 ban 还是走官方通道；
- 换模型、换 Agent、换人格，都只影响这一个文件里的后端实现。

这样「抗封禁」和「接 Agent」互不干扰：底层换桥，上层聊天照常。

> 兼容性：`ai_chat` 在旧内核（`python -m arkbot`）与 NG 微内核（`python -m arkbot2`）下都已验证可用，
> 因为会话/触发逻辑只依赖统一事件与 `event.reply`，不依赖任一内核的内部实现。

---

## 2. 五分钟接上

### 2.1 AstrBot（推荐先跑这个）

前提：AstrBot 已启动，WebUI 默认 `http://127.0.0.1:6185`；在 **设置 → API Key** 里建一个带 `chat` scope 的 key（形如 `abk_xxx`）。

```toml
# bot.toml
[plugins.ai_chat]
default_backend = "astr"

[plugins.ai_chat.backends.astr]
type = "astrbot"
base_url = "http://127.0.0.1:6185"
api_key = "abk_你的key"
username = "arkbot"      # AstrBot 用它区分会话归属，可任意填
stream = true            # 走 SSE 流式（v4.18+ 默认接口）
```

启动后在 QQ 里发 `/ai 你好` 即可。AstrBot 那边的插件、工具调用、知识库、人格都会照常生效——
因为请求走的是它**内建的 Agent**，不是裸模型。

> 老版本或装了社区 `astrbot_plugin_httpplatform` 的话，把 `path` 改成 `/api/v1/message`、`stream` 设为 `false`。

### 2.2 DSH「养鱼」

前提：DSH 已装 `dsh-aurabot-qq`（或同类 QQ 插件），并开启了它的「本地接口」页。

```toml
[plugins.ai_chat.backends.fish]
type = "dsh"
mode = "aurbot"                       # 见下表
base_url = "http://127.0.0.1:8770"    # 插件本地接口监听的地址
path = "/api/chat"                    # 插件实际暴露的路径，按它的面板填
token = ""                            # 若要求鉴权就填
persona = ""                          # 可写一句人设，透传给后端
```

DSH 的三种模式，按你的环境选：

| mode | 打哪里 | 适用场景 | 稳定性 |
|------|--------|----------|--------|
| `aurbot` | `POST {base_url}{path}`（JSON，一次返回；也兼容 `task_id` 异步任务再轮询） | `dsh-aurabot-qq` 的本地接口 | 取决于插件版本，字段较宽容 |
| `bridge` | 先 `POST /inbound` 投提示词，再轮询 `GET /outbox?since=…` 取回复 | `dsh-ai-bridge`：外部 AI 与 DSH 会话双向对话 | 需要插件按该约定实现 |
| `openai` | `POST {base_url}/chat/completions`（标准 OpenAI 协议） | 任何 OpenAI 兼容端点：DeepSeek 官方、Ollama、LM Studio、公司网关 | ✅ 最稳，协议确定 |

**建议**：先用 `openai` 模式打通（配一个 DeepSeek key 或本地 Ollama），确认 QQ 侧整条链路通，再切 `aurbot` 去用它的人格与记忆。

```toml
# 最稳的兜底：直接用模型
[plugins.ai_chat.backends.model]
type = "openai"
base_url = "https://api.deepseek.com/v1"
api_key = "sk-..."
model = "deepseek-chat"
persona = "你是一条住在 QQ 群里的大肥鱼，说话简短、爱用颜文字。"
```

> 说明：`aurbot` / `bridge` 两种模式的字段是按插件公开说明做的**宽容适配**（同时发送 `message/text/content/prompt` 多个别名，
> 回复从多种 JSON 结构里提取）。插件升级若改了协议，改 `path` 或加 `extra_body` 即可，无需改代码。

---

## 3. 命令与行为

| 命令 | 作用 |
|------|------|
| `/ai <内容>` | 用当前会话绑定的后端聊一句（不受冷却限制） |
| `/ai backend` | 查看可用后端与当前选择 |
| `/ai backend fish` | 把**当前会话**切到 `fish`（别名：`dsh`/`大鱼`/`养鱼`） |
| `/ai on` / `/ai off` | 开/关本群「养鱼模式」 |
| `/ai reset` | 开一段新会话（旧上下文不再带过去） |
| `/ai who` | 查看会话号、后端、养鱼模式、冷却 |

**养鱼模式**（默认开启）：群里被 @，或消息里含唤醒词（默认「大鱼」「肥鱼」）就自动接话；
带冷却（默认 5 秒）与「上一句还在想就不接第二句」的保护，避免刷屏与并发打爆后端。

**会话 ID**：`arkbot:g<群号>` / `arkbot:u<QQ号>`，`/ai reset` 后追加 `:r1`、`:r2`……
它是**确定性生成**的，所以进程重启后仍会回到原来那段上下文（历史保存在 AstrBot / DSH 那边）。

---

## 4. 配置项全表（`[plugins.ai_chat]`）

| 键 | 默认 | 说明 |
|----|------|------|
| `default_backend` | `""` | 未显式切换时用哪个后端（填 `backends` 里的名字） |
| `wake_words` | `["大鱼","肥鱼"]` | 群聊里触发接话的词 |
| `relay` | `true` | 群聊养鱼模式默认是否开启 |
| `cooldown` | `5.0` | 同一会话自动接话的最小间隔（秒）；命令不受限 |
| `max_chars` | `600` | 单条回复最大字数，超出分段发送（最小 80） |
| `backends.<名字>` | — | 后端定义，键名随便起，用 `type` 决定实现 |

后端通用键：`type`、`base_url`、`timeout`、`persona`、`extra_body`（原样并入请求体）。
AstrBot 专有：`api_key`、`username`、`path`、`stream`。
DSH 专有：`mode`、`path`、`token`、`model`、`poll_interval`。

---

## 5. 自己加一个后端（共创入口）

在 `arkbot/integrations/chat.py` 里加一个类，实现一个方法就行：

```python
class MyBackend(ChatBackend):
    name = "mybot"

    def __init__(self, base_url: str = "http://127.0.0.1:9000", timeout: float = 60):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def chat(self, text, *, session="", user="", nickname=""):
        data = await post_json(
            f"{self.base_url}/ask",
            {"q": text, "session": session, "user": user},
            {"Authorization": "Bearer xxx"},
            self.timeout,
        )
        return ChatReply(text=extract_text(data).strip(), backend=self.name, session=session)
```

然后在 `build_backend()` 加一个分支：

```python
if kind == "mybot":
    return MyBackend(base_url=str(cfg.get("base_url") or "http://127.0.0.1:9000"), ...)
```

配置里就能用了：

```toml
[plugins.ai_chat.backends.mine]
type = "mybot"
```

约定（请一并遵守）：

1. **只返回文本**：结构化内容（图片/语音）先降级成文本或链接，别把协议差异泄进插件层；
2. **失败抛 `ChatError`**：插件会把错误变成一句人话回给用户，而不是静默卡死；
3. **会话交给后端**：`session` 只是一个稳定字符串，不要在 ArkBot 侧存对话历史；
4. **超时可控**：外部服务可能很慢，一定要传 `timeout`；
5. **别引新依赖**：HTTP 用 `post_json` / `get_json` / `post_sse`（标准库 + 线程），项目只依赖 `websockets`。

再加个单测（`tests/unit/test_chat_integrations.py` 里已有模板：构造 + 请求体 + 别名路由），
和一条 e2e（照着 `tests/fakes/fake_chat_backends.py` 写个假服务端）。

---

## 6. 排障

| 现象 | 原因 | 处理 |
|------|------|------|
| `（astr 没有回应：HTTP 401…）` | API Key 无效或缺 `chat` scope | WebUI 重新建 key，确认 scope 勾了 `chat` |
| `HTTP 403 Insufficient API key scope` | key 权限不够 | 同上；管理员身份还需要 `chat:admin` |
| `（fish 没有回应：连接失败…）` | 本地接口没起 / 端口不对 | 在浏览器或 `curl` 里先打一下 `base_url` |
| 回复是空的 `（…想了一会儿，什么也没说）` | 后端返回结构不在已知字段里 | 用 `extra_body` 调整，或给 `extract_text` 加键（欢迎提 PR） |
| 群里不理人 | 养鱼模式关了 / 没 @ 也没唤醒词 / 冷却中 | `/ai who` 看状态；`/ai on` 打开 |
| 一直重复同一段 | 后端按 `session_id` 缓存了上下文 | `/ai reset` 换会话 |
| 日志里 `ai_chat 未配置任何聊天后端` | `[plugins.ai_chat.backends.*]` 没写或名字拼错 | 对照第 4 节检查 |

调试建议：把 `[bot] log_level` 设成 `DEBUG`，能看到每天请求的失败原因；假服务端（`tests/fakes/fake_chat_backends.py`）
可以在没有 AstrBot / DSH 的机器上验证整条链路。

---

## 7. 和「抗封禁」的关系

- **上层**（本文）：AstrBot 挂了还有 DSH，DSH 挂了还有裸模型，`/ai backend` 一句话切换；
- **下层**（[README](../../README.md)）：OneBot 挂了还有官方通道，官方通道受限还有离线发件箱。

两层互相独立，所以最坏情况下你仍然能：**在 QQ 里发一句话，得到一个回答**。
