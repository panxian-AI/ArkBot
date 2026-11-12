"""上层聊天后端接入：DSH（养鱼）与 AstrBot。

ArkBot 的分层里，这一层位于「插件之上、业务之下」——不碰协议、不碰框架核心，
只负责把一条文本送进外部 Agent，再把回复文本取回来。

支持的后端：

* ``AstrBotBackend`` —— AstrBot v4.18+ 的 HTTP API
  ``POST /api/v1/chat``（SSE 流式，Bearer API Key，需要 username / session_id）。
  也兼容社区 HTTP Platform 插件的 ``POST /api/v1/message``（非流式）。
* ``DshBackend`` —— DSH（DeepSeek Harness）里的「养鱼」插件，三种模式：
  - ``aurbot``：dsh-aurabot-qq「本地接口」页暴露的 HTTP 端点（可配置 path）；
  - ``bridge``：dsh-ai-bridge 的入站队列 + outbox 轮询（外部 AI 与 DSH 会话双向对话）；
  - ``openai``：任何 OpenAI 兼容的 ``/chat/completions``（DeepSeek 官方、
    本地 Ollama / LM Studio、网关都行），不依赖 DSH 内部实现，最稳。

设计取舍：全部用标准库 urllib 在线程里发请求（项目至今只依赖 websockets），
SSE 通过线程读行 + asyncio 队列桥回事件循环，不引入 aiohttp/httpx。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("arkbot.integrations.chat")


class ChatError(RuntimeError):
    """后端调用失败（网络、鉴权、协议错误）。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# 宽容的回复文本提取
# ---------------------------------------------------------------------------

# 依次尝试的「文本容器」键：先具体后笼统
_TEXT_KEYS = (
    "content", "text", "plain", "delta", "reply", "answer",
    "output", "response", "message", "data",
)
# 明显是元数据的键，兜底遍历时跳过，避免把 "plain"/"assistant" 之类的类型名当正文
_SKIP_KEYS = {
    "role", "type", "id", "msg_id", "message_id", "status", "code", "retcode",
    "session_id", "session", "event_id", "timestamp", "time", "model", "usage",
    "finish_reason", "index", "object", "created", "sender", "user_id",
    "group_id", "username", "nickname", "source", "from", "to", "seq",
    "conversation_id", "parent_id", "error", "detail", "reason",
}


def extract_text(obj: Any, _depth: int = 0) -> str:
    """从任意 JSON 结构里捞出第一段可用文本。

    兼容 AstrBot 的组件数组、OpenAI 的 choices/delta、DSH 插件的自定义返回。
    每层只取第一个命中的字段，避免同一段话被重复拼接。
    """
    if _depth > 8 or obj is None:
        return ""
    if isinstance(obj, str):
        return obj.strip()
    if isinstance(obj, list):
        for item in obj:
            got = extract_text(item, _depth + 1)
            if got:
                return got
        return ""
    if isinstance(obj, dict):
        for key in _TEXT_KEYS:
            if key in obj:
                got = extract_text(obj[key], _depth + 1)
                if got:
                    return got
        for key, val in obj.items():
            if key in _SKIP_KEYS or isinstance(val, (int, float, bool)):
                continue
            got = extract_text(val, _depth + 1)
            if got:
                return got
    return ""


# ---------------------------------------------------------------------------
# HTTP 传输（标准库 + 线程）
# ---------------------------------------------------------------------------

def _request(
    url: str,
    payload: dict | None,
    headers: dict[str, str],
    timeout: float,
    *,
    method: str | None = None,
    accept: str = "application/json",
) -> Any:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": accept, **headers},
        method=method or ("POST" if data is not None else "GET"),
    )
    return urllib.request.urlopen(req, timeout=timeout)


