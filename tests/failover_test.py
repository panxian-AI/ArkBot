"""ArkBot 双通道 failover 端到端测试。

模拟真实抗 ban 场景：
  阶段一：OneBot 通道 + 官方 API 通道并行在线，用户消息走 OneBot → 回复走 OneBot
  阶段二：「QQ 修掉了 OneBot 实现依赖的东西」——OneBot 服务端下线
  阶段三：用户改用官方通道发同样的命令 → 回复走官方通道，体验无感

验收标准：同一个插件代码、同一套命令，两个通道行为一致；
通道死亡不崩溃、不丢事件、回复自动切换到幸存通道。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fake_official_qq import TOKEN as OFFICIAL_TOKEN, FakeOfficialQQServer
from fake_onebot import ACCESS_TOKEN, FakeOneBotServer

from arkbot import MessageSegment, Plugin
from arkbot.app import ArkBot
from arkbot.config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("websockets").setLevel(logging.WARNING)
logger = logging.getLogger("e2e.failover")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool):
    (PASS if cond else FAIL).append(name)
    logger.info("%s: %s", "通过" if cond else "失败", name)


class DualPlugin(Plugin):
    """同一份插件代码服务两条通道 —— 用户体验无感的关键。"""

    name = "dual"

    async def onLoad(self):
        @self.on_command("echo", "复读")
        async def echo(event, state):
            await event.reply(state.get("args", "?"))

        @self.on_keyword("在吗", block=False)
        async def here(event, state):
            await event.reply("在的", quote=False)


async def main() -> int:
    ob = FakeOneBotServer(port=6705, token=ACCESS_TOKEN)
    ff = FakeOfficialQQServer(ws_port=6706, http_port=6707, token=OFFICIAL_TOKEN)
    await ob.start()
    await ff.start()

    cfg = Config({
        "bot": {"log_level": "INFO", "plugins_dir": ""},
        "adapters": [
            {"type": "onebot11", "url": "ws://127.0.0.1:6705", "access_token": ACCESS_TOKEN,
             "reconnect_interval": 0.5},
            {"type": "official_qq", "app_id": "APP123", "access_token": OFFICIAL_TOKEN,
             "gateway_url": "ws://127.0.0.1:6706", "api_base": "http://127.0.0.1:6707"},
        ],
    })
    app = ArkBot(config=cfg)
    app.plugin_manager.register_class(DualPlugin)
    runner = asyncio.create_task(app._main())

    # 等两条通道都连上
    for _ in range(60):
        await asyncio.sleep(0.2)
        if len(app.bot.healthy()) == 2:
            break
    check("双通道并行在线", len(app.bot.healthy()) == 2)

    # ========= 阶段一：OneBot 通道正常服务 =========
    await ob.push_group_message(888, 111, "/echo 你好", message_id=301)
    await asyncio.sleep(0.8)
    call = ob.find_call("send_group_msg")
    ok = (
        call is not None
        and any(s.get("type") == "text" and s["data"]["text"] == "你好" for s in call["params"]["message"])
    )
    check("阶段一：OneBot 通道命令正常回复", ok)
    check("阶段一：回复走了 OneBot 而非官方通道", ff.http_posts == [])

    # ========= 阶段二：模拟 QQ 修掉 OneBot 实现依赖的协议 =========
    await ob.stop()  # OneBot 实现瞬间死亡
    for _ in range(30):
        await asyncio.sleep(0.2)
        if not app.bot.adapters[0].is_connected:
            break
    check("OneBot 通道死亡被框架感知", not app.bot.adapters[0].is_connected)
    check("官方通道仍然健康", app.bot.adapters[1].is_connected)
    await asyncio.sleep(0.5)  # 等重连尝试的告警过去

    # ========= 阶段三：用户从官方通道发同样的命令，体验无感 =========
    ff.http_posts.clear()
    await ff.push_group_at_message("GROUP_OPENID_X", "USER_OPENID_A", "/echo 备份通道还在", event_id=1)
    await asyncio.sleep(1.2)
    post = ff.find_post("/v2/groups/")
    ok = (
        post is not None
        and post["body"].get("content") == "备份通道还在"
        and post["body"].get("msg_id") == "msg-1"   # 官方被动回复必须带来源 msg_id
    )
    check("阶段三：官方通道同一命令正常回复（用户无感）", ok)
    check("阶段三：官方回复带 msg_id（协议语义正确）", post is not None and post["body"].get("msg_id") == "msg-1")

    # 关键词在官方通道同样生效
    ff.http_posts.clear()
    await ff.push_c2c_message("USER_OPENID_B", "有人在吗", event_id=2)
    await asyncio.sleep(1.2)
    post = ff.find_post("/v2/users/")
    ok = post is not None and post["body"].get("content") == "在的"
    check("阶段三：关键词在官方通道同样生效", ok)

    # ========= 阶段五：主动消息在 OneBot 死亡后仍能发出 =========
    # 官方通道从阶段三的事件里学到了群 openid 映射（伪数字 ID = CRC32）
    import zlib as _zlib
    pseudo_group = _zlib.crc32(b"GROUP_OPENID_X") & 0x7FFFFFFF
    ff.http_posts.clear()
    await app.bot.send_group_msg(pseudo_group, MessageSegment.text("官方主动消息"))
    await asyncio.sleep(1.2)
    post = ff.find_post("/v2/groups/")
    ok = (
        post is not None
        and post["body"].get("content") == "官方主动消息"
        and not post["body"].get("msg_id")   # 不带 msg_id = 官方主动消息
    )
    check("阶段五：OneBot 死亡后主动消息改走官方通道", ok)
    check("阶段五：主动消息不带 msg_id（官方主动消息语义）",
          post is not None and not post["body"].get("msg_id"))
    check("阶段五：插件层零改动（仍用数字 group_id 调用）",
          post is not None and post["body"].get("content") == "官方主动消息")

    # 主动发送（如定时任务）在所有通道挂掉时入离线发件箱，不丢不崩
    ff.http_posts.clear()
    ob.received.clear()
    await app.bot.send_group_msg(888, MessageSegment.text("主动消息"))
    await asyncio.sleep(0.3)
    check("通道全灭时主动消息入离线发件箱（不丢失不崩溃）", len(app.bot._outbox) == 1)

    # ========= 阶段四：OneBot 实现恢复上线，发件箱自动补发 =========
    ob2 = FakeOneBotServer(port=6705, token=ACCESS_TOKEN)
    await ob2.start()  # 同端口重新拉起 = NapCat 修复上线
    for _ in range(60):
        await asyncio.sleep(0.2)
        if app.bot.adapters[0].is_connected:
            break
    check("OneBot 实现恢复后框架自动重连", app.bot.adapters[0].is_connected)
    await asyncio.sleep(4.0)  # 等发件箱补发周期
    call = ob2.find_call("send_group_msg")
    ok = (
        call is not None
        and call["params"]["group_id"] == 888
        and any(s["data"]["text"] == "主动消息" for s in call["params"]["message"])
    )
    check("阶段四：发件箱消息在通道恢复后自动补发", ok)
    check("补发后发件箱清空", len(app.bot._outbox) == 0)

    # ========= 汇总 =========
    print("\n========== 双通道 failover 测试结果 ==========")
    for name in PASS:
        print(f"  [PASS] {name}")
    for name in FAIL:
        print(f"  [FAIL] {name}")
    print(f"共 {len(PASS)} 通过 / {len(FAIL)} 失败")

    app._stopping.set()
    await asyncio.sleep(0.3)
    runner.cancel()
    try:
        await runner
    except asyncio.CancelledError:
        pass
    await ff.stop()
    await ob2.stop()
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
