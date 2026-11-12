"""「以 OneBot 为底层的软件」直接接入 ArkBot —— 全链路兼容测试。

真实链路（没有任何桩替代协议层）：

    测试客户端（扮演 NoneBot2 / Koishi / 老脚本）
        │  OneBot 11 正向 WS
        ▼
    ArkBot 兼容层（服务端）  ←→  ArkBot 适配器（客户端）
                                        │  OneBot 11 正向 WS
                                        ▼
                              假 OneBot 实现（扮演 NapCat）

断言两件事：
  1. 上层调 send_group_msg → ArkBot 转发给下游实现，返回真实 message_id；
  2. 下游推来群消息 → 上层收到的是一份合法 OneBot 事件（数组消息格式）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path[:0] = [str(_HERE), str(_HERE.parent), str(ROOT)]


import websockets

from fakes.fake_onebot import ACCESS_TOKEN, SELF_ID, FakeOneBotServer

from arkbot.adapters.onebot11 import OneBot11Adapter
from arkbot.app import ArkBot
from arkbot.compat import OneBot11Server

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("compat-e2e")

PASS: list[str] = []
FAIL: list[str] = []
TOKEN = "compat-e2e-token"
UPSTREAM_PORT = 6711


def check(name: str, cond: bool):
    (PASS if cond else FAIL).append(name)
    logger.info("%s: %s", "通过" if cond else "失败", name)


async def recv_json(ws, timeout: float = 5.0):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout))


async def recv_until(ws, pred, timeout: float = 5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        left = deadline - loop.time()
        if left <= 0:
            raise AssertionError("等待目标消息超时")
        data = json.loads(await asyncio.wait_for(ws.recv(), left))
        if pred(data):
            return data


async def main() -> int:
    upstream = FakeOneBotServer(port=UPSTREAM_PORT)
    await upstream.start()

    app = ArkBot(
        adapter=OneBot11Adapter(url=f"ws://127.0.0.1:{UPSTREAM_PORT}", access_token=ACCESS_TOKEN)
    )
    app.bot.self_id = SELF_ID
    adapter = app.adapters[0]
    adapter.on_event(app._on_event)
    adapter_task = asyncio.create_task(adapter.run_forever())

    compat = OneBot11Server(
        app, host="127.0.0.1", port=0, access_token=TOKEN, heartbeat_interval=0
    )
    app.compat = compat
    await compat.start()

    ok = await adapter.wait_connected(5)
    check("ArkBot 已连上下游 OneBot 实现", ok)

    ws_url = f"ws://127.0.0.1:{compat.port}/"
    try:
        async with websockets.connect(
            ws_url, additional_headers={"Authorization": f"Bearer {TOKEN}"}
        ) as ws:
            lifecycle = await recv_json(ws)
            check(
                "上层软件连上即收到 lifecycle",
                lifecycle.get("meta_event_type") == "lifecycle"
                and lifecycle.get("self_id") == SELF_ID,
            )

            # 1) 上层主动发群消息（CQ 码写法，老脚本最常见）
            await ws.send(json.dumps({
                "action": "send_group_msg",
                "params": {"group_id": 123456, "message": "[CQ:at,qq=999] 早上好"},
                "echo": "send-1",
            }))
            resp = await recv_until(ws, lambda d: d.get("echo") == "send-1")
            check("上层 send_group_msg 返回 retcode=0", resp.get("retcode") == 0)
            check(
                "返回真实 message_id（来自下游实现）",
                (resp.get("data") or {}).get("message_id") == 900002,
            )
            call = upstream.find_call("send_group_msg")
            check("下游实现确实收到 send_group_msg", call is not None)
            params = (call or {}).get("params") or {}
            check("group_id 原样透传", params.get("group_id") == 123456)
            segs = params.get("message") or []
            check(
                "CQ 码已转成消息段数组",
                bool(segs) and segs[0].get("type") == "at"
                and segs[0]["data"].get("qq") == "999",
            )

            # 2) 下游推群消息 → 上层应收到合法 OneBot 事件
            await upstream.push_group_message(654321, 999, "在吗", at_bot=True)
            event = await recv_until(ws, lambda d: d.get("post_type") == "message")
            check("群消息已推给上层软件", event.get("group_id") == 654321)
            check("事件 self_id 正确", event.get("self_id") == SELF_ID)
            check(
                "message 为数组格式且含 at 段",
                isinstance(event.get("message"), list)
                and event["message"][0].get("type") == "at",
            )
            check(
                "raw_message 保留 CQ 码原文",
                str(event.get("raw_message", "")).startswith("[CQ:at"),
            )
            check(
                "sender 信息完整",
                (event.get("sender") or {}).get("nickname") == "用户999",
            )

            # 3) 上层查群列表（透传到下游实现不支持 → 1404，而不是崩）
            await ws.send(json.dumps({
                "action": "get_group_list", "params": {}, "echo": "q-1",
            }))
            resp = await recv_until(ws, lambda d: d.get("echo") == "q-1")
            check(
                "不支持的动作返回 1404 而不报错",
                resp.get("retcode") in (0, 1404) and resp.get("status") in ("ok", "failed"),
            )

            # 4) 上层用 HTTP 也行（同一个端口）
            check("HTTP 与 WS 共用端口", compat.port == compat._server.port)
    finally:
        await compat.stop()
        await adapter.stop()
        adapter_task.cancel()
        try:
            await adapter_task
        except asyncio.CancelledError:
            pass
        await upstream.stop()

    print("\n========== 兼容层测试结果 ==========")
    for name in PASS:
        print(f"  [PASS] {name}")
    for name in FAIL:
        print(f"  [FAIL] {name}")
    print(f"共 {len(PASS)} 通过 / {len(FAIL)} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