async def post_json(
    url: str, payload: dict, headers: dict[str, str], timeout: float = 30.0
) -> Any:
    def _do() -> tuple[int, str]:
        try:
            with _request(url, payload, headers, timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            raise ChatError(f"连接失败 {url}: {e.reason}") from e

    status, raw = await asyncio.to_thread(_do)
    if status >= 400:
        raise ChatError(f"HTTP {status}: {raw[:300]}", status)
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"text": raw}  # 有的后端直接吐纯文本


async def get_json(url: str, headers: dict[str, str], timeout: float = 30.0) -> Any:
    def _do() -> tuple[int, str]:
        try:
            with _request(url, None, headers, timeout, method="GET") as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            raise ChatError(f"连接失败 {url}: {e.reason}") from e

    status, raw = await asyncio.to_thread(_do)
    if status >= 400:
        raise ChatError(f"HTTP {status}: {raw[:300]}", status)
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return {"text": raw}


def _sse_chunk(payload: str) -> str:
    """解析一条 SSE data 负载，返回其中的文本（无文本返回空串）。"""
    payload = payload.strip()
    if not payload or payload == "[DONE]":
        return ""
    try:
        return extract_text(json.loads(payload))
    except json.JSONDecodeError:
        return payload if not payload.startswith(("{", "[")) else ""


