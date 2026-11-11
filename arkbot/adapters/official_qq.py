"""QQ 官方机器人开放平台适配器 —— 异构备份通道。

不依赖任何 NTQQ 逆向/注入（OneBot 实现被 QQ 修复失效后，这条通道
依然可用——QQ 不可能封掉自己的官方开放平台）。

接入方式（官方 v2 群聊/单聊机器人）：
  - WebSocket 网关收事件（GROUP_AT_MESSAGE_CREATE / C2C_MESSAGE_CREATE 等）
  - HTTP POST 发消息（/v2/groups/{group_openid}/messages、/v2/users/{openid}/messages）

已知能力差异（相对 OneBot 通道）：
  - 群里只有被 @ 时才会收到消息（官方限制）
  - 主动消息：不携带 msg_id 的发送即主动消息，有频控
    （认证 60/qpm、未认证 30/qpm，每群每天 1000 条接收上限）；
    本适配器内置 30/qpm 保守限速，超限时抛 429，由框架降级到
    离线发件箱延迟补发 —— 主动消息「延后」而不是「丢失」
  - 数字 QQ 号 ↔ openid 映射：从收到的事件自动学习（CRC32 稳定伪 ID），
    插件照旧用数字 ID 调 send_group_msg/send_private_msg，本通道自动换算
  - 图片等富媒体需走富媒体接口（本适配器先支持文本 + 预留）

配置示例（bot.toml）::

    [[adapters]]
    type = "official_qq"
    app_id = "102345678"
    app_secret = "your-secret"
    # 可选，测试/代理时覆盖默认地址
    gateway_url = "wss://api.sgroup.qq.com/websocket"
    api_base = "https://api.sgroup.qq.com"
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections import deque
from typing import Any

import websockets

from ..event import Event, MessageEvent
from ..message import MessageChain, MessageSegment
from .base import Adapter, ApiError

logger = logging.getLogger("arkbot.adapter.official_qq")

# 官方 API 意图位：群@消息(1<<25=33554432) | C2C(1<<12=4096)
DEFAULT_INTENTS = 1 << 25 | 1 << 12
TOKEN_REFRESH_MARGIN = 600  # 提前 10 分钟刷新 access_token


def _pseudo_id(openid: str) -> int:
    """openid → 稳定伪数字 ID（CRC32 取正）。

    官方 API 只发 openid，OneBot 生态用数字 QQ 号；插件层统一用数字 ID，
    本映射在同一进程内稳定（同 openid 永远得到同数字），重启后事件
    会重新学习填充。
    """
    return zlib.crc32(openid.encode("utf-8")) & 0x7FFFFFFF


class OfficialQQAdapter(Adapter):
    """QQ 官方机器人 API 通道。"""

    name = "official_qq"

    def __init__(
        self,
        app_id: str = "",
        app_secret: str = "",
        gateway_url: str = "wss://api.sgroup.qq.com/websocket",
        api_base: str = "https://api.sgroup.qq.com",
        access_token: str = "",
        intents: int = DEFAULT_INTENTS,
        reconnect_interval: float = 3.0,
        max_backoff: float = 60.0,
    ):
        super().__init__()
        self.app_id = str(app_id)
        self.app_secret = str(app_secret)
        self.gateway_url = gateway_url
        self.api_base = api_base.rstrip("/")
        self._token = str(access_token)
        self.intents = int(intents)
        self.reconnect_interval = reconnect_interval
        self.max_backoff = max_backoff
        self._ws: Any | None = None
        self._connected = asyncio.Event()
        self._seq: int = 0
        self._msg_seq = random.randint(1, 10000)
        # 数字 ID ↔ openid 映射（从收到的事件自动学习，可落盘持久化）
        self._group_openid_by_id: dict[int, str] = {}
        self._user_openid_by_id: dict[int, str] = {}
        self._persist: Any = None
        # 主动消息限速：滑动窗口 60s（默认 30/qpm，未认证档保守值）
        self.active_rate_limit: int = 30
        self._active_times: deque[float] = deque()

    # ---------------- token ----------------

    async def _ensure_token(self) -> None:
        """有 app_secret 且无 token 时，从官方接口获取 access_token。"""
        if self._token or not (self.app_id and self.app_secret):
            return
        url = (
            "https://bots.qq.com/app/getAppAccessToken?"
            + urllib.parse.urlencode({"appId": self.app_id, "clientSecret": self.app_secret})
        )
        try:
            data = await asyncio.to_thread(self._http_json, "GET", url, None, {})
            self._token = str(data.get("access_token") or "")
            expires = int(data.get("expires_in") or "7200")
            logger.info("官方 access_token 获取成功，有效期 %ss", expires)
        except Exception as e:
            raise ApiError("getAppAccessToken", -1, f"获取官方 token 失败: {e}") from e

    # ---------------- 连接 ----------------

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    async def wait_connected(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def connect(self) -> None:
        await self._ensure_token()
        backoff = self.reconnect_interval
        while self._running:
            try:
                headers = {"Authorization": f"QQBot {self._token}"}
                if self.app_id:
                    headers["X-Union-Appid"] = self.app_id
                self._ws = await websockets.connect(
                    self.gateway_url,
                    additional_headers=headers,
                    max_size=16 * 1024 * 1024,
                    ping_interval=20,
                )
                # 鉴权 op=2（官方规范）
                await self._ws.send(json.dumps({
                    "op": 2,
                    "d": {
                        "token": f"QQBot {self._token}",
                        "intents": self.intents,
                        "shard": [0, 1],
                        "properties": {"$os": "arkbot", "$browser": "arkbot", "$device": "arkbot"},
                    },
                }))
                backoff = self.reconnect_interval
                self._connected.set()
                logger.info("官方 API 通道已连接: %s", self.gateway_url)
                try:
                    await self._recv_loop()
                finally:
                    self._connected.clear()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("官方通道断开/失败: %s，%.1fs 后重连", e, backoff)
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
        self._connected.clear()

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        async for raw in self._ws:
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            op = data.get("op")
            if op == 10:  # Hello → 立刻鉴权（上面已发，也响应心跳要求）
                continue
            if op == 1:  # 服务端要求心跳
                await self._ws.send(json.dumps({"op": 1, "d": self._seq}))
            elif op == 0:  # 事件分发
                self._seq = int(data.get("id") or self._seq)
                event = self._translate(data)
                if event is not None:
                    await self._emit(event)
            elif op == 11:  # 心跳 ACK
                continue
            elif op == 7:  # 需要重连
                raise ApiError("reconnect", 7, "官方网关要求重连")

    # ---------------- 事件翻译（官方 → ArkBot 统一模型） ----------------

    def _translate(self, data: dict[str, Any]) -> Event | None:
        d = data.get("d") or {}
        etype = str(d.get("type") or data.get("t") or "")
        content = str(d.get("content") or "").strip()
        # 官方群消息里 @机器人 的正文形如 "<@!APPID> 文本"
        to_me = True
        if content.startswith("<@!"):
            content = content.split(">", 1)[-1].strip()

        # openid → 稳定伪数字 ID（CRC32）：插件层照旧用数字 ID 工作，
        # 主动发送时本通道据此反查 openid
        user_id = 0
        group_id = 0
        official: dict[str, Any] = {}
        raw: dict[str, Any]
        if etype == "GROUP_AT_MESSAGE_CREATE":
            group_openid = str(d.get("group_openid") or "")
            member_openid = str((d.get("author") or {}).get("member_openid") or "")
            group_id = self._learn_group(group_openid)
            user_id = self._learn_user(member_openid)
            official = {
                "group_openid": group_openid,
                "member_openid": member_openid,
                "msg_id": str(d.get("id") or d.get("msg_id") or ""),
            }
            raw = {
                "post_type": "message",
                "message_type": "group",
                "sub_type": "normal",
                "message_id": 0,
                "user_id": user_id,
                "group_id": group_id,
                "message": [{"type": "text", "data": {"text": content}}],
                "raw_message": content,
                "sender": {"user_id": user_id, "nickname": "official-user"},
                "_official": official,
            }
        elif etype == "C2C_MESSAGE_CREATE":
            user_openid = str((d.get("author") or {}).get("user_openid") or "")
            user_id = self._learn_user(user_openid)
            official = {
                "user_openid": user_openid,
                "msg_id": str(d.get("id") or d.get("msg_id") or ""),
            }
            raw = {
                "post_type": "message",
                "message_type": "private",
                "sub_type": "friend",
                "message_id": 0,
                "user_id": user_id,
                "group_id": 0,
                "message": [{"type": "text", "data": {"text": content}}],
                "raw_message": content,
                "sender": {"user_id": user_id, "nickname": "official-user"},
                "_official": official,
            }
        else:
            # 其余官方事件（频道、进群等）暂不翻译，记录后忽略
            logger.debug("忽略官方事件类型: %s", etype)
            return None
        ev = Event.from_raw(raw)
        if isinstance(ev, MessageEvent):
            ev.to_me = to_me
        return ev

    # ---------------- 持久化 ----------------

    def attach_persist(self, persist: Any) -> None:
        """挂载持久化存储：映射表落盘 + 从磁盘恢复历史映射。

        由 App 在构建通道后调用。恢复映射意味着重启后无需等新事件，
        立即可向之前见过的群/用户主动发消息。
        """
        self._persist = persist
        if persist is None:
            return
        try:
            restored = 0
            for k in persist.keys("official.group."):
                pid = int(k.rsplit(".", 1)[1])
                self._group_openid_by_id[pid] = persist.get(k)
                restored += 1
            for k in persist.keys("official.user."):
                pid = int(k.rsplit(".", 1)[1])
                self._user_openid_by_id[pid] = persist.get(k)
                restored += 1
            if restored:
                logger.info("官方通道从磁盘恢复 %d 条 openid 映射", restored)
        except Exception:
            logger.exception("openid 映射恢复失败（忽略，等待新事件学习）")

    def _learn_group(self, openid: str) -> int:
        pseudo = _pseudo_id(openid)
        if openid and pseudo not in self._group_openid_by_id:
            self._group_openid_by_id[pseudo] = openid
            if self._persist is not None:
                self._persist.set(f"official.group.{pseudo}", openid)
        return pseudo

    def _learn_user(self, openid: str) -> int:
        pseudo = _pseudo_id(openid)
        if openid and pseudo not in self._user_openid_by_id:
            self._user_openid_by_id[pseudo] = openid
            if self._persist is not None:
                self._persist.set(f"official.user.{pseudo}", openid)
        return pseudo

    # ---------------- 回复（按官方协议语义） ----------------

    async def send_reply(self, event: Event, chain: MessageChain, quote: bool = True) -> bool:
        """官方通道回复：走 HTTP 被动回复（带 msg_id 才允许发）。

        quote 参数在此忽略：官方协议回复本身就是对 msg_id 的引用语义，
        这是平台层行为，对用户体验而言与 OneBot 的引用回复等效。
        """
        official = event.raw.get("_official")
        if not official or not isinstance(event, MessageEvent):
            return False
        msg_id = official.get("msg_id") or ""
        content = self._chain_to_official_text(chain)
        if event.message_type == "group":
            group_openid = official.get("group_openid")
            if not group_openid:
                return False
            data = await self._http_post(
                f"{self.api_base}/v2/groups/{group_openid}/messages",
                {"msg_type": 0, "msg_seq": self._next_seq(), "msg_id": msg_id, "content": content},
            )
            logger.info("官方群消息已回复 (group_openid=%s)", group_openid)
            return True
        if event.message_type == "private":
            user_openid = official.get("user_openid")
            if not user_openid:
                return False
            data = await self._http_post(
                f"{self.api_base}/v2/users/{user_openid}/messages",
                {"msg_type": 0, "msg_seq": self._next_seq(), "msg_id": msg_id, "content": content},
            )
            logger.info("官方单聊消息已回复 (user_openid=%s)", user_openid)
            return True
        return False

    @staticmethod
    def _chain_to_official_text(chain: MessageChain) -> str:
        """官方通道 v2 群聊暂支持纯文本；图片等段落降级为占位提示。"""
        parts: list[str] = []
        for seg in chain:
            if seg.type == "text":
                parts.append(str(seg.data.get("text", "")))
            elif seg.type == "at":
                parts.append(f"@{seg.data.get('qq', '')}")
            else:
                parts.append(f"[{seg.type}]")
        return "".join(parts).strip() or "…"

    def _next_seq(self) -> int:
        """官方要求同一 msg_id 下 msg_seq 递增。"""
        self._msg_seq += 1
        return self._msg_seq

    async def _rate_gate(self, action: str) -> None:
        """主动消息滑动窗口限速（默认 30/qpm）。

        超限抛 retcode=429：Bot 门面会将其视为「本通道暂不可用」，
        降级到其他通道或离线发件箱延迟补发。
        """
        now = time.monotonic()
        while self._active_times and now - self._active_times[0] > 60.0:
            self._active_times.popleft()
        if len(self._active_times) >= self.active_rate_limit:
            raise ApiError(action, 429, f"官方通道主动消息限速（{self.active_rate_limit}/分钟）")
        self._active_times.append(now)

    @classmethod
    def _message_param_to_text(cls, message: Any) -> str:
        """OneBot 风格 message 参数（list 段数组 / CQ 字符串 / MessageChain）→ 官方纯文本。"""
        if isinstance(message, MessageChain):
            return cls._chain_to_official_text(message)
        return cls._chain_to_official_text(MessageChain.parse(message))

    # ---------------- call_api（统一门面走官方 HTTP） ----------------

    async def call_api(self, action: str, params: dict[str, Any] | None = None) -> Any:
        params = params or {}
        if action == "get_status":
            return {"online": self.is_connected, "good": self.is_connected}
        if action == "get_login_info":
            return {"user_id": 0, "nickname": f"official-{self.app_id}"}
        if action == "send_group_msg":
            # 主动消息：不带 msg_id 直接发（消耗主动配额，内置限速）
            openid = self._group_openid_by_id.get(int(params.get("group_id") or 0))
            if not openid:
                raise ApiError(action, 1404, "官方通道无该群 openid 映射（未收到过该群消息）")
            await self._rate_gate(action)
            await self._http_post(
                f"{self.api_base}/v2/groups/{openid}/messages",
                {
                    "msg_type": 0,
                    "msg_seq": self._next_seq(),
                    "content": self._message_param_to_text(params.get("message")),
                },
            )
            logger.info("官方群主动消息已发送 (group=%s)", openid[:8] + "…")
            return {"message_id": 0}
        if action == "send_private_msg":
            openid = self._user_openid_by_id.get(int(params.get("user_id") or 0))
            if not openid:
                raise ApiError(action, 1404, "官方通道无该用户 openid 映射（未收到过该用户消息）")
            await self._rate_gate(action)
            await self._http_post(
                f"{self.api_base}/v2/users/{openid}/messages",
                {
                    "msg_type": 0,
                    "msg_seq": self._next_seq(),
                    "content": self._message_param_to_text(params.get("message")),
                },
            )
            logger.info("官方单聊主动消息已发送 (user=%s)", openid[:8] + "…")
            return {"message_id": 0}
        # 其余 OneBot 风格动作在本通道不支持 → 抛错，由 Bot 路由到其他通道
        raise ApiError(action, 1404, f"官方通道不支持动作 {action!r}（由框架路由其他通道）")

    # ---------------- HTTP 工具 ----------------

    def _http_json(self, method: str, url: str, body: dict | None, headers: dict) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"QQBot {self._token}")
        if self.app_id:
            req.add_header("X-Union-Appid", self.app_id)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read().decode("utf-8")
                return json.loads(text) if text.strip() else {}
        except urllib.error.HTTPError as e:
            raise ApiError(method, e.code, e.read().decode("utf-8", "ignore")[:200]) from e

    async def _http_post(self, url: str, body: dict) -> dict:
        return await asyncio.to_thread(self._http_json, "POST", url, body, {})
