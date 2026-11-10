"""ArkBot NG 微内核。

与 arkbot 旧内核（app/dispatch/plugin）相比的新能力：
  1. 结构化并发：非消息事件（notice/request/meta）用 asyncio.TaskGroup 并发分发，
     任一监听器异常自动记录，不再互相阻塞。
  2. 中间件管道：所有事件先穿过 middleware 链再进入匹配器，可做限流/审计/改写。
  3. 依赖注入：新式处理器按类型注解拿依赖 —— async def h(bot: Bot, ev: MessageEvent)。
     旧式 (event, state) 签名完全兼容（旧插件零改动）。
  4. 插件发现：目录扫描 + importlib.metadata.entry_points（pip 包即插件）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .bot import Bot
    from arkbot.event import Event, MessageEvent

logger = logging.getLogger("arkbot2.kernel")

Handler = Callable[..., Awaitable[Any]]


# ---------------- 依赖注入调用器 ----------------

def _build_invoker(fn: Handler) -> Callable[[MessageEvent, dict[str, Any]], Awaitable[Any]]:
    """把处理器包装成统一的 (event, state) 调用形式。

    兼容三种签名：
      async def h(event, state)          # 旧式（无注解或两参）
      async def h(event)                 # 单参简写
      async def h(bot: Bot, ev: MessageEvent, state: dict)   # DI 风格（有注解）
    """
    sig = inspect.signature(fn)
    params = [
        p for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    ]
    if any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in sig.parameters.values()):
        return lambda ev, st: fn(ev, st)

    n = len(params)
    if n == 0:
        return lambda ev, st: fn()
    anns = [p.annotation for p in params]
    # DI 风格：所有参数都有类型注解（旧式 event/state 无注解）
    if n >= 2 and all(a is not inspect.Parameter.empty for a in anns):

        def _is_bot_annotation(ann: Any) -> bool:
            # 注解可能是类、字符串，或 TYPE_CHECKING 下退化为 'Bot' 字符串——按名字识别，
            # 避免运行时 import arkbot.bot（兼容模式下旧包导入已被切换）
            cls = ann if inspect.isclass(ann) else None
            return (
                (cls is not None and cls.__name__ == "Bot")
                or (isinstance(ann, str) and "Bot" in ann)
            )

        async def _di(ev: MessageEvent, st: dict[str, Any]) -> Any:
            kwargs: dict[str, Any] = {}
            for p, ann in zip(params, anns):
                if ann is dict or (isinstance(ann, str) and ann == "dict"):
                    kwargs[p.name] = st
                elif _is_bot_annotation(ann):
                    if ev.bot is None:
                        raise TypeError(f"处理器 {fn.__name__} 需要 Bot，但事件未绑定")
                    kwargs[p.name] = ev.bot
                else:
                    # 其余注解一律当作事件类型
                    kwargs[p.name] = ev
            return await fn(**kwargs)

        return _di
    if n == 1:
        return lambda ev, st: fn(ev)
    return lambda ev, st: fn(ev, st)


# ---------------- 匹配器与监听器 ----------------

@dataclass
class Matcher:
    rule: Any                                   # arkbot.dispatch.Rule
    handler: Handler
    priority: int = 100
    block: bool = True
    plugin: str = ""

    def __post_init__(self) -> None:
        self._invoke = _build_invoker(self.handler)

    async def run(self, event: MessageEvent) -> bool:
        """返回 True 表示已处理且阻止后续 Matcher。"""
        ok, state = await self.rule(event)
        if not ok:
            return False
        try:
            result = await self._invoke(event, state)
        except Exception:
            logger.exception("处理器执行异常 (plugin=%s)", self.plugin or "?")
            return self.block
        if result is False:
            return False
        return self.block


@dataclass
class Listener:
    """通知 / 请求 / 元事件监听器。"""

    post_type: str                    # notice / request / meta_event
    sub_key: str                      # notice_type / request_type / meta_event_type
    sub_values: tuple[str, ...]       # 空 = 全部
    handler: Handler
    plugin: str = ""

    def __post_init__(self) -> None:
        self._invoke = _build_invoker(self.handler)

    def matches(self, event: Event) -> bool:
        if event.post_type != self.post_type:
            return False
        sub = str(event.raw.get(self.sub_key) or "")
        return not self.sub_values or sub in self.sub_values

    async def run(self, event: Event) -> None:
        await self._invoke(event, {})


# ---------------- 微内核 ----------------

class Kernel:
    """事件分发核心：中间件 → 匹配器（消息）/ 监听器（其余事件）。"""

    def __init__(self) -> None:
        self.matchers: list[Matcher] = []
        self.listeners: list[Listener] = []
        self.middlewares: list[Callable[[Event, Callable], Awaitable[Any]]] = []
        self._sorted = True
        self.event_count = 0

    # ---------- 注册 ----------
    def add_matcher(self, m: Matcher) -> None:
        self.matchers.append(m)
        self._sorted = False

    def add_listener(self, l: Listener) -> None:
        self.listeners.append(l)

    def middleware(
        self, fn: Callable[[Event, Callable], Awaitable[Any]]
    ) -> Callable[[Event, Callable], Awaitable[Any]]:
        """@kernel.middleware  async def mw(event, next): ...; await next(event)"""
        self.middlewares.append(fn)
        return fn

    # ---------- 分发 ----------
    async def dispatch(self, event: Event) -> None:
        self.event_count += 1
        nxt: Callable[[Event], Awaitable[Any]] = self._fanout
        for mw in reversed(self.middlewares):
            nxt = _chain(mw, nxt)
        await nxt(event)

    async def _fanout(self, event: Event) -> None:
        if event.post_type == "message":
            self._ensure_sorted()
            for m in self.matchers:
                if await m.run(event):
                    break
        elif event.post_type in ("notice", "request", "meta_event"):
            # 结构化并发：同一事件的所有监听器并发执行
            hits = [l for l in self.listeners if l.matches(event)]
            if not hits:
                return

            async def guarded(l: Listener) -> None:
                try:
                    await l.run(event)
                except Exception:
                    logger.exception("监听器执行异常 (plugin=%s)", l.plugin or "?")

            async with asyncio.TaskGroup() as tg:
                for l in hits:
                    tg.create_task(guarded(l), name=f"ark2-listen-{l.plugin}")

    def _ensure_sorted(self) -> None:
        if not self._sorted:
            self.matchers.sort(key=lambda m: m.priority)
            self._sorted = True


def _chain(
    mw: Callable[[Event, Callable], Awaitable[Any]],
    nxt: Callable[[Event], Awaitable[Any]],
) -> Callable[[Event], Awaitable[Any]]:
    async def runner(event: Event) -> Any:
        return await mw(event, nxt)
    return runner