async def post_sse(
    url: str,
    payload: dict,
    headers: dict[str, str],
    timeout: float = 60.0,
    on_text: Callable[[str], None] | None = None,
) -> tuple[str, bool]:
    """POST 一个 SSE 流，返回 (拼接后的文本, 是否收到过数据事件)。"""
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def push(kind: str, val: Any) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (kind, val))

    def worker() -> None:
        try:
            with _request(
                url, payload, headers, timeout, accept="text/event-stream"
            ) as resp:
                for raw_line in resp:
                    push("line", raw_line.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            push("error", ChatError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}", e.code))
        except urllib.error.URLError as e:
            push("error", ChatError(f"连接失败 {url}: {e.reason}"))
        except Exception as e:  # noqa: BLE001 - 线程内必须兜住所有异常
            push("error", ChatError(str(e)))
        finally:
            push("done", None)

    threading.Thread(target=worker, daemon=True, name="ark-sse").start()

    parts: list[str] = []
    saw_data = False
    pending: list[str] = []
    deadline = time.monotonic() + timeout

    def flush() -> None:
        nonlocal saw_data
        if not pending:
            return
        saw_data = True
        chunk = _sse_chunk("\n".join(pending))
        pending.clear()
        if chunk:
            parts.append(chunk)
            if on_text:
                try:
                    on_text(chunk)
                except Exception:  # noqa: BLE001
                    logger.debug("SSE 回调异常", exc_info=True)

    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise ChatError("SSE 读取超时")
        kind, val = await asyncio.wait_for(queue.get(), timeout=remain)
        if kind == "done":
            break
        if kind == "error":
            raise val
        line = str(val).rstrip("\r\n")
        if not line:
            flush()
            continue
        if line.startswith("data:"):
            pending.append(line[5:].lstrip())
        # event: / id: / retry: 忽略
    flush()
    return "".join(parts), saw_data


# ---------------------------------------------------------------------------
# 后端抽象
# ---------------------------------------------------------------------------

@dataclass
class ChatReply:
    text: str
    backend: str = ""
    session: str = ""
    raw: Any = field(default=None, repr=False)


class ChatBackend:
    """聊天后端接口：给一条文本，拿一段回复。"""

    name = "chat"

    async def chat(
        self,
        text: str,
        *,
        session: str = "",
        user: str = "",
        nickname: str = "",
    ) -> ChatReply:
        raise NotImplementedError

    async def close(self) -> None:
        return None

    # 便于子类复用
    @staticmethod
    def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
        return dict(extra or {})


class AstrBotBackend(ChatBackend):
    """AstrBot HTTP API 后端。

    默认走 v4.18+ 的 ``POST /api/v1/chat``（SSE 流式）；老版本或装了
    HTTP Platform 插件时，可把 ``path`` 改成 ``/api/v1/message`` 走非流式。
    """

    name = "astrbot"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:6185",
        api_key: str = "",
        username: str = "arkbot",
        path: str = "/api/v1/chat",
        stream: bool = True,
        timeout: float = 90.0,
        persona: str = "",
        extra_body: dict[str, Any] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.username = username
        self.path = path if path.startswith("/") else "/" + path
        self.stream = stream
        self.timeout = timeout
        self.persona = persona
        self.extra_body = dict(extra_body or {})

    def _headers(self) -> dict[str, str]:
        h = {"User-Agent": "ArkBot/0.2 (+astrbot-backend)"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _body(self, text: str, session: str, user: str) -> dict[str, Any]:
        # AstrBot 要求 username 必填；session_id 省略则服务端自动建会话
        body: dict[str, Any] = {
            "message": text,
            "username": user or self.username,
        }
        if session:
            body["session_id"] = session
        if self.persona:
            body["persona"] = self.persona
        body.update(self.extra_body)
        return body

    async def chat(
        self, text: str, *, session: str = "", user: str = "", nickname: str = ""
    ) -> ChatReply:
        url = f"{self.base_url}{self.path}"
        body = self._body(text, session, user)
        answer = ""
        if self.stream:
            answer, saw_data = await post_sse(url, body, self._headers(), self.timeout)
            if not answer and not saw_data:
                data = await post_json(url, body, self._headers(), self.timeout)
                answer = extract_text(data)
        else:
            data = await post_json(url, body, self._headers(), self.timeout)
            answer = extract_text(data)
        return ChatReply(text=answer.strip(), backend=self.name, session=session)


class DshBackend(ChatBackend):
    """DSH「养鱼」后端。

    ``mode="aurbot"``：打 dsh-aurabot-qq 的本地接口（默认 ``/api/chat``）。
    ``mode="bridge"``：dsh-ai-bridge 模式——把提示词投进入站队列，再轮询 outbox。
    ``mode="openai"``：OpenAI 兼容 ``/chat/completions``，最稳的兜底。
    """

    name = "dsh"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:3080",
        mode: str = "aurbot",
        path: str = "",
        token: str = "",
        timeout: float = 120.0,
        poll_interval: float = 0.8,
        model: str = "",
        api_key: str = "",
        persona: str = "",
        extra_body: dict[str, Any] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.mode = mode
        self.path = path or ("/api/chat" if mode == "aurbot" else "")
        self.token = token or api_key
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.model = model
        self.persona = persona
        self.extra_body = dict(extra_body or {})

    def _headers(self) -> dict[str, str]:
        h = {"User-Agent": "ArkBot/0.2 (+dsh-backend)"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _base_body(self, text: str, session: str, user: str, nickname: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            # 字段名多写几个别名，兼容不同 fork 的实现
            "message": text,
            "text": text,
            "content": text,
            "prompt": text,
            "source": "arkbot",
            "session": session,
            "session_id": session,
            "user": user,
            "username": user,
            "nickname": nickname,
        }
        if self.persona:
            body["persona"] = self.persona
        body.update(self.extra_body)
        return body

    async def chat(
        self, text: str, *, session: str = "", user: str = "", nickname: str = ""
    ) -> ChatReply:
        if self.mode == "openai":
            return await self._chat_openai(text, session, user)
        if self.mode == "bridge":
            return await self._chat_bridge(text, session, user, nickname)
        return await self._chat_aurbot(text, session, user, nickname)

    async def _chat_aurbot(
        self, text: str, session: str, user: str, nickname: str
    ) -> ChatReply:
        url = f"{self.base_url}{self.path if self.path.startswith('/') else '/' + self.path}"
        body = self._base_body(text, session, user, nickname)
        data = await post_json(url, body, self._headers(), self.timeout)
        answer = extract_text(data)
        if not answer and isinstance(data, dict):
            # 有些 implementation 是异步任务：返回 task_id，需要再拉结果
            task_id = data.get("task_id") or data.get("job_id") or data.get("id")
            if task_id:
                answer = await self._poll_task(str(task_id), session)
        return ChatReply(text=answer.strip(), backend=self.name, session=session)

    async def _poll_task(self, task_id: str, session: str) -> str:
        deadline = time.monotonic() + self.timeout
        url = f"{self.base_url}{self.path.rstrip('/')}/{urllib.parse.quote(task_id)}"
        while time.monotonic() < deadline:
            await asyncio.sleep(self.poll_interval)
            try:
                data = await get_json(url, self._headers(), timeout=15)
            except ChatError:
                continue
            got = extract_text(data)
            if got:
                return got
            if str(data.get("status") or "").lower() in {"failed", "error"}:
                return ""
        return ""

    async def _chat_bridge(
        self, text: str, session: str, user: str, nickname: str
    ) -> ChatReply:
        """dsh-ai-bridge：写进入站队列，然后按时间戳增量轮到 outbox。"""
        body = self._base_body(text, session, user, nickname)
        body["mark"] = "【外部 AI 输入 · 来自 ArkBot】"
        since = time.time()
        await post_json(f"{self.base_url}/inbound", body, self._headers(), 30)
        collected: list[str] = []
        deadline = time.monotonic() + self.timeout
        last_gain = time.monotonic()
        while time.monotonic() < deadline:
            await asyncio.sleep(self.poll_interval)
            query = urllib.parse.urlencode({"since": since, "session": session} if session else {"since": since})
            try:
                data = await get_json(
                    f"{self.base_url}/outbox?{query}", self._headers(), timeout=15
                )
            except ChatError:
                continue
            items = data if isinstance(data, list) else (
                data.get("items") or data.get("messages") or data.get("outbox") or []
            )
            gain = False
            for item in items:
                if not isinstance(item, dict):
                    continue
                src = str(item.get("source") or item.get("from") or item.get("role") or "").lower()
                if src in {"external", "arkbot", "user", "human"}:
                    continue
                got = extract_text(item)
                if got and got not in collected:
                    collected.append(got)
                    gain = True
                ts = item.get("timestamp") or item.get("time") or item.get("ts")
                if isinstance(ts, (int, float)):
                    since = max(since, float(ts))
            if gain:
                last_gain = time.monotonic()
            elif collected and time.monotonic() - last_gain > 2.0:
                break  # 已经拿到回复且流静默 → 视为说完
        return ChatReply(text="".join(collected).strip(), backend=self.name, session=session)

    async def _chat_openai(self, text: str, session: str, user: str) -> ChatReply:
        url = f"{self.base_url}/chat/completions"
        messages: list[dict[str, str]] = []
        if self.persona:
            messages.append({"role": "system", "content": self.persona})
        messages.append({"role": "user", "content": text})
        body: dict[str, Any] = {"messages": messages, "stream": False}
        if self.model:
            body["model"] = self.model
        if session:
            body["user"] = session  # 部分网关按 user 维度做会话隔离
        body.update(self.extra_body)
        data = await post_json(url, body, self._headers(), self.timeout)
        return ChatReply(text=extract_text(data).strip(), backend=self.name, session=session)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

_ALIAS_GROUPS: tuple[tuple[str, set[str]], ...] = (
    ("astrbot", {"astrbot", "astr", "ab", "astr-bot"}),
    ("dsh", {"dsh", "fish", "养鱼", "大鱼", "肥鱼"}),
    ("model", {"model", "openai", "llm", "deepseek"}),
)


class ChatHub:
    """按名字路由到不同聊天后端。"""

    def __init__(self, backends: dict[str, ChatBackend], default: str = ""):
        self.backends = backends
        self.default = default or (next(iter(backends), ""))

    def resolve(self, name: str = "") -> str:
        """把别名/中文名解析成实际配置的后端名。

        用户给后端起什么名字都行（``fish`` / ``dsh`` / ``大鱼`` 都可以），
        这里按「同类别名组」找到配置里真实存在的那个。
        """
        key = (name or "").strip().lower()
        if key in self.backends:
            return key
        for canonical, members in _ALIAS_GROUPS:
            if key in members:
                for candidate in (canonical, *members):
                    if candidate in self.backends:
                        return candidate
        # 名字不认识时退回默认后端，保证「换个叫法也能聊」
        return self.default

    def names(self) -> list[str]:
        return list(self.backends)

    async def chat(
        self,
        text: str,
        *,
        backend: str = "",
        session: str = "",
        user: str = "",
        nickname: str = "",
    ) -> ChatReply:
        name = self.resolve(backend)
        if not name:
            raise ChatError("没有可用的聊天后端，请检查 bot.toml 的 [plugins.ai_chat.backends]")
        return await self.backends[name].chat(
            text, session=session, user=user, nickname=nickname
        )

    async def close(self) -> None:
        for b in self.backends.values():
            try:
                await b.close()
            except Exception:  # noqa: BLE001
                pass


def build_backend(name: str, cfg: dict[str, Any]) -> ChatBackend:
    """从配置字典构造一个后端。``type`` 默认按名字猜。"""
    kind = str(cfg.get("type") or ("astrbot" if name == "astrbot" else "dsh")).lower()
    if kind in {"astrbot", "astr"}:
        return AstrBotBackend(
            base_url=str(cfg.get("base_url") or "http://127.0.0.1:6185"),
            api_key=str(cfg.get("api_key") or ""),
            username=str(cfg.get("username") or "arkbot"),
            path=str(cfg.get("path") or "/api/v1/chat"),
            stream=bool(cfg.get("stream", True)),
            timeout=float(cfg.get("timeout", 90)),
            persona=str(cfg.get("persona") or ""),
            extra_body=cfg.get("extra_body") or None,
        )
    if kind in {"dsh", "fish"}:
        return DshBackend(
            base_url=str(cfg.get("base_url") or "http://127.0.0.1:3080"),
            mode=str(cfg.get("mode") or "aurbot"),
            path=str(cfg.get("path") or ""),
            token=str(cfg.get("token") or ""),
            timeout=float(cfg.get("timeout", 120)),
            poll_interval=float(cfg.get("poll_interval", 0.8)),
            model=str(cfg.get("model") or ""),
            api_key=str(cfg.get("api_key") or ""),
            persona=str(cfg.get("persona") or ""),
            extra_body=cfg.get("extra_body") or None,
        )
    if kind in {"openai", "model", "llm"}:
        return DshBackend(
            base_url=str(cfg.get("base_url") or "https://api.deepseek.com/v1"),
            mode="openai",
            model=str(cfg.get("model") or ""),
            api_key=str(cfg.get("api_key") or ""),
            timeout=float(cfg.get("timeout", 120)),
            persona=str(cfg.get("persona") or ""),
            extra_body=cfg.get("extra_body") or None,
        )
    raise ChatError(f"未知聊天后端类型: {kind!r}（可用: astrbot / dsh / openai）")


def build_hub(cfg: dict[str, Any] | None = None) -> ChatHub:
    """从 ``[plugins.ai_chat]`` 配置段构造 ChatHub。

    形如::

        backends = { astr = {type="astrbot", base_url="..."}, fish = {type="dsh"} }
        default = "astr"
    """
    cfg = cfg or {}
    raw = cfg.get("backends") or {}
    backends: dict[str, ChatBackend] = {}
    if isinstance(raw, dict):
        for name, sub in raw.items():
            if isinstance(sub, dict):
                try:
                    backends[str(name)] = build_backend(str(name), sub)
                except Exception as e:  # noqa: BLE001
                    logger.warning("聊天后端 %s 构造失败: %s", name, e)
    return ChatHub(backends, default=str(cfg.get("default") or ""))
