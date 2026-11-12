"""模拟 OneBot 11 实现的假服务端（正向 WS）。

用于端到端测试：推送各类事件、响应 API 调用、校验鉴权。
在真实部署里，这一端是 NapCat / Lagrange 等。
"""

from __future__ import annotations

import asyncio
import json
import logging

import websockets

logger = logging.getLogger("fake.onebot")

SELF_ID = 10000
ACCESS_TOKEN = "test-token"


class FakeOneBotServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 6701, token: str = ACCESS_TOKEN):
        self.host = host
        self.port = port
        self.token = token
        self.received: list[dict] = []      # 收到的所有 API 调用
        self.echo_seq = 0
        self._clients: set = set()
        self._server = None

    async def start(self):
        self._server = await websockets.serve(
            self._handler, self.host, self.port, max_size=8 * 1024 * 1024
        )
        logger.info("假 OneBot 服务端启动: ws://%s:%d", self.host, self.port)

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handler(self, ws):
        # 鉴权：Authorization header 或 access_token query
        path = ws.request.path if hasattr(ws, "request") and ws.request else getattr(ws, "path", "/")
        auth_ok = False
        headers = {}
        req = getattr(ws, "request", None)
        if req is not None:
            headers = {k.lower(): v for k, v in (req.headers or {}).items()}
            path = req.path
        if headers.get("authorization") == f"Bearer {self.token}":
            auth_ok = True
        if f"access_token={self.token}" in (path or ""):
            auth_ok = True
        if self.token and not auth_ok:
            await ws.close(code=4001, reason="鉴权失败")
            return

        self._clients.add(ws)
        try:
            # 连接生命周期事件（规范：正向 WS 会推送 connect）
            await self._push(ws, {
                "time": _ts(), "self_id": SELF_ID,
                "post_type": "meta_event",
                "meta_event_type": "lifecycle", "sub_type": "connect",
            })
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                self.received.append(data)
                action = data.get("action", "")
                echo = data.get("echo")
                resp = self._handle_api(action, data.get("params") or {})
                resp["echo"] = echo
                await ws.send(json.dumps(resp, ensure_ascii=False))
        finally:
            self._clients.discard(ws)

    # ---------- 模拟 API ----------
    def _handle_api(self, action: str, params: dict) -> dict:
        if action in ("send_private_msg", "send_group_msg", "get_status", "get_login_info",
                      "friend_poke", "group_poke", "set_group_ban", "set_group_kick"):
            data = {
                "send_private_msg": {"message_id": 900001},
                "send_group_msg": {"message_id": 900002},
                "get_status": {"online": True, "good": True},
                "get_login_info": {"user_id": SELF_ID, "nickname": "方舟测试号"},
            }.get(action)
            return {"status": "ok", "retcode": 0, "data": data}
        return {"status": "failed", "retcode": 1404, "data": None}

    # ---------- 推送事件 ----------
    async def _push(self, ws, event: dict):
        await ws.send(json.dumps(event, ensure_ascii=False))

    async def push_private_message(self, user_id: int, text: str, message_id: int = 100):
        ev = {
            "time": _ts(), "self_id": SELF_ID, "post_type": "message",
            "message_type": "private", "sub_type": "friend",
            "message_id": message_id, "user_id": user_id,
            "message": [{"type": "text", "data": {"text": text}}],
            "raw_message": text, "font": 0,
            "sender": {"user_id": user_id, "nickname": f"用户{user_id}", "sex": "unknown", "age": 0},
        }
        for ws in list(self._clients):
            await self._push(ws, ev)

    async def push_group_message(self, group_id: int, user_id: int, text: str,
                                 at_bot: bool = False, message_id: int = 200):
        segs = []
        if at_bot:
            segs.append({"type": "at", "data": {"qq": str(SELF_ID)}})
        segs.append({"type": "text", "data": {"text": text}})
        raw = ("[CQ:at,qq=%d]" % SELF_ID if at_bot else "") + text
        ev = {
            "time": _ts(), "self_id": SELF_ID, "post_type": "message",
            "message_type": "group", "sub_type": "normal",
            "message_id": message_id, "group_id": group_id, "user_id": user_id,
            "message": segs, "raw_message": raw, "font": 0,
            "sender": {
                "user_id": user_id, "nickname": f"用户{user_id}",
                "card": "", "sex": "unknown", "age": 0, "area": "",
                "level": "1", "role": "member", "title": "",
            },
        }
        for ws in list(self._clients):
            await self._push(ws, ev)

    async def push_group_increase(self, group_id: int, user_id: int):
        ev = {
            "time": _ts(), "self_id": SELF_ID, "post_type": "notice",
            "notice_type": "group_increase", "sub_type": "approve",
            "group_id": group_id, "user_id": user_id,
        }
        for ws in list(self._clients):
            await self._push(ws, ev)

    async def push_friend_request(self, user_id: int):
        ev = {
            "time": _ts(), "self_id": SELF_ID, "post_type": "request",
            "request_type": "friend", "user_id": user_id,
            "comment": "我是测试", "flag": "flag-123",
        }
        for ws in list(self._clients):
            await self._push(ws, ev)

    async def push_custom(self, event: dict):
        """推送任意原始事件（测试用）。"""
        event.setdefault("time", _ts())
        event.setdefault("self_id", SELF_ID)
        for ws in list(self._clients):
            await self._push(ws, event)

    def find_call(self, action: str) -> dict | None:
        """查询框架是否发起过某个 API 调用。"""
        for c in self.received:
            if c.get("action") == action:
                return c
        return None


def _ts() -> int:
    import time
    return int(time.time())
