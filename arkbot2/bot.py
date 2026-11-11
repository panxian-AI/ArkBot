"""ArkBot NG Bot 门面：面向插件的平台无关 API + 多通道路由。

与 arkbot.bot.Bot 的区别：通道是鸭子类型的（任何对象只要有
call_api / send_reply / name / is_connected 即可作通道），
内核不再绑定适配器基类。

路由规则不变：
  - reply(event, chain)  → 优先事件来源通道，失败降级到其他健康通道；
  - 主动发送 → 第一个健康通道，失败逐个降级；
  - 全通道不可用 → send_* 动作入离线发件箱，恢复后自动补发。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .app import ArkBot2
    from arkbot.persist import Persist

logger = logging.getLogger("arkbot2.bot")


class Channel(Protocol):
    """通道协议：适配器只需满足这四个成员即可接入 NG 内核。"""

    name: str

    @property
    def is_connected(self) -> bool: ...

    async def call_api(self, action: str, params: dict[str, Any] | None = None) -> Any: ...

    async def send_reply(self, event: Any, chain: Any, quote: bool = True) -> bool: ...


def _to_onebot(msg: Any) -> Any:
    """把任意消息表示转成 OneBot 数组。鸭子类型：兼容新旧 MessageChain。"""
    if hasattr(msg, "to_onebot"):
        return msg.to_onebot()
    if isinstance(msg, str):
        from arkbot.message import MessageChain
        return MessageChain.parse(msg).to_onebot()
    return msg


class Bot:
    """统一 API。所有方法返回与 OneBot 风格同构的 data。"""

    def __init__(
        self,
        app: "ArkBot2",
        channels: list[Any],
        persist: "Persist | None" = None,
    ):
        self.app = app
        self.channels: list[Any] = channels
        self.self_id: int = 0
        # 持久化：离线发件箱落盘，进程重启后自动恢复补发（与旧内核同一套语义）
        self.persist = persist
        self._outbox: list[dict[str, Any]] = []
        self._outbox_task: asyncio.Task | None = None

    OUTBOX_TTL = 600.0
    OUTBOX_MAX = 200
    OUTBOX_FLUSH_INTERVAL = 3.0

    # ---------- 发件箱 ----------
    def start_outbox(self) -> None:
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
                    await self._raw_call(item["action"], item["params"])
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
    def adapter(self) -> Any:
        """兼容旧插件：返回第一个健康通道（无健康通道则返回第一个）。"""
        for c in self.channels:
            if c.is_connected:
                return c
        return self.channels[0]

    def healthy(self) -> list[Any]:
        return [c for c in self.channels if c.is_connected]

    # ---------- 回复路由 ----------
    async def reply(self, event: Any, chain: Any, quote: bool = True) -> Any:
        origin = getattr(event, "adapter", None)
        if origin is not None and origin.is_connected:
            try:
                if await origin.send_reply(event, chain, quote=quote):
                    return None
            except Exception as e:
                logger.warning("来源通道 %s 回复失败，尝试其他通道: %s", origin.name, e)
        last_err: Exception | None = None
        for c in self.healthy():
            if c is origin:
                continue
            try:
                if await c.send_reply(event, chain, quote=quote):
                    return None
            except Exception as e:
                last_err = e
                logger.warning("通道 %s 回复失败: %s", c.name, e)
        raise RuntimeError(f"所有通道均无法回复（最后错误: {last_err}）")

    # ---------- 底层 ----------
    async def _raw_call(self, action: str, params: dict[str, Any], via: Any = None) -> Any:
        candidates = [via] if via is not None else []
        candidates += [c for c in self.channels if c is not via]
        last_err: Exception | None = None
        for c in candidates:
            if not c.is_connected:
                continue
            try:
                return await c.call_api(action, params)
            except Exception as e:
                # 1404=该通道不支持此动作；429=限速 → 尝试下一个通道，
                # 都不行时 send_* 动作落入离线发件箱延迟补发
                retcode = getattr(e, "retcode", None)
                if retcode in (1404, 429):
                    continue
                last_err = e
                logger.warning("通道 %s 调用 %s 失败: %s", c.name, action, e)
        if action.startswith("send_"):
            self._enqueue_outbox(action, params)
            return None
        raise RuntimeError(f"无可用通道完成 {action}（最后错误: {last_err}）")

    async def call_api(self, action: str, via: Any = None, **params: Any) -> Any:
        return await self._raw_call(action, params, via=via)

    # ---------- 发消息 ----------
    async def send_private_msg(self, user_id: int, message: Any) -> Any:
        return await self._raw_call(
            "send_private_msg", {"user_id": user_id, "message": _to_onebot(message)}
        )

    async def send_group_msg(self, group_id: int, message: Any) -> Any:
        return await self._raw_call(
            "send_group_msg", {"group_id": group_id, "message": _to_onebot(message)}
        )

    async def send(self, event_type: str, target_id: int, message: Any) -> Any:
        if event_type == "group":
            return await self.send_group_msg(target_id, message)
        return await self.send_private_msg(target_id, message)

    async def delete_msg(self, message_id: int) -> Any:
        return await self._raw_call("delete_msg", {"message_id": message_id})

    # ---------- 群管理 ----------
    async def get_group_list(self) -> Any:
        return await self._raw_call("get_group_list", {})

    async def get_group_member_list(self, group_id: int) -> Any:
        return await self._raw_call("get_group_member_list", {"group_id": group_id})

    async def get_group_member_info(self, group_id: int, user_id: int) -> Any:
        return await self._raw_call(
            "get_group_member_info", {"group_id": group_id, "user_id": user_id}
        )

    async def set_group_kick(self, group_id: int, user_id: int) -> Any:
        return await self._raw_call(
            "set_group_kick", {"group_id": group_id, "user_id": user_id}
        )

    async def set_group_ban(self, group_id: int, user_id: int, duration: int = 1800) -> Any:
        return await self._raw_call(
            "set_group_ban",
            {"group_id": group_id, "user_id": user_id, "duration": duration},
        )

    async def set_group_card(self, group_id: int, user_id: int, card: str) -> Any:
        return await self._raw_call(
            "set_group_card", {"group_id": group_id, "user_id": user_id, "card": card}
        )

    async def set_group_leave(self, group_id: int) -> Any:
        return await self._raw_call("set_group_leave", {"group_id": group_id})

    # ---------- 好友 / 请求 ----------
    async def set_friend_add_request(self, flag: str, approve: bool = True) -> Any:
        return await self._raw_call(
            "set_friend_add_request", {"flag": flag, "approve": approve}
        )

    async def set_group_add_request(self, flag: str, sub_type: str, approve: bool = True) -> Any:
        return await self._raw_call(
            "set_group_add_request",
            {"flag": flag, "sub_type": sub_type, "approve": approve},
        )

    # ---------- 戳一戳 ----------
    async def friend_poke(self, user_id: int) -> Any:
        return await self._raw_call("friend_poke", {"user_id": user_id})

    async def group_poke(self, user_id: int, group_id: int) -> Any:
        return await self._raw_call(
            "group_poke", {"user_id": user_id, "group_id": group_id}
        )

    # ---------- 状态 ----------
    async def get_status(self) -> Any:
        return await self._raw_call("get_status", {})

    async def get_login_info(self) -> Any:
        return await self._raw_call("get_login_info", {})
