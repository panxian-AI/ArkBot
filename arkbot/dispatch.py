"""规则匹配与事件分发。

Matcher = 规则(Rule) + 处理函数(handler)，按 priority 排序依次尝试；
命中且 handler 未放行（Block）则停止传播。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from .event import Event, MessageEvent
from .message import MessageChain

logger = logging.getLogger("arkbot.dispatch")


class Rule:
    """一条可组合的匹配规则。call(event) -> (是否命中, 提取的参数 dict)。"""

    def __init__(
        self,
        check: Callable[[MessageEvent], bool] | None = None,
        extract: Callable[[MessageEvent], dict[str, Any]] | None = None,
    ):
        self.check = check or (lambda e: True)
        self.extract = extract or (lambda e: {})

    async def __call__(self, event: MessageEvent) -> tuple[bool, dict[str, Any]]:
        try:
            ok = self.check(event)
            return (bool(ok), self.extract(event) if ok else {})
        except Exception:
            logger.exception("规则匹配异常")
            return (False, {})

    def __and__(self, other: "Rule") -> "Rule":
        return Rule(
            check=lambda e: self.check(e) and other.check(e),
            extract=lambda e: {**self.extract(e), **other.extract(e)},
        )


# ---------------- 内置规则 ----------------

def command(*cmds: str, prefixes: tuple[str, ...] = ("/",), to_me_also: bool = True) -> Rule:
    """命令规则：/cmd 参数... → state['args']，state['prefix']。"""
    cmds_ = [c.lower() for c in cmds]

    def _extract(e: MessageEvent) -> dict[str, Any]:
        text = e.message.plain_text()
        # 去掉开头的 @bot
        if e.to_me and text.startswith("@"):
            text = re.sub(r"^\s*@\S+\s*", "", text, count=1)
        text = text.strip()
        for p in prefixes:
            if text.startswith(p):
                body = text[len(p):]
                parts = body.split(maxsplit=1)
                if parts and parts[0].lower() in cmds_:
                    args = parts[1].strip() if len(parts) > 1 else ""
                    return {"args": args, "prefix": p, "cmd": parts[0].lower()}
        return {}

    def _check(e: MessageEvent) -> bool:
        return bool(_extract(e))

    return Rule(check=_check, extract=lambda e: _extract(e))


def startswith(*texts: str) -> Rule:
    return Rule(check=lambda e: any(e.message.plain_text().lstrip().startswith(t) for t in texts))


def keyword(*words: str) -> Rule:
    return Rule(check=lambda e: any(w in e.message.plain_text() for w in words))


def regex(pattern: str, flags: int = 0) -> Rule:
    rx = re.compile(pattern, flags)

    def _extract(e: MessageEvent) -> dict[str, Any]:
        m = rx.search(e.message.plain_text())
        return {"match": m} if m else {}

    def _check(e: MessageEvent) -> bool:
        return rx.search(e.message.plain_text()) is not None

    return Rule(check=_check, extract=_extract)


def to_me() -> Rule:
    return Rule(check=lambda e: e.to_me or e.is_private)


def only_private() -> Rule:
    return Rule(check=lambda e: e.is_private)


def only_group() -> Rule:
    return Rule(check=lambda e: e.is_group)


def only_admin() -> Rule:
    return Rule(check=lambda e: e.is_group and e.is_admin)


def from_user(*user_ids: int) -> Rule:
    ids = {int(i) for i in user_ids}
    return Rule(check=lambda e: e.user_id in ids)


def from_group(*group_ids: int) -> Rule:
    ids = {int(i) for i in group_ids}
    return Rule(check=lambda e: e.is_group and e.group_id in ids)


# ---------------- Matcher ----------------

@dataclass
class Matcher:
    rule: Rule
    handler: Callable[[MessageEvent, dict[str, Any]], Awaitable[None | bool]]
    priority: int = 100
    block: bool = True
    plugin: str = ""

    async def run(self, event: MessageEvent) -> bool:
        """返回 True 表示已处理且阻止后续 Matcher。"""
        ok, state = await self.rule(event)
        if not ok:
            return False
        try:
            result = await self.handler(event, state)
        except Exception:
            logger.exception("处理器执行异常 (plugin=%s)", self.plugin or "?")
            return self.block
        if result is False:
            return False  # handler 显式放行
        return self.block


@dataclass
class NoticeMatcher:
    """通知/请求事件的监听器。"""

    post_type: str                    # notice / request / meta_event
    sub_key: str                      # notice_type / request_type / meta_event_type
    sub_values: tuple[str, ...]       # 空 = 全部
    handler: Callable[[Event], Awaitable[None]]
    plugin: str = ""


class Dispatcher:
    """持有所有 matcher，把事件分发给它们。"""

    def __init__(self) -> None:
        self.matchers: list[Matcher] = []
        self.listeners: list[NoticeMatcher] = []
        self._sorted = True

    def add_matcher(self, m: Matcher) -> None:
        self.matchers.append(m)
        self._sorted = False

    def add_listener(self, l: NoticeMatcher) -> None:
        self.listeners.append(l)

    def _ensure_sorted(self) -> None:
        if not self._sorted:
            self.matchers.sort(key=lambda m: m.priority)
            self._sorted = True

    async def dispatch(self, event: Event) -> None:
        if isinstance(event, MessageEvent):
            self._ensure_sorted()
            for m in self.matchers:
                if await m.run(event):
                    break
        elif event.post_type in ("notice", "request", "meta_event"):
            key = {
                "notice": "notice_type",
                "request": "request_type",
                "meta_event": "meta_event_type",
            }[event.post_type]
            sub = str(event.raw.get(key) or "")
            for l in self.listeners:
                if l.post_type == event.post_type and (not l.sub_values or sub in l.sub_values):
                    try:
                        await l.handler(event)
                    except Exception:
                        logger.exception("监听器执行异常 (plugin=%s)", l.plugin or "?")
