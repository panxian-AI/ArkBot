"""OneBot 11 兼容层（服务端侧）——让「以 OneBot 为底层」的软件直接接进来。

ArKbot 原来的适配器是 **客户端**：连到 NapCat / Lagrange 这类 OneBot 实现上。
本模块补上另一半能力：ArkBot 自己**扮演一个 OneBot 11 实现**，对外提供

  - 正向 WebSocket 服务端：ws://host:port/ 、/event、/api（NoneBot2 正向、
    Koishi、自研脚本都是这么连的）
  - 反向 WebSocket 客户端：主动连到 AstrBot / NoneBot2 的反向 WS 端口
  - HTTP API：POST /send_msg 等（curl / 各种 SDK 直接调）
  - HTTP 事件上报：把事件 POST 到指定 URL

于是链路变成：

    NoneBot2 / Koishi / AstrBot / 老脚本
                 │  OneBot 11（它们一行不用改）
                 ▼
            ArkBot 兼容层
                 │  统一事件 / 统一 API
                 ▼
      NapCat / 官方 QQ / 未来任意通道   ← 换通道不影响上层

这就是「底层换了，上层软件照旧」的关键：协议边界在 ArkBot 这里，
而不是在 QQ 接入方式那里。
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
import urllib.request
from dataclasses import asdict
from typing import Any, Iterable

try:  # websockets 用于「反向 WS 客户端」；服务端是标准库实现
    import websockets
except ImportError:  # pragma: no cover - 依赖缺失时降级为正/HTTP 模式
    websockets = None  # type: ignore[assignment]

from ..adapters.base import ApiError
from ..event import Event, MessageEvent
from ..message import MessageChain, MessageSegment
from .ws import HTTPRequest, HTTPResponse, WSConnection, WebSocketServer

logger = logging.getLogger("arkbot.compat.onebot11")

VERSION = "v11"


class OneBot11Server:
    """把 ArkBot 暴露成一个 OneBot 11 实现。

    只需要宿主提供 `app.bot`（含 call_api / send_group_msg / send_private_msg /
    healthy / self_id），因此 arkbot 与 arkbot2 两套内核都能复用。
    """

    def __init__(
        self,
        app: Any,
        *,
        host: str = "127.0.0.1",
        port: int = 6800,
        access_token: str | None = None,
        reverse_urls: Iterable[str] = (),
        http_report_urls: Iterable[str] = (),
        heartbeat_interval: float = 30.0,
        self_id: int = 0,
        nickname: str = "ArkBot",
    ):
        self.app = app
        self.host = host
        self.port = int(port)
        self.access_token = access_token or None
        self.reverse_urls = [_expand_reverse_url(u) for u in reverse_urls if u]
        self.http_report_urls = [u for u in http_report_urls if u]
        self.heartbeat_interval = float(heartbeat_interval or 0)
        self._self_id_override = int(self_id or 0)
        self.nickname = nickname

        self._server: WebSocketServer | None = None
        self._clients: set[Any] = set()          # WSConnection / 反向连接包装
        self._tasks: set[asyncio.Task[Any]] = set()
        self._running = False
        self._stop_evt = asyncio.Event()
        self._msg_seq = itertools.count(1)
        self._sent: dict[int, dict[str, Any]] = {}
        self._event_count = 0
        # 本地知识库：从真实事件里学到的成员信息 / 消息快照。
        # 上层软件（尤其 AstrBot）会在处理一条消息时同步反问「这个人是谁」
        # 「这条被引用的消息是什么」，下游通道不一定答得上来（官方通道压根没这些接口）。
        # 有缓存就先答，答不上来也不报错 —— 详见 _api_member_info 的注释。
        self._members: dict[tuple[int, int], dict[str, Any]] = {}
        self._strangers: dict[int, dict[str, Any]] = {}
        self._known_msgs: dict[int, dict[str, Any]] = {}
        self._warned_no_self_id = False

    MEMBER_CACHE_MAX = 2000
    MSG_CACHE_MAX = 500

    def _spawn(self, coro: Any, name: str | None = None) -> asyncio.Task[Any]:
        """托管后台任务：随 server 一起取消，结束后自动出表。"""
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)

        def _done(t: asyncio.Task[Any]) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.error("兼容层任务异常", exc_info=t.exception())

        task.add_done_callback(_done)
        return task

    # ================= 生命周期 =================

    async def start(self) -> "OneBot11Server":
        if self._running:
            return self
        self._running = True
        self._server = WebSocketServer(
            self._handle_ws, self._handle_http, host=self.host, port=self.port
        )
        await self._server.start()
        self.port = self._server.port
        logger.info(
            "OneBot 11 兼容层已就绪：ws://%s:%d/  http://%s:%d/  （NoneBot2 / Koishi / AstrBot / 老脚本可直接接入）",
            self.host, self.port, self.host, self.port,
        )
        await self._resolve_self_id()
        for url in self.reverse_urls:
            logger.info("反向 WS 目标: %s", url)
            self._spawn(self._reverse_loop(url), f"ark-compat-rev-{url}")
        if self.heartbeat_interval > 0:
            self._spawn(self._heartbeat_loop(), "ark-compat-heartbeat")
        return self

    async def serve_forever(self) -> None:
        """给 TaskGroup 用的常驻入口。"""
        await self.start()
        try:
            await self._stop_evt.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        self._running = False
        self._stop_evt.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for conn in list(self._clients):
            try:
                await self._close_client(conn)
            except Exception:
                pass
        self._clients.clear()
        if self._server is not None:
            await self._server.stop()
            self._server = None
        logger.info("OneBot 11 兼容层已停止")

    # ================= 身份与鉴权 =================

    def _self_id(self) -> int:
        if self._self_id_override:
            return self._self_id_override
        bot = getattr(self.app, "bot", None)
        return int(getattr(bot, "self_id", 0) or 0)

    async def _resolve_self_id(self) -> None:
        """self_id 未知时向下游问一次（不阻塞启动，失败就算了）。

        上层软件（尤其 AstrBot）用 self_id 判断「这条消息是不是 @ 我」，
        拿 0 会让群里 @ 机器人彻底失效，所以值得主动补一次。
        """
        if self._self_id():
            return
        bot = getattr(self.app, "bot", None)
        if bot is None:
            return
        try:
            info = await asyncio.wait_for(bot.call_api("get_login_info"), timeout=5)
        except Exception:
            return
        try:
            uid = int((info or {}).get("user_id") or 0)
        except (TypeError, ValueError, AttributeError):
            return
        if uid:
            self._self_id_override = uid
            try:
                bot.self_id = uid
            except Exception:
                pass
            nickname = (info or {}).get("nickname")
            if nickname:
                self.nickname = str(nickname)
            logger.info("兼容层已从下游取得 self_id=%d（%s）", uid, self.nickname)

    def _online(self) -> bool:
        bot = getattr(self.app, "bot", None)
        try:
            return bool(bot is not None and bot.healthy())
        except Exception:
            return False

    def _auth_ok(self, headers: dict[str, str], query: dict[str, str]) -> bool:
        if not self.access_token:
            return True
        auth = (headers.get("authorization") or "").strip()
        if auth in (f"Bearer {self.access_token}", f"Token {self.access_token}"):
            return True
        return query.get("access_token") == self.access_token

    # ================= HTTP =================

    async def _handle_http(self, req: HTTPRequest) -> HTTPResponse:
        if not self._auth_ok(req.headers, req.query):
            return HTTPResponse.json(
                {"status": "failed", "retcode": 401, "data": None, "msg": "access token 校验失败"},
                status=401,
            )

        body = req.json()
        action = str(body.get("action") or req.query.get("action") or "").strip()
        if not action:
            path = req.path.strip("/")
            if path not in ("", "health", "status", "api", "event", "ws"):
                action = path.split("/")[-1]
        # 兼容 POST /send_msg 的「params 直接在体里」写法
        params: dict[str, Any] = {}
        if isinstance(body.get("params"), dict):
            params.update(body["params"])
        elif body and "action" not in body:
            params.update({k: v for k, v in body.items()})
        params.update(
            {k: v for k, v in req.query.items() if k not in ("access_token", "action")}
        )

        echo = body.get("echo")
        if not action:
            # 浏览器 / 探活
            return HTTPResponse.json(self._status_payload(echo))

        retcode, data, wording = await self._dispatch(action, params)
        payload: dict[str, Any] = {
            "status": "ok" if retcode == 0 else "failed",
            "retcode": retcode,
            "data": data,
        }
        if wording:
            payload["msg"] = wording
            payload["wording"] = wording
        if echo is not None:
            payload["echo"] = echo
        return HTTPResponse.json(payload)

    # ================= WebSocket =================

    async def _handle_ws(self, conn: WSConnection) -> None:
        if not self._auth_ok(conn.headers, conn.query):
            await conn.close(1008, "access token 校验失败")
            return
        self._clients.add(conn)
        logger.info("OneBot 客户端接入: %s %s", conn.remote, conn.path)
        try:
            await conn.send_json(self._meta_event("lifecycle", "connect"))
            while True:
                raw = await conn.recv()
                if raw is None:
                    break
                try:
                    data = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    logger.debug("忽略非 JSON 帧: %r", raw[:200])
                    continue
                if not isinstance(data, dict):
                    continue
                if "action" in data:
                    # 单独建任务：慢 API 不能卡住后面的请求
                    self._spawn(self._answer(conn, data))
                elif data.get("post_type"):
                    # 极少数客户端会把事件回推，忽略以免自己跟自己转圈
                    logger.debug("忽略客户端回推事件: %s", data.get("post_type"))
        finally:
            self._clients.discard(conn)
            logger.info("OneBot 客户端断开: %s", conn.remote)

    async def _answer(self, conn: Any, req: dict[str, Any]) -> None:
        action = str(req.get("action") or "")
        params = req.get("params") if isinstance(req.get("params"), dict) else {}
        retcode, data, wording = await self._dispatch(action, dict(params or {}))
        payload: dict[str, Any] = {
            "status": "ok" if retcode == 0 else "failed",
            "retcode": retcode,
            "data": data,
            "echo": req.get("echo"),
        }
        if wording:
            payload["msg"] = wording
            payload["wording"] = wording
        try:
            await conn.send_json(payload)
        except Exception:
            logger.debug("响应 %s 失败（连接可能已断开）", action)

    # ================= 反向 WS（客户端侧） =================

    async def _reverse_loop(self, url: str) -> None:
        if websockets is None:  # pragma: no cover
            logger.error("未安装 websockets，无法启用反向 WS: %s", url)
            return
        backoff = 3.0
        while self._running:
            try:
                headers = {
                    "X-Self-ID": str(self._self_id()),
                    "X-Client-Role": "Universal",
                    "User-Agent": f"ArkBot/{VERSION}",
                }
                if self.access_token:
                    headers["Authorization"] = f"Bearer {self.access_token}"
                if not self._self_id() and not self._warned_no_self_id:
                    self._warned_no_self_id = True
                    # aiocqhttp 用 X-Self-ID 做 API 路由键；0 不是合法 QQ 号，
                    # AstrBot 的「是否 @ 了我」判断也依赖它，所以这里必须提醒
                    logger.warning(
                        "兼容层 self_id 为 0：AstrBot 的 @ 判定与多账号路由会失准。"
                        "请在 [compat] self_id 里显式填写机器人 QQ 号"
                    )
                async with _ws_connect(
                    url, headers=headers, max_size=64 * 1024 * 1024, ping_interval=20
                ) as ws:
                    backoff = 3.0
                    conn = _ReverseConn(ws, url)
                    self._clients.add(conn)
                    logger.info("反向 WS 已连接: %s", url)
                    try:
                        await conn.send_json(self._meta_event("lifecycle", "connect"))
                        async for raw in ws:
                            try:
                                data = json.loads(raw)
                            except (json.JSONDecodeError, TypeError):
                                continue
                            if isinstance(data, dict) and "action" in data:
                                # 单独建任务：一条慢 API 不能卡住后面的请求
                                # （aiocqhttp 的 api_timeout 是 180s，串行会连锁超时）
                                self._spawn(self._answer(conn, data))
                    finally:
                        self._clients.discard(conn)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("反向 WS %s 断开/失败: %s，%.0fs 后重连", url, e, backoff)
            if not self._running:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    # ================= 事件推送 =================

    async def push_event(self, event: Event) -> None:
        """把一个框架事件广播给所有兼容层客户端。"""
        if not self._running:
            return
        payload = self.to_onebot_event(event)
        self._remember(payload)
        self._event_count += 1
        clients = list(self._clients)
        if clients:
            await asyncio.gather(
                *(self._safe_send(c, payload) for c in clients), return_exceptions=True
            )
        for url in self.http_report_urls:
            self._spawn(self._post_report(url, payload), "ark-compat-report")

    # ================= 本地知识库 =================

    def _remember(self, payload: dict[str, Any]) -> None:
        """从推给上层的事件里留存「成员信息」和「消息快照」。

        只缓存真实发生过的数据（事件里带来的 sender / message），不猜、不造。
        作用是让上层软件反问 `get_group_member_info` / `get_msg` 时，
        即使下游通道不支持这些接口，也能拿到真实答案。
        """
        post_type = payload.get("post_type")
        if post_type != "message":
            return
        try:
            mid = int(payload.get("message_id") or 0)
            uid = int(payload.get("user_id") or 0)
            gid = int(payload.get("group_id") or 0)
        except (TypeError, ValueError):
            return
        if mid:
            self._known_msgs[mid] = payload
            if len(self._known_msgs) > self.MSG_CACHE_MAX:
                for key in list(self._known_msgs)[: self.MSG_CACHE_MAX // 4]:
                    self._known_msgs.pop(key, None)
        sender = payload.get("sender") or {}
        if not uid or not isinstance(sender, dict):
            return
        info = {
            "user_id": uid,
            "nickname": str(sender.get("nickname") or ""),
            "card": str(sender.get("card") or ""),
            "sex": str(sender.get("sex") or "unknown"),
            "age": int(sender.get("age") or 0),
            "area": str(sender.get("area") or ""),
            "level": str(sender.get("level") or ""),
            "role": str(sender.get("role") or "member"),
            "title": str(sender.get("title") or ""),
        }
        self._strangers[uid] = info
        if gid:
            self._members[(gid, uid)] = info
            if len(self._members) > self.MEMBER_CACHE_MAX:
                for key in list(self._members)[: self.MEMBER_CACHE_MAX // 4]:
                    self._members.pop(key, None)
        if len(self._strangers) > self.MEMBER_CACHE_MAX:
            for key in list(self._strangers)[: self.MEMBER_CACHE_MAX // 4]:
                self._strangers.pop(key, None)

    async def _safe_send(self, conn: Any, payload: dict[str, Any]) -> None:
        try:
            await conn.send_json(payload)
        except Exception as e:
            logger.debug("推送事件失败（%s）：%s", getattr(conn, "remote", "?"), e)
            self._clients.discard(conn)

    async def _post_report(self, url: str, payload: dict[str, Any]) -> None:
        def _do() -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "X-Self-ID": str(self._self_id()),
                "X-Client-Role": "Universal",
                "User-Agent": f"ArkBot/{VERSION}",
            }
            if self.access_token:
                headers["Authorization"] = f"Bearer {self.access_token}"
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
                resp.read()

        try:
            await asyncio.to_thread(_do)
        except Exception as e:
            logger.warning("HTTP 上报失败 %s: %s", url, e)

    async def _heartbeat_loop(self) -> None:
        interval_ms = int(self.heartbeat_interval * 1000)
        while self._running:
            await asyncio.sleep(self.heartbeat_interval)
            payload = self._meta_event("heartbeat", "")
            payload["interval"] = interval_ms
            for conn in list(self._clients):
                await self._safe_send(conn, payload)

    # ================= 事件格式转换 =================

    def to_onebot_event(self, event: Event) -> dict[str, Any]:
        """内部事件 → OneBot 11 事件 JSON（数组消息格式）。"""
        data: dict[str, Any] = {}
        for k, v in (event.raw or {}).items():
            if k.startswith("_") or v is None:   # 下划线字段是内部标记（如 _official）
                continue
            data[k] = v
        data["time"] = int(event.time or time.time())
        data["self_id"] = self._self_id() or int(event.self_id or 0)
        post_type = event.post_type or data.get("post_type") or "message"
        data["post_type"] = post_type

        if post_type == "message" and isinstance(event, MessageEvent):
            data["message_type"] = event.message_type or data.get("message_type", "private")
            data["sub_type"] = event.sub_type or data.get("sub_type", "normal")
            data["message_id"] = int(event.message_id)
            data["user_id"] = int(event.user_id)
            data["group_id"] = int(event.group_id)
            data["message"] = event.message.to_onebot()
            data["raw_message"] = event.raw_message or event.message.to_cq()
            data.setdefault("font", 0)
            data["sender"] = asdict(event.sender) if event.sender else {}
        elif post_type == "meta_event":
            data.setdefault("meta_event_type", "heartbeat")
            if data.get("meta_event_type") == "heartbeat":
                data.setdefault("interval", int(self.heartbeat_interval * 1000))
                data["status"] = {
                    "app_enabled": True,
                    "app_good": True,
                    "online": self._online(),
                    "good": True,
                }
        elif post_type == "notice":
            data.setdefault("notice_type", "")
        elif post_type == "request":
            data.setdefault("request_type", "")
        return data

    def _meta_event(self, meta_type: str, sub_type: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "time": int(time.time()),
            "self_id": self._self_id(),
            "post_type": "meta_event",
            "meta_event_type": meta_type,
            "status": {
                "app_enabled": True,
                "app_good": True,
                "online": self._online(),
                "good": True,
            },
        }
        if meta_type == "lifecycle":
            payload["sub_type"] = sub_type
            payload["interval"] = int(self.heartbeat_interval * 1000)
        return payload

    def _status_payload(self, echo: Any = None) -> dict[str, Any]:
        payload = {
            "status": "ok",
            "retcode": 0,
            "data": {
                "app_name": "ArkBot",
                "app_version": VERSION,
                "protocol_version": VERSION,
                "online": self._online(),
                "self_id": self._self_id(),
                "clients": len(self._clients),
                "events": self._event_count,
            },
        }
        if echo is not None:
            payload["echo"] = echo
        return payload

    # ================= API 分发 =================

    async def _dispatch(self, action: str, params: dict[str, Any]) -> tuple[int, Any, str]:
        action = action.strip().lstrip(".")
        try:
            if action == "get_login_info":
                return 0, {"user_id": self._self_id(), "nickname": self.nickname}, ""
            if action == "get_status":
                return 0, {"online": self._online(), "good": self._online()}, ""
            if action == "get_version_info":
                return 0, {
                    "app_name": "ArkBot",
                    "app_version": VERSION,
                    "protocol_version": VERSION,
                }, ""
            if action in ("can_send_image", "can_send_record"):
                return 0, {"yes": True}, ""
            if action == "get_csrf_token":
                return 0, {"token": 0}, ""
            if action == "get_cookies":
                return 0, {"cookies": ""}, ""
            if action in ("set_restart", "clean_cache"):
                return 0, None, ""
            if action in ("handle_quick_operation", "handle_quick_operation_async"):
                # aiocqhttp 在事件处理函数返回 dict 时会回灌这个动作，空操作即可
                return 0, None, ""
            if action in ("send_msg", "send_private_msg", "send_group_msg"):
                return await self._api_send(action, params)
            if action in ("get_group_member_info", "get_stranger_info"):
                return await self._api_member_info(action, params)
            if action == "delete_msg":
                mid = int(params.get("message_id") or 0)
                await self.app.bot.delete_msg(mid)
                self._sent.pop(mid, None)
                self._known_msgs.pop(mid, None)
                return 0, None, ""
            if action == "get_msg":
                mid = int(params.get("message_id") or 0)
                # 先答「自己发过的」和「刚收到过的」——上层软件（AstrBot 的引用回复）
                # 会在处理消息的同一时刻同步回查，依赖下游必有回执是不现实的
                for cache in (self._sent, self._known_msgs):
                    cached = cache.get(mid)
                    if cached is not None:
                        return 0, cached, ""
                try:
                    return 0, await self.app.bot.get_msg(mid), ""
                except ApiError:
                    return 0, {}, ""
            # 其余动作全部透传给下游通道：接的是 OneBot 实现就原生支持，
            # 接的是官方 QQ 时由官方适配器尽力翻译，不支持的返回 1404。
            return 0, await self.app.bot.call_api(action, **self._clean_params(params)), ""
        except ApiError as e:
            retcode = int(e.retcode or 100)
            if retcode in (-1, 100) and not self._online():
                return 100, None, "当前无可用 QQ 通道"
            if retcode == -1:
                # 有通道但没人认识这个动作 → OneBot 规范里的 1404
                return 1404, None, f"不支持的 API: {action}"
            return retcode, None, e.message
        except Exception as e:
            logger.exception("兼容层执行 %s 失败", action)
            return 100, None, str(e)

    async def _api_member_info(
        self, action: str, params: dict[str, Any]
    ) -> tuple[int, Any, str]:
        """`get_group_member_info` / `get_stranger_info`：先本地、再下游、最后降级。

        为什么不能直接透传：AstrBot（aiocqhttp）在处理**每条带 @ 的群消息**时都会
        同步调这两个接口问「被 @ 的是谁」。下游是官方 QQ 通道时根本没有这个接口，
        透传会返回 1404 → AstrBot 的 handler 抛异常 → **整条消息被丢弃**，
        群里 @ 机器人完全没反应。所以这里的契约是「尽量答，答不上来也给个
        结构合法的空档案」，让对方能继续往下走。
        """
        try:
            group_id = int(params.get("group_id") or 0)
            user_id = int(params.get("user_id") or 0)
        except (TypeError, ValueError):
            group_id = user_id = 0

        cached = self._members.get((group_id, user_id)) if group_id else None
        cached = cached or self._strangers.get(user_id)
        if cached:
            return 0, dict(cached), ""

        try:
            data = await self.app.bot.call_api(action, **self._clean_params(params))
        except ApiError as e:
            retcode = int(e.retcode or 100)
            # 下游不认识这个动作 / 没有通道 / 通道限速：降级为「已知 ID、未知昵称」
            if retcode in (1404, 429, 100, -1) or not self._online():
                return 0, self._unknown_member(user_id, group_id), ""
            raise
        if isinstance(data, dict) and data:
            return 0, data, ""
        # 下游返回空（官方通道常见）：同样降级，不让上层软件崩
        return 0, self._unknown_member(user_id, group_id), ""

    @staticmethod
    def _unknown_member(user_id: int, group_id: int = 0) -> dict[str, Any]:
        """结构合法的空档案：字段全在、值未知，上层软件可安全取用。"""
        return {
            "user_id": user_id,
            "nickname": "",
            "card": "",
            "sex": "unknown",
            "age": 0,
            "area": "",
            "level": "",
            "role": "member",
            "title": "",
            "group_id": group_id,
        }

    async def _api_send(self, action: str, params: dict[str, Any]) -> tuple[int, Any, str]:
        message = params.get("message")
        if params.get("auto_escape") and isinstance(message, str):
            # auto_escape=true：内容按纯文本处理，不解析 CQ 码
            chain = MessageChain(MessageSegment.text(str(message)))
        else:
            chain = MessageChain.parse(message)

        if action == "send_msg":
            mtype = params.get("message_type") or ("group" if params.get("group_id") else "private")
            action = "send_group_msg" if mtype == "group" else "send_private_msg"

        if action == "send_group_msg":
            group_id = int(params.get("group_id") or 0)
            if not group_id:
                return 100, None, "缺少 group_id"
            res = await self.app.bot.send_group_msg(group_id, chain)
            target = {"message_type": "group", "group_id": group_id}
        else:
            user_id = int(params.get("user_id") or 0)
            if not user_id:
                return 100, None, "缺少 user_id"
            res = await self.app.bot.send_private_msg(user_id, chain)
            target = {"message_type": "private", "user_id": user_id}

        mid = self._message_id(res)
        # 记录一条**结构完整**的消息事件，供 get_msg / delete_msg / 引用回复使用。
        # AstrBot 拿到 get_msg 的返回值会直接 Event.from_payload 重建事件，
        # 少一个 message_type 都会让引用回复退化成纯文本。
        self._sent[mid] = {
            "time": int(time.time()),
            "self_id": self._self_id(),
            "post_type": "message",
            "message_id": mid,
            "real_id": (res or {}).get("message_id") if isinstance(res, dict) else None,
            "message": chain.to_onebot(),
            "raw_message": chain.to_cq(),
            "font": 0,
            "sub_type": "normal" if target["message_type"] == "group" else "friend",
            "sender": {
                "user_id": self._self_id(),
                "nickname": self.nickname,
                "card": "",
                "role": "owner",
            },
            **target,
        }
        if len(self._sent) > 500:
            for key in list(self._sent)[:100]:
                self._sent.pop(key, None)
        return 0, {"message_id": mid}, ""

    def _message_id(self, res: Any) -> int:
        if isinstance(res, dict):
            for key in ("message_id", "msg_id", "id"):
                try:
                    if res.get(key) is not None:
                        return int(res[key])
                except (TypeError, ValueError):
                    continue
        elif isinstance(res, int):
            return res
        # 下游走离线发件箱（无即时回执）时，给客户端一个本地唯一 ID，
        # 避免上层软件因为 data 缺失而报错
        return 1_000_000_000 + next(self._msg_seq)

    # 这些字段是上层 SDK 的「协议内路由参数」或本框架的关键字参数，
    # 透传给下游会导致 TypeError / 语义错乱：
    #   self_id   aiocqhttp 每个动作都会带上（多账号路由用），单机器人实例不需要
    #   no_cache  AstrBot 查成员信息时带的分页/缓存开关，OneBot 规范里没有
    _DROP_PARAMS = ("via", "action", "echo", "self_id", "no_cache", "_allow_outbox")

    @classmethod
    def _clean_params(cls, params: dict[str, Any]) -> dict[str, Any]:
        """剔除会与 Bot.call_api 关键字参数冲突的字段。"""
        return {k: v for k, v in params.items() if k not in cls._DROP_PARAMS}

    # ================= 杂项 =================

    async def _close_client(self, conn: Any) -> None:
        close = getattr(conn, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result
        else:
            await conn.send_json({"post_type": "meta_event", "meta_event_type": "lifecycle",
                                  "sub_type": "disconnect", "self_id": self._self_id(),
                                  "time": int(time.time())})


def _expand_reverse_url(url: str) -> str:
    """`astrbot://127.0.0.1:6199` → `ws://127.0.0.1:6199/ws`。

    AstrBot 的 aiocqhttp 适配器固定监听 `/ws`、`/ws/api`、`/ws/event` 三个路径，
    而 Universal 角色（一条连接同时收发）只有 `/ws`。写全 URL 容易漏 path，
    所以给个只填 host:port 的简写。
    """
    url = str(url).strip()
    if not url.lower().startswith("astrbot://"):
        return url
    rest = url[len("astrbot://"):].rstrip("/")
    return f"ws://{rest}/ws"


def _ws_connect(url: str, *, headers: dict[str, str], **kwargs: Any) -> Any:
    """跨 websockets 版本的连接入口。

    14.0 把 `extra_headers` 改名成 `additional_headers`，而 <=13.x 只认旧名字；
    装错版本会导致反向 WS 一直「连接失败」并无限退避，所以两个名都试。
    """
    if websockets is None:  # pragma: no cover
        raise RuntimeError("未安装 websockets")
    try:
        return websockets.connect(url, additional_headers=headers, **kwargs)
    except TypeError:  # pragma: no cover - 老版本 websockets
        return websockets.connect(url, extra_headers=headers, **kwargs)


class _ReverseConn:
    """把 websockets 客户端包成与 WSConnection 同形的对象。"""

    def __init__(self, ws: Any, url: str):
        self._ws = ws
        self.url = url
        self.remote = url
        self._lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return getattr(self._ws, "closed", False)

    async def send_json(self, obj: Any) -> None:
        async with self._lock:
            await self._ws.send(json.dumps(obj, ensure_ascii=False))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await self._ws.close(code, reason)
        except Exception:
            pass
