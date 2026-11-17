"""OneBot 11 兼容层测试（服务端侧）。

验证目标很具体：**别人写的、以 OneBot 为底层的软件**能不能直接接进来。
所以测试里用的是真实的 websockets 客户端 + 真实的 HTTP 请求，
而不是直接调内部函数 —— 协议对不对，得让协议栈说话。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request

import pytest
import websockets
from websockets.exceptions import ConnectionClosedError

from arkbot.adapters.base import ApiError
from arkbot.app import ArkBot
from arkbot.compat import OneBot11Server
from arkbot.compat.onebot11 import _expand_reverse_url
from arkbot.event import Event
from arkbot.message import MessageChain

TOKEN = "compat-token"


class StubAdapter:
    """一个永远健康的下游通道，记录收到的 API 调用。"""

    name = "stub"

    def __init__(self, connected: bool = True) -> None:
        self.is_connected = connected
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, action, params=None):
        params = params or {}
        self.calls.append((action, params))
        if action == "send_group_msg" or action == "send_private_msg":
            return {"message_id": 4242}
        if action == "get_group_list":
            return [{"group_id": 1, "group_name": "测试群"}]
        if action == "delete_msg":
            raise ApiError(action, 1404, "官方通道不支持撤回")
        raise ApiError(action, 1404, "不支持")

    async def send_reply(self, event, chain, quote=True):
        return True


def make_server(connected: bool = True):
    stub = StubAdapter(connected=connected)
    app = ArkBot(adapter=[stub])
    app.bot.self_id = 10000
    server = OneBot11Server(
        app, host="127.0.0.1", port=0, access_token=TOKEN, heartbeat_interval=0
    )
    app.compat = server
    return app, stub, server


async def _recv(ws, timeout=5.0):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def _recv_until(ws, pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        left = deadline - loop.time()
        if left <= 0:
            raise AssertionError("等待目标消息超时")
        data = json.loads(await asyncio.wait_for(ws.recv(), left))
        if pred(data):
            return data


def _http(port: int, path: str, payload: dict | None = None, token: str | None = TOKEN):
    url = f"http://127.0.0.1:{port}{path}"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# ---------------------------------------------------------------------------


def test_ws_client_can_connect_and_call_api():
    """正向 WS：连上就有 lifecycle，随后 echo 配对、data 结构同 OneBot。"""

    async def run():
        app, stub, server = make_server()
        await server.start()
        try:
            url = f"ws://127.0.0.1:{server.port}/"
            async with websockets.connect(
                url, additional_headers={"Authorization": f"Bearer {TOKEN}"}
            ) as ws:
                lifecycle = await _recv(ws)
                assert lifecycle["post_type"] == "meta_event"
                assert lifecycle["meta_event_type"] == "lifecycle"
                assert lifecycle["self_id"] == 10000

                await ws.send(json.dumps(
                    {"action": "get_login_info", "params": {}, "echo": "e1"}
                ))
                resp = await _recv_until(ws, lambda d: d.get("echo") == "e1")
                assert resp["status"] == "ok" and resp["retcode"] == 0
                assert resp["data"]["user_id"] == 10000

                await ws.send(json.dumps({
                    "action": "send_group_msg",
                    "params": {"group_id": 555, "message": "你好"},
                    "echo": "e2",
                }))
                resp = await _recv_until(ws, lambda d: d.get("echo") == "e2")
                assert resp["retcode"] == 0
                assert resp["data"]["message_id"] == 4242
                assert stub.calls[-1][0] == "send_group_msg"
                assert stub.calls[-1][1]["group_id"] == 555
        finally:
            await server.stop()

    asyncio.run(run())


def test_events_are_pushed_in_onebot_shape():
    """下游事件 → 兼容层：字段齐全、message 为数组、self_id 已归一。"""

    async def run():
        app, stub, server = make_server()
        await server.start()
        try:
            url = f"ws://127.0.0.1:{server.port}/event"
            async with websockets.connect(
                url, additional_headers={"Authorization": f"Bearer {TOKEN}"}
            ) as ws:
                await _recv(ws)  # lifecycle
                event = Event.from_raw({
                    "post_type": "message",
                    "message_type": "group",
                    "sub_type": "normal",
                    "message_id": 321,
                    "group_id": 555,
                    "user_id": 999,
                    "self_id": 10000,
                    "time": 1700000000,
                    "message": [{"type": "text", "data": {"text": "在吗"}}],
                    "raw_message": "在吗",
                    "sender": {"user_id": 999, "nickname": "小明", "role": "member"},
                    "_official": {"msg_id": "abc"},   # 内部字段必须被过滤
                })
                await server.push_event(event)
                data = await _recv(ws)
                assert data["post_type"] == "message"
                assert data["group_id"] == 555 and data["user_id"] == 999
                assert data["message"] == [{"type": "text", "data": {"text": "在吗"}}]
                assert data["sender"]["nickname"] == "小明"
                assert "_official" not in data

                # 私聊事件的 message 是 CQ 字符串时，也要转成数组再发出去
                ev2 = Event.from_raw({
                    "post_type": "message",
                    "message_type": "private",
                    "sub_type": "friend",
                    "message_id": 322,
                    "user_id": 999,
                    "self_id": 10000,
                    "message": "[CQ:at,qq=10000] 好",
                    "raw_message": "[CQ:at,qq=10000] 好",
                })
                await server.push_event(ev2)
                data = await _recv(ws)
                assert data["message_type"] == "private"
                assert data["message"][0]["type"] == "at"
        finally:
            await server.stop()

    asyncio.run(run())


def test_http_api_matches_onebot_semantics():
    """HTTP API：POST /send_msg 可用，取不到 token 返回 401，GET / 可探活。"""

    async def run():
        app, stub, server = make_server()
        await server.start()
        try:
            status, body = await asyncio.to_thread(
                _http, server.port, "/send_group_msg",
                {"group_id": 555, "message": [{"type": "text", "data": {"text": "hi"}}]},
            )
            assert status == 200 and body["retcode"] == 0
            assert body["data"]["message_id"] == 4242

            # OneBot 常见的「/send_msg + message_type」写法
            status, body = await asyncio.to_thread(
                _http, server.port, "/send_msg",
                {"message_type": "private", "user_id": 999, "message": "hi"},
            )
            assert status == 200 and body["retcode"] == 0
            assert stub.calls[-1][0] == "send_private_msg"

            # GET 带 query 参数（老 SDK 常用）
            status, body = await asyncio.to_thread(
                _http, server.port, "/get_group_list?access_token=" + TOKEN
            )
            assert status == 200 and body["data"][0]["group_id"] == 1

            # 错误 token
            status, body = await asyncio.to_thread(
                _http, server.port, "/send_group_msg", {"group_id": 1, "message": "x"}, "bad"
            )
            assert status == 401 and body["retcode"] == 401

            status, body = await asyncio.to_thread(_http, server.port, "/")
            assert status == 200 and body["data"]["app_name"] == "ArkBot"
        finally:
            await server.stop()

    asyncio.run(run())


def test_bad_token_ws_is_rejected():
    async def run():
        app, stub, server = make_server()
        await server.start()
        try:
            url = f"ws://127.0.0.1:{server.port}/"
            async with websockets.connect(
                url, additional_headers={"Authorization": "Bearer wrong"}
            ) as ws:
                with pytest.raises(ConnectionClosedError) as exc:
                    await asyncio.wait_for(ws.recv(), 5)
                assert exc.value.rcvd is not None and exc.value.rcvd.code == 1008
        finally:
            await server.stop()

    asyncio.run(run())


def test_unsupported_action_reports_1404_not_crash():
    async def run():
        app, stub, server = make_server()
        await server.start()
        try:
            retcode, data, wording = await server._dispatch("delete_msg", {"message_id": 1})
            assert retcode == 1404 and data is None and "不支持" in wording
        finally:
            await server.stop()

    asyncio.run(run())


def test_send_still_works_when_no_channel_is_alive():
    """核心承诺：QQ 通道全灭时，上层软件调 send 不报错，消息进离线发件箱。"""

    async def run():
        app, stub, server = make_server(connected=False)
        await server.start()
        try:
            status, body = await asyncio.to_thread(
                _http, server.port, "/send_group_msg", {"group_id": 555, "message": "延迟送达"}
            )
            assert status == 200 and body["retcode"] == 0
            assert body["data"]["message_id"] > 0
            assert len(app.bot._outbox) == 1
            assert app.bot._outbox[0]["action"] == "send_group_msg"

            # 查询类动作没有通道可用时，要明确告诉上层「暂时不可用」而不是假成功
            status, body = await asyncio.to_thread(_http, server.port, "/get_group_list")
            assert body["retcode"] == 100
        finally:
            await server.stop()

    asyncio.run(run())


def test_auto_escape_and_cq_message_from_client():
    async def run():
        app, stub, server = make_server()
        await server.start()
        try:
            status, body = await asyncio.to_thread(
                _http, server.port, "/send_group_msg",
                {"group_id": 555, "message": "[CQ:face,id=1]", "auto_escape": True},
            )
            assert body["retcode"] == 0
            sent = stub.calls[-1][1]["message"]
            assert sent == [{"type": "text", "data": {"text": "[CQ:face,id=1]"}}]

            # 关闭 auto_escape 时按 CQ 码解析
            status, body = await asyncio.to_thread(
                _http, server.port, "/send_group_msg",
                {"group_id": 555, "message": "[CQ:face,id=1]"},
            )
            assert stub.calls[-1][1]["message"][0]["type"] == "face"
            assert MessageChain.parse("[CQ:face,id=1]").segments[0].type == "face"
        finally:
            await server.stop()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# AstrBot（aiocqhttp）接入时会同步反问的那些接口 —— 见 docs/guides/AstrBot接入.md
# ---------------------------------------------------------------------------


def test_member_info_degrades_instead_of_failing():
    """下游没有成员信息接口时，也必须给出结构合法的答案。

    AstrBot 处理每条带 @ 的群消息都会问 `get_group_member_info`；
    这里报错会让它把**整条消息**丢掉（群里 @ 机器人彻底没反应）。
    """

    async def run():
        app, stub, server = make_server()
        await server.start()
        try:
            retcode, data, _ = await server._dispatch(
                "get_group_member_info",
                {"group_id": 555, "user_id": 999, "no_cache": False, "self_id": 10000},
            )
            assert retcode == 0, "不能返回错误码，否则上层软件会丢弃整条消息"
            assert data["user_id"] == 999
            # 字段齐全但值未知 —— 上层可以直接下标取值不炸
            for key in ("nickname", "card", "role", "sex", "age"):
                assert key in data
            # 透传给下游的参数里不该带 aiocqhttp 的私有字段
            action, params = stub.calls[-1]
            assert action == "get_group_member_info"
            assert "self_id" not in params and "no_cache" not in params
        finally:
            await server.stop()

    asyncio.run(run())


def test_member_info_uses_what_we_actually_saw():
    """曾经在群里说过话的人，昵称/名片是真实见过的，直接答，不必问下游。"""

    async def run():
        app, stub, server = make_server()
        await server.start()
        try:
            event = Event.from_raw({
                "post_type": "message", "message_type": "group", "sub_type": "normal",
                "message_id": 700, "group_id": 555, "user_id": 999, "self_id": 10000,
                "message": [{"type": "text", "data": {"text": "hi"}}],
                "raw_message": "hi",
                "sender": {"user_id": 999, "nickname": "小明", "card": "小明同学",
                           "role": "admin", "sex": "male", "age": 18},
            })
            await server.push_event(event)
            before = len(stub.calls)

            retcode, data, _ = await server._dispatch(
                "get_group_member_info", {"group_id": 555, "user_id": 999}
            )
            assert retcode == 0 and data["card"] == "小明同学"
            assert data["role"] == "admin"
            assert len(stub.calls) == before, "命中本地缓存就不该再打扰下游"

            # 陌生人也走同一条降级路径，不抛错
            retcode, data, _ = await server._dispatch(
                "get_stranger_info", {"user_id": 10086}
            )
            assert retcode == 0 and data["user_id"] == 10086
        finally:
            await server.stop()

    asyncio.run(run())


def test_get_msg_can_recall_received_message():
    """AstrBot 的引用回复会拿 message_id 回查**收到的**那条消息。"""

    async def run():
        app, stub, server = make_server()
        await server.start()
        try:
            event = Event.from_raw({
                "post_type": "message", "message_type": "group", "sub_type": "normal",
                "message_id": 801, "group_id": 555, "user_id": 999, "self_id": 10000,
                "time": 1700000000,
                "message": [{"type": "text", "data": {"text": "被引用的原话"}}],
                "raw_message": "被引用的原话", "font": 0,
                "sender": {"user_id": 999, "nickname": "小明", "card": "", "role": "member"},
            })
            await server.push_event(event)

            retcode, data, _ = await server._dispatch("get_msg", {"message_id": 801})
            assert retcode == 0
            # aiocqhttp 的 Event.from_payload 只看这两个字段，AstrBot 还要 sender
            assert data["post_type"] == "message" and data["message_type"] == "group"
            assert data["sender"]["user_id"] == 999
            assert data["message"] == [{"type": "text", "data": {"text": "被引用的原话"}}]

            # 自己不认识的 id：空对象而不是报错（AstrBot 会走纯文本兜底）
            retcode, data, _ = await server._dispatch("get_msg", {"message_id": 999999})
            assert retcode == 0
        finally:
            await server.stop()

    asyncio.run(run())


def test_aiocqhttp_housekeeping_actions_are_noops():
    """aiocqhttp 会回灌 `.handle_quick_operation_async`，必须是空操作而不是 1404。"""

    async def run():
        app, stub, server = make_server()
        await server.start()
        try:
            for action in ("handle_quick_operation_async", "handle_quick_operation"):
                retcode, _, _ = await server._dispatch(action, {"context": {}, "operation": {}})
                assert retcode == 0
        finally:
            await server.stop()

    asyncio.run(run())


def test_astrbot_shorthand_expands_to_ws_path():
    """`astrbot://host:port` 自动补成 aiocqhttp 反向 WS 的 `/ws`。"""

    app, stub, server = make_server()
    urls = [_expand_reverse_url(u) for u in (
        "astrbot://127.0.0.1:6199",
        "astrbot://127.0.0.1:6199/",
        "ws://127.0.0.1:6199/ws",
    )]
    assert urls[0] == "ws://127.0.0.1:6199/ws"
    assert urls[1] == "ws://127.0.0.1:6199/ws"
    assert urls[2] == "ws://127.0.0.1:6199/ws"


def test_self_id_is_learned_from_downstream_when_missing():
    """self_id 没配时向下游问一次：AstrBot 的「@ 我了吗」全靠它。"""

    async def run():
        app, stub, server = make_server()
        app.bot.self_id = 0
        server._self_id_override = 0
        stub.calls.clear()

        async def call_api(action, params=None):
            stub.calls.append((action, params or {}))
            if action == "get_login_info":
                return {"user_id": 12345, "nickname": "方舟"}
            raise ApiError(action, 1404, "不支持")

        stub.call_api = call_api
        await server._resolve_self_id()
        assert server._self_id() == 12345
        assert app.bot.self_id == 12345
        assert server.nickname == "方舟"
        await server.stop()

    asyncio.run(run())
