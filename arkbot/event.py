"""ArkBot 事件模型。

所有适配器收到原始数据后，统一包装为 ArkBot 事件对象。
插件只接触这些类型，不接触任何协议细节 —— 这是平台解耦的关键。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .message import MessageChain, MessageSegment

if TYPE_CHECKING:
    from .adapters.base import Adapter
    from .bot import Bot


@dataclass
class Sender:
    user_id: int = 0
    nickname: str = ""
    sex: str = "unknown"
    age: int = 0
    card: str = ""        # 群名片
    area: str = ""
    level: str = ""
    role: str = "member"  # owner / admin / member
    title: str = ""

    @classmethod
    def parse(cls, raw: dict[str, Any] | None) -> "Sender":
        raw = raw or {}
        return cls(
            user_id=int(raw.get("user_id") or 0),
            nickname=str(raw.get("nickname") or ""),
            sex=str(raw.get("sex") or "unknown"),
            age=int(raw.get("age") or 0),
            card=str(raw.get("card") or ""),
            area=str(raw.get("area") or ""),
            level=str(raw.get("level") or ""),
            role=str(raw.get("role") or "member"),
            title=str(raw.get("title") or ""),
        )


@dataclass
class Event:
    """事件基类：保留原始 dict，同时提供常用字段。"""

    raw: dict[str, Any] = field(default_factory=dict)
    time: int = field(default_factory=lambda: int(time.time()))
    self_id: int = 0
    bot: "Bot | None" = None
    post_type: str = ""
    # 事件来源通道（由适配器 _emit 时注入）；回复优先走来源通道
    adapter: "Adapter | None" = None

    def _init_from_raw(self) -> None:
        pass

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Event":
        """根据 post_type 构造对应的子类并填充字段。"""
        post_type = str(raw.get("post_type") or "")
        sub_cls = {
            "message": MessageEvent,
            "notice": NoticeEvent,
            "request": RequestEvent,
            "meta_event": MetaEvent,
        }.get(post_type, Event)
        ev = sub_cls(raw=raw)
        ev.post_type = post_type
        ev.time = int(raw.get("time") or ev.time)
        ev.self_id = int(raw.get("self_id") or 0)
        ev._init_from_raw()
        if isinstance(ev, MessageEvent):
            ev._resolve_to_me()
        return ev


@dataclass
class MessageEvent(Event):
    """消息事件（私聊/群聊）。"""

    message_type: str = ""   # private / group
    sub_type: str = ""
    message_id: int = 0
    user_id: int = 0
    group_id: int = 0
    message: MessageChain = field(default_factory=MessageChain)
    raw_message: str = ""
    sender: Sender = field(default_factory=Sender)
    to_me: bool = False

    def _init_from_raw(self) -> None:
        self.message_type = str(self.raw.get("message_type") or "")
        self.sub_type = str(self.raw.get("sub_type") or "")
        self.message_id = int(self.raw.get("message_id") or 0)
        self.user_id = int(self.raw.get("user_id") or 0)
        self.group_id = int(self.raw.get("group_id") or 0)
        self.raw_message = str(self.raw.get("raw_message") or "")
        self.message = MessageChain.parse(self.raw.get("message"))
        if not self.raw_message:
            self.raw_message = self.message.to_cq()
        self.sender = Sender.parse(self.raw.get("sender"))

    def _resolve_to_me(self) -> None:
        """消息开头 @ 机器人本人则视为 to_me。"""
        if self.message.segments and self.message[0].type == "at":
            if str(self.message[0].data.get("qq")) == str(self.self_id):
                self.to_me = True

    @property
    def is_private(self) -> bool:
        return self.message_type == "private"

    @property
    def is_group(self) -> bool:
        return self.message_type == "group"

    @property
    def is_admin(self) -> bool:
        return self.sender.role in ("owner", "admin")

    @property
    def text(self) -> str:
        """去掉 CQ 码后的纯文本。"""
        return self.message.plain_text()

    async def reply(
        self,
        message: "str | MessageChain",
        at_sender: bool = False,
        quote: bool = True,
    ) -> Any:
        """快捷回复：自动区分私聊/群聊，默认引用原消息。

        路由规则：优先经来源通道回复（保证协议语义正确），
        来源通道不可用时由 Bot 路由到其他健康通道。
        """
        assert self.bot is not None
        chain = message if isinstance(message, MessageChain) else MessageChain.parse(message)
        if at_sender and self.is_group:
            chain = chain + MessageSegment.at(self.user_id)
        return await self.bot.reply(self, chain, quote=quote)


@dataclass
class NoticeEvent(Event):
    """通知事件：群文件上传、戳一戳、成员变动、禁言等。"""

    notice_type: str = ""
    sub_type: str = ""
    group_id: int = 0
    user_id: int = 0
    operator_id: int = 0
    target_id: int = 0
    file_id: str = ""
    file_name: str = ""
    duration: int = 0

    def _init_from_raw(self) -> None:
        self.notice_type = str(self.raw.get("notice_type") or "")
        self.sub_type = str(self.raw.get("sub_type") or "")
        self.group_id = int(self.raw.get("group_id") or 0)
        self.user_id = int(self.raw.get("user_id") or 0)
        self.operator_id = int(self.raw.get("operator_id") or 0)
        self.target_id = int(self.raw.get("target_id") or 0)
        self.duration = int(self.raw.get("duration") or 0)
        file = self.raw.get("file") or {}
        self.file_id = str(file.get("id") or "")
        self.file_name = str(file.get("name") or "")

    async def poke_back(self) -> Any:
        """回戳对方（仅 group_poke / friend_poke）。"""
        assert self.bot is not None
        if self.group_id:
            return await self.bot.group_poke(self.user_id, self.group_id)
        return await self.bot.friend_poke(self.user_id)


@dataclass
class RequestEvent(Event):
    """请求事件：加好友 / 加群。"""

    request_type: str = ""
    sub_type: str = ""
    user_id: int = 0
    group_id: int = 0
    comment: str = ""
    flag: str = ""

    def _init_from_raw(self) -> None:
        self.request_type = str(self.raw.get("request_type") or "")
        self.sub_type = str(self.raw.get("sub_type") or "")
        self.user_id = int(self.raw.get("user_id") or 0)
        self.group_id = int(self.raw.get("group_id") or 0)
        self.comment = str(self.raw.get("comment") or "")
        self.flag = str(self.raw.get("flag") or "")

    async def approve(self) -> Any:
        assert self.bot is not None
        if self.request_type == "friend":
            return await self.bot.set_friend_add_request(self.flag, True)
        return await self.bot.set_group_add_request(self.flag, self.sub_type, True)

    async def reject(self) -> Any:
        assert self.bot is not None
        if self.request_type == "friend":
            return await self.bot.set_friend_add_request(self.flag, False)
        return await self.bot.set_group_add_request(self.flag, self.sub_type, False)


@dataclass
class MetaEvent(Event):
    """元事件：连接生命周期（connect）、心跳。"""

    meta_event_type: str = ""
    sub_type: str = ""
    status: dict[str, Any] = field(default_factory=dict)
    interval: int = 0

    def _init_from_raw(self) -> None:
        self.meta_event_type = str(self.raw.get("meta_event_type") or "")
        self.sub_type = str(self.raw.get("sub_type") or "")
        self.status = dict(self.raw.get("status") or {})
        self.interval = int(self.raw.get("interval") or 0)
