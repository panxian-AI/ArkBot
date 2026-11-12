"""Bot：面向插件的平台无关 API 门面 + 多通道路由。

路由规则：
  - reply(event, chain)     → 优先事件来源通道，来源不可用/不支持时
                              依次尝试其他健康通道（抗 ban 核心）。
  - send_group_msg 等主动发送 → 路由到第一个健康通道；失败自动尝试下一通道。
  - call_api                → 直呼指定通道（默认第一个健康通道）。

插件只面对 Bot，永远不知道消息最终从哪条通道出去。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from .adapters.base import Adapter, ApiError
from .message import MessageChain, MessageSegment, Msg

if TYPE_CHECKING:
    from .app import ArkBot
    from .persist import Persist

logger = logging.getLogger("arkbot.bot")


def _to_chain(msg: Msg) -> MessageChain:
    if isinstance(msg, MessageChain):
        return msg
    if isinstance(msg, MessageSegment):
        return MessageChain(msg)
    if isinstance(msg, str):
        return MessageChain.parse(msg)
    return MessageChain.parse(msg)


class Bot:
    """统一 API。所有方法返回与 OneBot 风格同构的 data。"""

    def __init__(
        self,
        app: "ArkBot",
        adapters: list[Adapter] | Adapter,
        persist: "Persist | None" = None,
    ):
        self.app = app
        self.adapters: list[Adapter] = (
            list(adapters) if isinstance(adapters, list) else [adapters]
        )
        self.self_id: int = 0
        # 持久化：离线发件箱落盘，进程重启后自动恢复补发（磁盘不可用则内存模式）
        self.persist = persist
        # 离线发件箱：所有通道都不可用时暂存主动消息，通道恢复后自动补发
        # —— 让定时任务类消息在故障期间「延迟到达」而不是「丢失」，体验无感
        # 注意：at 用墙上时钟 time.time()，跨进程重启仍然可比（旧版用循环
        # 相对时间，重启后 TTL 判断会失真，这也是本次持久化修复的一部分）
        self._outbox: list[dict[str, Any]] = []
        self._outbox_task: asyncio.Task | None = None

    def start_outbox(self) -> None:
        """由 App 在事件循环内调用：先从磁盘恢复未补发的消息，再启动补发任务。"""
        if self.persist is not None:
            try:
                saved = self.persist.load_outbox()
                now = time.time()
                restored = [i for i in saved if now - i.get("at", 0) <= self.OUTBOX_TTL]
                dropped = len(saved) - len(restored)
                if restored:
                    self._outbox = restored[: self.OUTBOX_MAX]
                    logger.info("从磁盘恢复 %d 条发件箱消息（过期丢弃 %d 条）", len(restored), dropped)
                elif dropped:
                    logger.info("清理 %d 条过期发件箱消息", dropped)
            except Exception:
                logger.exception("发件箱恢复失败（忽略，继续内存模式）")
        self._outbox_task = asyncio.get_running_loop().create_task(self._flush_outbox())

    async def stop_outbox(self) -> None:
        if self._outbox_task is not None:
            self._outbox_task.cancel()
            try:
                await self._outbox_task
            except asyncio.CancelledError:
                pass
            self._outbox_task = None

    OUTBOX_TTL = 600.0        # 暂存 10 分钟
    OUTBOX_MAX = 200
    OUTBOX_FLUSH_INTERVAL = 3.0

    async def _flush_outbox(self) -> None:
        while True:
            await asyncio.sleep(self.OUTBOX_FLUSH_INTERVAL)
            if not self._outbox or not self.healthy():
                continue
            still_pending: list[dict[str, Any]] = []
            now = time.time()
            for item in self._outbox:
                if now - item["at"] > self.OUTBOX_TTL:
                    logger.warning("发件箱消息过期丢弃: %s", item["action"])
                    continue
                try:
                    # _allow_outbox=False：补发失败要「留在队列里」，不能重新入队
                    # （否则每轮补发都会产生新副本，队列永远清不空）
                    await self.call_api(item["action"], _allow_outbox=False, **item["params"])
                    logger.info("发件箱消息已补发: %s", item["action"])
                except Exception:
                    still_pending.append(item)
            self._outbox = still_pending[: self.OUTBOX_MAX]
            self._save_outbox()

    # ---------- 发件箱持久化 ----------
    def _enqueue_outbox(self, action: str, params: dict[str, Any]) -> None:
        if len(self._outbox) >= self.OUTBOX_MAX:
            logger.warning("发件箱已满（%d），丢弃 %s", self.OUTBOX_MAX, action)
            return
        self._outbox.append({"action": action, "params": params, "at": time.time()})
        self._save_outbox()
        logger.warning("无通道可完成 %s，已入离线发件箱（恢复后自动补发）", action)

    def _save_outbox(self) -> None:
        if self.persist is None:
            return
        try:
            self.persist.save_outbox(list(self._outbox))
        except Exception:
            logger.exception("发件箱落盘失败（忽略）")

    # ---------- 通道选择 ----------
    @property
    def adapter(self) -> Adapter:
        """兼容旧代码：返回第一个健康通道（无健康通道则返回第一个）。"""
        for a in self.adapters:
            if a.is_connected:
                return a
        return self.adapters[0]

    def healthy(self) -> list[Adapter]:
        return [a for a in self.adapters if a.is_connected]

    # ---------- 回复路由（抗 ban 核心） ----------
    async def reply(self, event: Any, chain: Msg, quote: bool = True) -> Any:
        """按事件来源通道回复；来源通道失败则逐个尝试其他健康通道。"""
        chain = _to_chain(chain)
        # 1) 来源通道优先（保持协议语义，如官方被动回复的 msg_id）
        origin = event.adapter
        if origin is not None and origin.is_connected:
            try:
                if await origin.send_reply(event, chain, quote=quote):
                    return None
            except Exception as e:
                logger.warning("来源通道 %s 回复失败，尝试其他通道: %s", origin.name, e)
        # 2) 逐个尝试健康通道
        last_err: Exception | None = None
        for a in self.healthy():
            if a is origin:
                continue
            try:
                if await a.send_reply(event, chain, quote=quote):
                    return None
            except Exception as e:
                last_err = e
                logger.warning("通道 %s 回复失败: %s", a.name, e)
        raise ApiError("reply", -1, f"所有通道均无法回复（最后错误: {last_err}）")

    # ---------- 底层 ----------
    async def call_api(
        self, action: str, via: Adapter | None = None, _allow_outbox: bool = True, **params: Any
    ) -> Any:
        """调用指定/默认通道的 API。默认路由到第一个健康通道，失败逐个降级。"""
        candidates: list[Adapter] = [via] if via is not None else []
        candidates += [a for a in self.adapters if a is not via]
        last_err: Exception | None = None
        for a in candidates:
            if not a.is_connected:
                continue
            try:
                return await a.call_api(action, params)
            except ApiError as e:
                if e.retcode in (1404, 429):
                    # 1404=通道不支持此动作；429=通道限速 → 尝试下一个通道，
                    # 都不行时 send_* 动作落入离线发件箱延迟补发
                    continue
                raise
            except Exception as e:
                last_err = e
                logger.warning("通道 %s 调用 %s 失败: %s", a.name, action, e)
        # 所有通道都无法完成该动作（不可达或不支持）：主动消息入发件箱补发
        if action.startswith("send_") and _allow_outbox:
            self._enqueue_outbox(action, params)
            return None
        raise ApiError(action, -1, f"无可用通道（最后错误: {last_err}）")

    # ---------- 发消息（主动） ----------
    async def send_private_msg(self, user_id: int, message: Msg) -> Any:
        return await self.call_api(
            "send_private_msg", user_id=user_id, message=_to_chain(message).to_onebot()
        )

    async def send_group_msg(self, group_id: int, message: Msg) -> Any:
        return await self.call_api(
            "send_group_msg", group_id=group_id, message=_to_chain(message).to_onebot()
        )

    async def send(self, event_type: str, target_id: int, message: Msg) -> Any:
        """按上下文发送：('private'|'group', id, msg)。"""
        if event_type == "group":
            return await self.send_group_msg(target_id, message)
        return await self.send_private_msg(target_id, message)

    async def delete_msg(self, message_id: int) -> Any:
        return await self.call_api("delete_msg", message_id=message_id)

    # ---------- 消息获取 ----------
    async def get_msg(self, message_id: int) -> Any:
        return await self.call_api("get_msg", message_id=message_id)

    async def get_forward_msg(self, id_: str) -> Any:
        return await self.call_api("get_forward_msg", id=id_)

    # ---------- 群管理 ----------
    async def get_group_list(self) -> Any:
        return await self.call_api("get_group_list")

    async def get_group_member_list(self, group_id: int) -> Any:
        return await self.call_api("get_group_member_list", group_id=group_id)

    async def get_group_member_info(self, group_id: int, user_id: int) -> Any:
        return await self.call_api("get_group_member_info", group_id=group_id, user_id=user_id)

    async def set_group_kick(self, group_id: int, user_id: int) -> Any:
        return await self.call_api("set_group_kick", group_id=group_id, user_id=user_id)

    async def set_group_ban(
        self, group_id: int, user_id: int, duration: int = 1800
    ) -> Any:
        return await self.call_api(
            "set_group_ban", group_id=group_id, user_id=user_id, duration=duration
        )

    async def set_group_card(self, group_id: int, user_id: int, card: str) -> Any:
        return await self.call_api(
            "set_group_card", group_id=group_id, user_id=user_id, card=card
        )

    async def set_group_leave(self, group_id: int) -> Any:
        return await self.call_api("set_group_leave", group_id=group_id)

    # ---------- 好友 ----------
    async def get_friend_list(self) -> Any:
        return await self.call_api("get_friend_list")

    async def set_friend_add_request(self, flag: str, approve: bool = True) -> Any:
        return await self.call_api(
            "set_friend_add_request", flag=flag, approve=approve
        )

    async def set_group_add_request(self, flag: str, sub_type: str, approve: bool = True) -> Any:
        return await self.call_api(
            "set_group_add_request", flag=flag, sub_type=sub_type, approve=approve
        )

    # ---------- 戳一戳（OneBot 扩展） ----------
    async def friend_poke(self, user_id: int) -> Any:
        return await self.call_api("friend_poke", user_id=user_id)

    async def group_poke(self, user_id: int, group_id: int) -> Any:
        return await self.call_api("group_poke", user_id=user_id, group_id=group_id)

    # ---------- 状态 ----------
    async def get_status(self) -> Any:
        return await self.call_api("get_status")

    async def get_login_info(self) -> Any:
        return await self.call_api("get_login_info")

    # ---------- 便捷构造 ----------
    @staticmethod
    def text(t: str) -> MessageSegment:
        return MessageSegment.text(t)

    @staticmethod
    def at(qq: int | str) -> MessageSegment:
        return MessageSegment.at(qq)

    @staticmethod
    def image(file: str) -> MessageSegment:
        return MessageSegment.image(file)

    @staticmethod
    def reply_seg(message_id: int) -> MessageSegment:
        return MessageSegment.reply(message_id)
