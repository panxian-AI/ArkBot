"""ArkBot 端到端测试。

流程：启动假 OneBot 服务端 → 启动 ArkBot（含测试插件）→ 推送各类事件
→ 断言框架正确发出了对应的 API 调用 → 优雅退出。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path[:0] = [str(_HERE), str(_HERE.parent), str(ROOT)]

from fakes.fake_onebot import ACCESS_TOKEN, FakeOneBotServer

from arkbot import MessageSegment, Plugin
from arkbot.app import ArkBot
from arkbot.config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("e2e")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool):
    (PASS if cond else FAIL).append(name)
    logger.info("%s: %s", "通过" if cond else "失败", name)


# ---------------- 测试插件 ----------------
class TPlugin(Plugin):
    name = "tplugin"
    default_config = {"greet_word": "你好呀"}

    async def onLoad(self):
        @self.on_command("echo", "复读")
        async def echo(event, state):
            await event.reply(state.get("args", "?"))

        @self.on_command("status")
        async def status(event, state):
            st = await self.bot.get_status()
            await event.reply(
                MessageSegment.text(f"在线: {st['online']} ")
                + MessageSegment.text("适配器: %s" % self.bot.adapter.name)
            )

        @self.on_notice("group_increase")
        async def welcome(event):
            await self.bot.send_group_msg(
                event.group_id,
                MessageSegment.at(event.user_id) + MessageSegment.text(" 欢迎入群"),
            )

        @self.on_request("friend")
        async def friend(event):
            await event.approve()

        @self.on_keyword("在吗", block=False)
        async def here(event, state):
            await event.reply("在的", quote=False)


async def main() -> int:
    server = FakeOneBotServer(port=6701, token=ACCESS_TOKEN)
    await server.start()

    # 用代码方式组装配置 + 注册插件（不依赖 plugins 目录扫描，测试自包含）
    cfg = Config({
        "adapter": {
            "type": "onebot11",
            "url": "ws://127.0.0.1:6701",
            "access_token": ACCESS_TOKEN,
            "reconnect_interval": 0.5,
        },
        "bot": {"log_level": "INFO", "plugins_dir": ""},
        "plugins": {"tplugin": {}},
    })
    app = ArkBot(config=cfg)
    app.plugin_manager.register_class(TPlugin)

    runner = asyncio.create_task(app._main())

    # 等待框架连上（收 lifecycle connect 后 self_id 应被填充）
    for _ in range(60):
        await asyncio.sleep(0.2)
        if app.bot.self_id:
            break
    check("框架成功连接并通过鉴权（收到 lifecycle connect）", app.bot.self_id == 10000)

    await asyncio.sleep(0.5)
    server.received.clear()

    # ---- 1. 私聊命令 ----
    await server.push_private_message(111, "/echo 你好世界")
    await asyncio.sleep(0.8)
    call = server.find_call("send_private_msg")
    ok = (
        call is not None
        and call["params"]["user_id"] == 111
        and any(
            s["type"] == "text" and s["data"]["text"] == "你好世界"
            for s in call["params"]["message"]
        )
    )
    check("私聊 /echo 命令 → 回复原文", ok)

    # 回复默认带引用段
    if call:
        check("回复自动带 reply 消息段", call["params"]["message"][0]["type"] == "reply")

    # ---- 2. 群聊命令 ----
    server.received.clear()
    await server.push_group_message(888, 222, "/echo 群消息", message_id=201)
    await asyncio.sleep(0.8)
    call = server.find_call("send_group_msg")
    ok = (
        call is not None
        and call["params"]["group_id"] == 888
        and any(
            s["type"] == "text" and s["data"]["text"] == "群消息"
            for s in call["params"]["message"]
        )
    )
    check("群聊 /echo 命令 → 群回复", ok)

    # ---- 3. 调 API 的命令 ----
    server.received.clear()
    await server.push_private_message(111, "/status")
    await asyncio.sleep(0.8)
    call = server.find_call("send_private_msg")
    texts = "".join(
        s["data"].get("text", "")
        for s in (call["params"]["message"] if call else [])
    )
    check("/status 调用 get_status 并回复在线状态", call is not None and "True" in texts)
    check("get_status API 被正确调用", server.find_call("get_status") is not None)

    # ---- 4. 群欢迎（通知事件） ----
    server.received.clear()
    await server.push_group_increase(888, 333)
    await asyncio.sleep(0.8)
    call = server.find_call("send_group_msg")
    ok = call is not None and any(
        s["type"] == "at" and s["data"]["qq"] == "333"
        for s in call["params"]["message"]
    )
    check("新成员入群 → @新成员 欢迎", ok)

    # ---- 5. 好友请求自动通过 ----
    server.received.clear()
    await server.push_friend_request(444)
    await asyncio.sleep(0.8)
    call = server.find_call("set_friend_add_request")
    ok = call is not None and call["params"]["approve"] is True
    check("好友请求 → 自动通过", ok)

    # ---- 6. 关键词 ----
    server.received.clear()
    await server.push_private_message(111, "有人在吗？")
    await asyncio.sleep(0.8)
    call = server.find_call("send_private_msg")
    ok = call is not None and any(
        s["type"] == "text" and s["data"]["text"] == "在的"
        for s in call["params"]["message"]
    )
    check("关键词『在吗』→ 回复", ok)

    # ---- 汇总 ----
    print("\n========== 测试结果 ==========")
    for name in PASS:
        print(f"  [PASS] {name}")
    for name in FAIL:
        print(f"  [FAIL] {name}")
    print(f"共 {len(PASS)} 通过 / {len(FAIL)} 失败")

    # 优雅关闭
    app._stopping.set()
    await asyncio.sleep(0.3)
    runner.cancel()
    try:
        await runner
    except asyncio.CancelledError:
        pass
    await server.stop()

    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
