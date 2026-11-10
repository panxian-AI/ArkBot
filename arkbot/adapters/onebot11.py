"""OneBot 11 正向 WebSocket 适配器。

连接任意 OneBot 11 实现（NapCat / Lagrange.Core / go-cqhttp / LLOneBot 等）
的正向 WS 端点（ws://host:port，兼容 /event /api / 路径差异）。
断线自动指数退避重连 —— 实现被 ban 换端点即可，业务无感。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import random
from typing import Any

import websockets

from ..event import Event
from .base import Adapter, ApiError

logger = logging.getLogger("arkbot.adapter.onebot11")


class OneBot11Adapter(Adapter):
    """正向 WS 客户端，单连接双通道（/ 路径同时收事件和调 API）。"""

    name = "onebot11"

    def __init__(
        self,
        url: str = "ws://127.0.0.1:6700",
        access_token: str | None = None,
        reconnect_interval: float = 3.0,
        max_backoff: float = 60.0,
    ):
        super().__init__()
        self.url = url.rstrip("/")
        self.access_token = access_token
        self.reconnect_interval = reconnect_interval
        self.max_backoff = max_backoff
        self._ws: Any | None = None
        self._echo_counter = itertools.count(1)
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._recv_task: asyncio.Task | None = None
        self._connected = asyncio.Event()

    # ---------------- 连接管理 ----------------

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    async def connect(self) -> None:
        backoff = self.reconnect_interval
        while self._running:
            try:
                headers = {}
                query = ""
                if self.access_token:
                    # 规范允许两种鉴权方式，同时带上兼容性最好
                    headers["Authorization"] = f"Bearer {self.access_token}"
                    query = f"?access_token={self.access_token}"
                logger.info("正在连接 OneBot 实现: %s", self.url)
                self._ws = await websockets.connect(
                    f"{self.url}/{query.lstrip('?') if query else ''}".rstrip("/"),
                    additional_headers=headers,
                    max_size=64 * 1024 * 1024,
                    ping_interval=20,
                )
                backoff = self.reconnect_interval
                logger.info("已连接 %s", self.url)
                self._connected.set()
                try:
                    await self._recv_loop()
                finally:
                    self._connected.clear()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("连接断开/失败: %s，%.1fs 后重连", e, backoff)
            if not self._running:
                break
            await asyncio.sleep(backoff + random.uniform(0, 1))
            backoff = min(backoff * 2, self.max_backoff)

    async def disconnect(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    async def wait_connected(self, timeout: float | None = None) -> bool:
        """等待连接就绪（供调度器等场景使用）。"""
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ---------------- 收事件 ----------------

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        async for raw in self._ws:
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                logger.warning("收到无法解析的消息: %r", raw)
                continue
            if "post_type" in data:
                event = Event.from_raw(data)
                await self._emit(event)
            elif "echo" in data:
                # API 响应
                echo = str(data["echo"])
                fut = self._pending.pop(echo, None)
                if fut is not None and not fut.done():
                    if data.get("status") == "ok" or data.get("retcode") == 0:
                        fut.set_result(data.get("data"))
                    else:
                        fut.set_exception(
                            ApiError(
                                str(echo),
                                int(data.get("retcode", -1)),
                                str(data.get("wording") or data.get("msg") or ""),
                            )
                        )
            else:
                logger.debug("忽略未知消息: %r", data)

    # ---------------- 调 API ----------------

    async def call_api(self, action: str, params: dict[str, Any] | None = None) -> Any:
        if self._ws is None or not self._connected.is_set():
            if not await self.wait_connected(timeout=10.0):
                raise ApiError(action, -1, "与 OneBot 实现的连接不可用")
        echo = f"ark-{next(self._echo_counter)}"
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[echo] = fut
        payload = {"action": action, "params": params or {}, "echo": echo}
        try:
            await self._ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            self._pending.pop(echo, None)
            raise ApiError(action, -1, f"发送失败: {e}") from e
        try:
            return await asyncio.wait_for(fut, timeout=60.0)
        except asyncio.TimeoutError:
            self._pending.pop(echo, None)
            raise ApiError(action, -1, "API 调用超时") from None
