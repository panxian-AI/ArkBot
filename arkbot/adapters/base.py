"""ArkBot 适配器抽象层 —— 框架抗 ban 的核心。

适配器只负责一件事：把「某个 QQ 接入方案」翻译成 ArkBot 的统一事件流
和统一 API 调用。插件永远不关心适配器是谁。

- 某个 OneBot 实现被 ban？换实现，改一行 url。
- QQ 修掉了所有 OneBot 实现依赖的逆向协议（OneBot 系全灭）？
  官方 API 适配器（official_qq）作为异构备份通道继续工作，
  插件代码一行不改。

多通道并行 + 故障切换规则：
  - 事件带来源（event.adapter），回复走来源通道；
  - 主动发送路由到第一个健康通道；
  - 单通道死亡（断线、被 ban）不影响其余通道。
"""

from __future__ import annotations

import abc
import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ..event import Event

if TYPE_CHECKING:
    from ..message import MessageChain

logger = logging.getLogger("arkbot.adapter")

# 事件回调：适配器产出统一 Event，交给框架分发
EventHandler = Callable[[Event], Awaitable[None]]


class Adapter(abc.ABC):
    """QQ 接入方案的抽象基类。

    子类需要实现：
      - connect()          建立与 QQ 侧的连接（可断线重连）
      - call_api(action, params)   调用平台能力（发消息等）
      - 产出事件 → 调用 self._emit(event)
    可选实现：
      - send_reply(event, chain)   按来源通道回复（官方 API 等非 OneBot 协议需要）
    """

    name: str = "base"

    def __init__(self) -> None:
        self._handler: EventHandler | None = None
        self._running = False

    def on_event(self, handler: EventHandler) -> None:
        """注册事件回调（由框架注入）。"""
        self._handler = handler

    async def _emit(self, event: Event) -> None:
        """把一个事件交给框架分发（标记来源通道）。

        重要：分发必须作为独立任务运行。事件处理器里通常会回调 API（如快捷回复），
        而 API 响应要靠接收循环解析——如果内联 await，会形成死锁。
        """
        event.adapter = self
        if self._handler is not None:
            task = asyncio.get_running_loop().create_task(self._handler(event))
            task.add_done_callback(self._log_task_error)

    @staticmethod
    def _log_task_error(task: "asyncio.Task") -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("事件处理任务异常", exc_info=task.exception())

    # ---------------- 通道健康度 ----------------

    @property
    def is_connected(self) -> bool:
        """当前是否可用于发送。默认 False，子类按实际连接状态实现。"""
        return False

    async def wait_connected(self, timeout: float | None = None) -> bool:
        """等待通道就绪。默认实现直接返回健康状态。"""
        return self.is_connected

    # ---------------- 回复路由 ----------------

    async def send_reply(self, event: Event, chain: "MessageChain", quote: bool = True) -> bool:
        """按来源通道回复。返回 False 表示本通道不支持该事件的回复语义，
        由框架决定是否走其他通道。默认按 call_api('send_group/private_msg') 尝试。"""
        from ..message import MessageSegment

        if isinstance(event, Event) and getattr(event, "message_type", None):
            reply_seg = (
                [MessageSegment.reply(event.message_id).to_onebot()]
                if quote and event.message_id else []
            )
            if event.message_type == "group" and event.group_id:
                await self.call_api("send_group_msg", {
                    "group_id": event.group_id,
                    "message": [*reply_seg, *chain.to_onebot()],
                })
                return True
            if event.message_type == "private" and event.user_id:
                await self.call_api("send_private_msg", {
                    "user_id": event.user_id,
                    "message": [*reply_seg, *chain.to_onebot()],
                })
                return True
        return False

    # ---------------- 抽象接口 ----------------

    @abc.abstractmethod
    async def connect(self) -> None:
        """建立连接并开始接收事件（内部应循环重连直到 stop）。"""

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """断开连接，释放资源。"""

    @abc.abstractmethod
    async def call_api(self, action: str, params: dict[str, Any] | None = None) -> Any:
        """调用平台 API，返回 data 部分；失败抛 ApiError。"""

    async def run_forever(self) -> None:
        self._running = True
        await self.connect()

    async def stop(self) -> None:
        self._running = False
        await self.disconnect()


class ApiError(Exception):
    """API 调用失败。"""

    def __init__(self, action: str, retcode: int, message: str = ""):
        self.action = action
        self.retcode = retcode
        self.message = message
        super().__init__(f"API {action!r} 失败 retcode={retcode} {message}")
