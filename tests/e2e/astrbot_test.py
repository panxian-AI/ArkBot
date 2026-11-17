"""AstrBot（aiocqhttp 平台）接入 ArkBot —— 用真实 aiocqhttp 当对端。

和 `compat_test.py` 的区别：那里上层是 websockets 手写的「假客户端」，
这里上层是 **真实的 `aiocqhttp.CQHttp(use_ws_reverse=True)`**，也就是 AstrBot
`astrbot/core/platform/sources/aiocqhttp/` 实际用的那个库。协议细节（握手头、
`echo: {seq}` 应答匹配、`X-Self-ID` 路由、call_action 注入 self_id）全部由库
自己实现，ArkBot 只要错一点就会当场暴露。

链路：

    AstrBot（真实 aiocqhttp 反向 WS 服务端）
        ▲  ws://127.0.0.1:6199/ws   X-Client-Role: Universal
        │
    ArkBot 兼容层（反向 WS 客户端）   ← 本次要验证的
        │  OneBot 11 正向 WS（客户端）
        ▼
    假 OneBot 实现（扮演 NapCat）

同时按 AstrBot 的真实代码路径做转换与调用：
  - `event.sender["user_id"]`（直接下标，缺字段就崩）
  - `event.message` 必须是**数组**，否则 AstrBot 直接丢弃并报 critical
  - 收到 at 段时调 `get_group_member_info` / `get_stranger_info`（失败则丢 at）
  - 收到 reply 段时调 `get_msg`，并用返回数据 `Event.from_payload` 重建事件
  - 发送时 `send_group_msg(message=[...])`，并带 `self_id` 路由参数
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path[:0] = [str(_HERE), str(_HERE.parent), str(ROOT)]

try:  # 真实 AstrBot 用的就是这个库；没装就跳过（不影响 CI / 其余测试）
    from aiocqhttp import CQHttp, Event
except ImportError:  # pragma: no cover
    print(
        "SKIP: 未安装 aiocqhttp —— 这条用例要对端是真实协议栈才有意义。\n"
        "      pip install aiocqhttp quart   然后重跑。"
    )
    raise SystemExit(0)

from fakes.fake_onebot import ACCESS_TOKEN, SELF_ID, FakeOneBotServer  # noqa: E402

from arkbot.adapters.onebot11 import OneBot11Adapter  # noqa: E402
from arkbot.app import ArkBot  # noqa: E402
from arkbot.compat import OneBot11Server  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("astrbot-e2e")

PASS: list[str] = []
FAIL: list[str] = []
TOKEN = "astrbot-token"
DOWNSTREAM_PORT = 6721
ASTRBOT_PORT = 6199


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    logger.info("%s: %s %s", "通过" if cond else "失败", name, detail)


def ts() -> int:
    return int(time.time())


class AstrBotPeer:
    """复刻 AstrBot aiocqhttp 适配器的关键路径（不引入 AstrBot 全部依赖）。"""

    def __init__(self, port: int = ASTRBOT_PORT, token: str = TOKEN):
        self.port = port
        self.bot = CQHttp(
            use_ws_reverse=True,
            import_name="astrbot-peer",
            api_timeout_sec=15,
            access_token=token,
        )
        self.connected = asyncio.Event()
        self.messages: list[dict] = []      # 转换后的「AstrBotMessage」
        self.notices: list[dict] = []
        self.errors: list[str] = []
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._register_handlers()

    # ---------- AstrBot 的 handler 注册方式 ----------
    def _register_handlers(self) -> None:
        bot = self.bot

        @bot.on_websocket_connection
        def _on_conn(_ws) -> None:
            logger.info("AstrBot 侧：aiocqhttp(OneBot v11) 适配器已连接")
            self.connected.set()

        @bot.on_message("group")
        async def _group(event: Event) -> None:
            try:
                self.messages.append(await self._convert_message(event))
            except Exception as e:            # AstrBot 里是 logger.exception 后丢弃
                self.errors.append(f"group: {e!r}")
                logger.error("AstrBot 侧转换群消息失败: %r", e)

        @bot.on_message("private")
        async def _private(event: Event) -> None:
            try:
                self.messages.append(await self._convert_message(event))
            except Exception as e:
                self.errors.append(f"private: {e!r}")
                logger.error("AstrBot 侧转换私聊消息失败: %r", e)

        @bot.on_notice()
        async def _notice(event: Event) -> None:
            try:
                self.notices.append(await self._convert_notice(event))
            except Exception as e:
                self.errors.append(f"notice: {e!r}")
                logger.error("AstrBot 侧转换通知失败: %r", e)

    # ---------- AstrBot 的 convert_message ----------
    async def _convert_message(self, event: Event) -> dict:
        # AstrBot: assert event.sender is not None
        assert event.sender is not None, "sender 缺失"
        # AstrBot: event.sender["user_id"] —— 直接下标
        sender_uid = event.sender["user_id"]
        # AstrBot: 非数组直接丢弃
        assert isinstance(event.message, list), "message 不是数组格式"

        abm: dict = {
            "self_id": str(event.self_id),
            "sender": {
                "user_id": str(sender_uid),
                "nickname": event.sender.get("card") or event.sender.get("nickname", "N/A"),
            },
            "message_id": str(event.message_id),
            "segments": [],
            "message_str": "",
            "group_id": str(event.get("group_id") or ""),
            "type": "GROUP" if event["message_type"] == "group" else "FRIEND",
        }
        routing = {"self_id": event.self_id} if event.self_id else {}

        for seg in event.message:
            t, data = seg["type"], seg["data"]
            abm["segments"].append(dict(seg))
            if t == "text":
                abm["message_str"] += (data.get("text") or "").strip()
            elif t == "at":
                if data["qq"] == "all":
                    continue
                # AstrBot 会为 at 段查群成员信息；查不到就丢掉这个 at
                info = await self.bot.call_action(
                    action="get_group_member_info",
                    group_id=event.get("group_id"),
                    user_id=int(data["qq"]),
                    no_cache=False,
                    **routing,
                )
                nickname = (info or {}).get("card", "")
                if not nickname and info is not None:
                    stranger = await self.bot.call_action(
                        action="get_stranger_info",
                        user_id=int(data["qq"]),
                        no_cache=False,
                        **routing,
                    )
                    nickname = (stranger or {}).get("nick", "") or (stranger or {}).get("nickname", "")
                abm.setdefault("ats", []).append({"qq": data["qq"], "name": nickname})
            elif t == "reply":
                # AstrBot 用 get_msg 取回被引用消息，再 Event.from_payload 重建
                detail = await self.bot.call_action(
                    action="get_msg", message_id=int(data["id"]), **routing
                )
                detail["post_type"] = "message"
                quoted = Event.from_payload(detail)
                assert quoted is not None, "get_msg 返回的数据无法构造 Event"
                abm["quote"] = {
                    "message_id": str(quoted.message_id),
                    "user_id": str(quoted.sender["user_id"]),
                    "message": quoted.message,
                }
        return abm

    async def _convert_notice(self, event: Event) -> dict:
        abm = {
            "self_id": str(event.self_id),
            "notice_type": event.get("notice_type"),
            "user_id": str(event.get("user_id")),
            "group_id": str(event.get("group_id") or ""),
            "poke_target": (
                str(event["target_id"])
                if event.get("sub_type") == "poke" and event.get("target_id")
                else None
            ),
        }
        return abm

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        self._stop = asyncio.Event()

        async def _trigger() -> None:
            assert self._stop is not None
            await self._stop.wait()

        self._task = asyncio.create_task(
            self.bot.run_task(
                host="127.0.0.1", port=self.port, shutdown_trigger=_trigger
            )
        )
        # 等端口真的起来（hypercorn 启动是异步的）
        for _ in range(100):
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
                writer.close()
                return
            except OSError:
                await asyncio.sleep(0.1)
        raise RuntimeError("AstrBot 侧端口没起来")

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    # ---------- 模拟 AstrBot 发消息 ----------
    async def send_group(self, group_id: int, text: str, self_id: int | None = None) -> dict:
        # AstrBot: message 传的是消息段数组，不是 CQ 码
        return await self.bot.send_group_msg(
            group_id=group_id,
            message=[{"type": "text", "data": {"text": text}}],
            **({"self_id": self_id} if self_id else {}),
        )


async def wait_for(pred, timeout: float = 8.0, what: str = ""):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        got = pred()
        if got:
            return got
        await asyncio.sleep(0.05)
    raise AssertionError(f"等待超时: {what}")


async def main() -> int:
    upstream = FakeOneBotServer(port=DOWNSTREAM_PORT)
    await upstream.start()

    peer = AstrBotPeer()
    await peer.start()

    app = ArkBot(
        adapter=OneBot11Adapter(
            url=f"ws://127.0.0.1:{DOWNSTREAM_PORT}", access_token=ACCESS_TOKEN
        )
    )
    app.bot.self_id = SELF_ID
    adapter = app.adapters[0]
    adapter.on_event(app._on_event)
    adapter_task = asyncio.create_task(adapter.run_forever())

    compat = OneBot11Server(
        app,
        host="127.0.0.1",
        port=0,
        access_token=TOKEN,
        reverse_urls=[f"ws://127.0.0.1:{ASTRBOT_PORT}/ws"],
        heartbeat_interval=0,
    )
    app.compat = compat

    try:
        check("ArkBot 连上下游 OneBot 实现", await adapter.wait_connected(5))
        await compat.start()

        try:
            await asyncio.wait_for(peer.connected.wait(), 10)
            check("ArkBot 反向 WS 被 AstrBot 接受（X-Self-ID / Universal 合法）", True)
        except asyncio.TimeoutError:
            check("ArkBot 反向 WS 被 AstrBot 接受（X-Self-ID / Universal 合法）", False,
                  "10s 内没连上")
            return _report()

        # ---------- 1. 下游群消息 → AstrBot ----------
        await upstream.push_group_message(654321, 999, " 在吗", at_bot=True, message_id=2001)
        try:
            abm = (await wait_for(lambda: peer.messages, 8, "AstrBot 收到群消息"))[0]
        except AssertionError as e:
            check("AstrBot 能收到群消息", False, str(e))
            return _report()

        check("AstrBot 能收到群消息", abm["type"] == "GROUP")
        check("self_id 为真实登录号（@ 判定依赖它）", abm["self_id"] == str(SELF_ID),
              f"got {abm['self_id']}")
        check("sender 可被 AstrBot 直接下标取值", abm["sender"]["user_id"] == "999")
        check("message 为数组格式（AstrBot 硬性要求）", bool(abm["segments"]))
        check("群消息文本正确", abm["message_str"] == "在吗", f"got {abm['message_str']!r}")
        ats = abm.get("ats") or []
        check("at 段未被丢弃（get_group_member_info 可用）", bool(ats) and ats[0]["qq"] == str(SELF_ID),
              f"ats={ats}")

        # ---------- 2. AstrBot 发消息 → 下游 ----------
        res = await peer.send_group(654321, "pong", self_id=SELF_ID)
        check("AstrBot send_group_msg 返回 retcode 0", (res or {}).get("retcode", 0) == 0 if isinstance(res, dict) else False,
              f"res={res}")
        call = upstream.find_call("send_group_msg")
        check("下游实现收到 AstrBot 发出的消息", call is not None)
        params = (call or {}).get("params") or {}
        segs = params.get("message") or []
        check("消息段数组原样透传（没有被二次转义）",
              segs and segs[0].get("type") == "text" and segs[0]["data"]["text"] == "pong",
              f"segs={segs}")
        check("AstrBot 注入的 self_id 不会污染下游调用", "self_id" not in params,
              f"params={params}")

        # ---------- 3. 引用消息 → get_msg ----------
        await upstream.push_custom({
            "post_type": "message", "message_type": "group", "sub_type": "normal",
            "message_id": 2002, "group_id": 654321, "user_id": 777,
            "message": [
                {"type": "reply", "data": {"id": "2001"}},
                {"type": "text", "data": {"text": "引用测试"}},
            ],
            "raw_message": "[CQ:reply,id=2001]引用测试", "font": 0,
            "sender": {"user_id": 777, "nickname": "用户777", "card": "", "role": "member"},
        })
        try:
            abm2 = (await wait_for(lambda: [m for m in peer.messages if m["message_id"] == "2002"], 8,
                                   "AstrBot 收到引用消息"))[0]
            quote = abm2.get("quote")
            check("get_msg 能还原被引用消息（AstrBot 可 Event.from_payload）",
                  bool(quote) and quote["message_id"] == "2001", f"quote={quote}")
            check("被引用消息的 sender 完整", bool(quote) and quote["user_id"] == "999")
        except AssertionError as e:
            check("get_msg 能还原被引用消息（AstrBot 可 Event.from_payload）", False, str(e))

        # ---------- 4. 通知事件（戳一戳） ----------
        await upstream.push_custom({
            "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
            "group_id": 654321, "user_id": 999, "target_id": SELF_ID,
        })
        try:
            await wait_for(lambda: peer.notices, 5, "AstrBot 收到 notice")
            check("notice 事件可被 AstrBot 解析（poke target）",
                  peer.notices[0].get("poke_target") == str(SELF_ID), f"{peer.notices[0]}")
        except AssertionError as e:
            check("notice 事件可被 AstrBot 解析（poke target）", False, str(e))

        # ---------- 5. 并发调用不被单条慢请求卡住 ----------
        upstream.slow_send_group = 1.5
        slow = asyncio.create_task(peer.send_group(654321, "slow-1", self_id=SELF_ID))
        await asyncio.sleep(0.2)
        t0 = time.monotonic()
        fast = await peer.send_group(654321, "fast", self_id=SELF_ID)
        elapsed = time.monotonic() - t0
        await slow
        check("连续 API 调用互不阻塞（慢请求不卡后面）", elapsed < 1.2,
              f"第二条耗时 {elapsed:.2f}s")
        check("并发两条都成功", isinstance(fast, dict) and fast.get("retcode", 0) == 0)

        check("AstrBot 侧无转换异常", not peer.errors, f"errors={peer.errors}")
    finally:
        await compat.stop()
        await adapter.stop()
        adapter_task.cancel()
        try:
            await adapter_task
        except asyncio.CancelledError:
            pass
        await peer.stop()
        await upstream.stop()

    return _report()


def _report() -> int:
    print("\n========== AstrBot 兼容测试结果 ==========")
    for name in PASS:
        print(f"  [PASS] {name}")
    for name in FAIL:
        print(f"  [FAIL] {name}")
    print(f"共 {len(PASS)} 通过 / {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
