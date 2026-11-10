"""模拟 QQ 官方机器人开放平台的假服务端。

- WebSocket 网关：鉴权(op=2) → 推送 GROUP_AT_MESSAGE_CREATE / C2C 事件
- HTTP API：接收 POST /v2/groups/{openid}/messages 等被动回复

真实部署里这一端是 api.sgroup.qq.com（腾讯官方）。
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websockets

logger = logging.getLogger("fake.official_qq")

TOKEN = "official-test-token"


class FakeOfficialQQServer:
    def __init__(self, ws_host: str = "127.0.0.1", ws_port: int = 6703,
                 http_port: int = 6704, token: str = TOKEN):
        self.ws_host = ws_host
        self.ws_port = ws_port
        self.http_port = http_port
        self.token = token
        self.http_posts: list[dict] = []      # 收到的所有 HTTP POST（path + body）
        self._clients: set = set()
        self._ws_server = None
        self._http_server: ThreadingHTTPServer | None = None

    async def start(self):
        self._ws_server = await websockets.serve(
            self._ws_handler, self.ws_host, self.ws_port, max_size=4 * 1024 * 1024
        )
        self._http_server = ThreadingHTTPServer((self.ws_host, self.http_port), self._make_handler())
        threading.Thread(target=self._http_server.serve_forever, daemon=True).start()
        logger.info(
            "假官方服务端启动: ws://%s:%d + http://%s:%d",
            self.ws_host, self.ws_port, self.ws_host, self.http_port,
        )

    async def stop(self):
        if self._ws_server:
            self._ws_server.close()
            await self._ws_server.wait_closed()
        if self._http_server:
            self._http_server.shutdown()

    # ---------------- WS 网关 ----------------
    async def _ws_handler(self, ws):
        authed = False
        req = getattr(ws, "request", None)
        headers = {k.lower(): v for k, v in (req.headers or {}).items()} if req else {}
        if headers.get("authorization") == f"QQBot {self.token}":
            authed = True
        if not authed:
            await ws.close(code=4001, reason="鉴权失败")
            return

        self._clients.add(ws)
        # Hello (op=10)
        await ws.send(json.dumps({"op": 10, "d": {"heartbeat_interval": 30000}}))
        try:
            async for raw in ws:
                data = json.loads(raw)
                if data.get("op") == 2:  # 鉴权
                    await ws.send(json.dumps({"op": 0, "s": 1, "t": "READY", "d": {"user": {"id": "app"}}}))
                elif data.get("op") == 1:  # 心跳
                    await ws.send(json.dumps({"op": 11}))
        finally:
            self._clients.discard(ws)

    # ---------------- 事件推送 ----------------
    async def _push(self, payload: dict):
        for ws in list(self._clients):
            await ws.send(json.dumps(payload, ensure_ascii=False))

    async def push_group_at_message(self, group_openid: str, member_openid: str,
                                   content: str, event_id: int = 1):
        await self._push({
            "op": 0, "id": event_id, "t": "GROUP_AT_MESSAGE_CREATE",
            "d": {
                "id": f"msg-{event_id}",
                "group_openid": group_openid,
                "content": f"<@!APP123> {content}",
                "author": {"member_openid": member_openid, "union_openid": f"u-{member_openid}"},
                "timestamp": "1700000000",
            },
        })

    async def push_c2c_message(self, user_openid: str, content: str, event_id: int = 2):
        await self._push({
            "op": 0, "id": event_id, "t": "C2C_MESSAGE_CREATE",
            "d": {
                "id": f"msg-{event_id}",
                "content": content,
                "author": {"user_openid": user_openid},
                "timestamp": "1700000000",
            },
        })

    # ---------------- HTTP API ----------------
    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.http_posts.append({"path": self.path, "body": body})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"code":0}')

            def log_message(self, *args):
                pass

        return Handler

    def find_post(self, fragment: str) -> dict | None:
        for p in self.http_posts:
            if fragment in p["path"]:
                return p
        return None
