"""模拟 AstrBot / DSH 的本地 HTTP 服务，用于离线端到端测试。

只用 asyncio.start_server 手写极简 HTTP，避免引入 aiohttp。
AstrBot 假服务按官方文档用 SSE 分片返回，用来验证流式拼接。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


class FakeHttpBase:
    def __init__(self, port: int = 0):
        self.port = port
        self.requests: list[dict[str, Any]] = []
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._server = None

    # ---- 极简 HTTP 解析 ----
    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except Exception:  # noqa: BLE001
            writer.close()
            return
        raw_head, _, rest = head.partition(b"\r\n\r\n")
        lines = raw_head.decode("latin1").split("\r\n")
        try:
            method, path, _ = lines[0].split(" ")
        except ValueError:
            writer.close()
            return
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        need = int(headers.get("content-length") or 0)
        body = rest
        while len(body) < need:
            chunk = await reader.read(need - len(body))
            if not chunk:
                break
            body += chunk
        try:
            await self.handle(method, path, headers, body, writer)
        except Exception as e:  # noqa: BLE001
            await self._json(writer, 500, {"error": str(e)})
        finally:
            try:
                await writer.drain()
            except Exception:  # noqa: BLE001
                pass
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    async def _json(writer: asyncio.StreamWriter, status: int, payload: Any) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        writer.write(
            f"HTTP/1.1 {status} OK\r\nContent-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode("latin1") + raw
        )
        await writer.drain()

    async def handle(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
        writer: asyncio.StreamWriter,
    ) -> None:
        raise NotImplementedError

    def find(self, needle: str) -> dict[str, Any] | None:
        return next((r for r in self.requests if needle in r["path"]), None)


class FakeAstrBot(FakeHttpBase):
    """模拟 AstrBot v4.18+ 的 POST /api/v1/chat（SSE 流式）。"""

    def __init__(self, port: int = 0, api_key: str = "abk_test", reply: str | None = None):
        super().__init__(port)
        self.api_key = api_key
        self.reply = reply

    def _answer(self, payload: dict[str, Any]) -> str:
        if self.reply is not None:
            return self.reply
        return f"回声：{payload.get('message', '')}"

    async def handle(self, method, path, headers, body, writer):
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        self.requests.append({"path": path, "headers": headers, "json": payload})

        if path.startswith("/api/v1/chat"):
            if self.api_key and headers.get("authorization") != f"Bearer {self.api_key}":
                await self._json(writer, 401, {"error": "unauthorized"})
                return
            text = self._answer(payload)
            # 按两片返回，验证 SSE 分片拼接
            mid = max(1, len(text) // 2)
            pieces = [text[:mid], text[mid:]] if len(text) > 1 else [text]
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\nConnection: close\r\n\r\n"
            )
            for piece in pieces:
                event = json.dumps(
                    {"type": "message", "data": {"content": {"type": "text", "data": {"text": piece}}}},
                    ensure_ascii=False,
                )
                writer.write(f"data: {event}\n\n".encode("utf-8"))
                await writer.drain()
            writer.write(b"data: [DONE]\n\n")
            return

        await self._json(writer, 404, {"error": f"no such path {path}"})


class FakeDsh(FakeHttpBase):
    """模拟 DSH 养鱼插件的本地接口（JSON 一次返回）。"""

    def __init__(self, port: int = 0, reply: str | None = None, delay: float = 0.0):
        super().__init__(port)
        self.reply = reply
        self.delay = delay

    async def handle(self, method, path, headers, body, writer):
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            payload = {}
        self.requests.append({"path": path, "headers": headers, "json": payload})
        if path.startswith("/api/chat"):
            if self.delay:
                await asyncio.sleep(self.delay)
            text = self.reply if self.reply is not None else f"大鱼说：{payload.get('message', '')}"
            await self._json(
                writer, 200,
                {"reply": text, "session": payload.get("session_id") or payload.get("session") or ""},
            )
            return
        await self._json(writer, 404, {"error": f"no such path {path}"})
