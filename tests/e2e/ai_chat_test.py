"""上层聊天集成端到端测试。

链路：假 OneBot 服务端 ⇄ ArkBot（plugins/ai_chat.py）⇄ 假 AstrBot / 假 DSH。

覆盖：
  1. /ai 命令走默认后端，SSE 分片正确拼接
  2. 会话 ID 确定性生成 → 同一会话续聊、/ai reset 开新会话
  3. /ai backend 切换后端生效且只影响当前会话
  4. 群聊养鱼模式：被 @ 自动接话、冷却抑制、/ai off 关闭
  5. 长回复按 max_chars 分段发送
"""

from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path[:0] = [str(_HERE), str(_HERE.parent), str(ROOT), str(ROOT / "plugins")]

from ai_chat import AiChat  # noqa: E402  （plugins/ 下的插件模块）
from fakes.fake_chat_backends import FakeAstrBot, FakeDsh
from fakes.fake_onebot import ACCESS_TOKEN, FakeOneBotServer

from arkbot.app import ArkBot
from arkbot.config import Config

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("websockets").setLevel(logging.WARNING)
logger = logging.getLogger("ai-chat-e2e")

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool) -> None:
    (PASS if cond else FAIL).append(name)
    logger.info("%s: %s", "通过" if cond else "失败", name)


def texts(call: dict | None) -> str:
    if not call:
        return ""
    return "".join(
        seg.get("data", {}).get("text", "")
        for seg in call["params"].get("message", [])
        if seg.get("type") == "text"
    )


def replies(server: FakeOneBotServer, action: str) -> list[dict]:
    return [c for c in server.received if c["action"] == action]


async def main() -> int:
    ob = FakeOneBotServer(port=6721, token=ACCESS_TOKEN)
    ab = FakeAstrBot(api_key="abk_test")
    dsh = FakeDsh()
    await ob.start()
    await ab.start()
    await dsh.start()

    tmp = tempfile.mkdtemp(prefix="arkbot-ai-")
    cfg = Config({
        "adapter": {
            "type": "onebot11",
            "url": "ws://127.0.0.1:6721",
            "access_token": ACCESS_TOKEN,
            "reconnect_interval": 0.5,
        },
        "bot": {"log_level": "INFO", "plugins_dir": "", "data_dir": str(Path(tmp) / "data")},
        "plugins": {
            "ai_chat": {
                "default_backend": "astr",
                "wake_words": ["大鱼", "肥鱼"],
                "relay": True,
                "cooldown": 3.0,
                "max_chars": 500,
                "backends": {
                    "astr": {
                        "type": "astrbot",
                        "base_url": f"http://127.0.0.1:{ab.port}",
                        "api_key": "abk_test",
                        "username": "arkbot-test",
                    },
                    "fish": {
                        "type": "dsh",
                        "mode": "aurbot",
                        "base_url": f"http://127.0.0.1:{dsh.port}",
                    },
                },
            }
        },
    })
    app = ArkBot(config=cfg)
    app.plugin_manager.register_class(AiChat)
    runner = asyncio.create_task(app._main())

    for _ in range(60):
        await asyncio.sleep(0.2)
        if app.bot.self_id:
            break
    check("框架连接成功", app.bot.self_id == 10000)
    plugin = app.plugin_manager.plugins.get("ai_chat")
    check("ai_chat 插件已加载且识别出两个后端", bool(plugin) and set(plugin.hub.names()) == {"astr", "fish"})

    await asyncio.sleep(0.4)
    ob.received.clear()

    # ---- 1. 默认后端 + SSE 拼接 ----
    await ob.push_private_message(111, "/ai 你好", message_id=301)
    await asyncio.sleep(1.2)
    req = ab.find("/api/v1/chat")
    check("AstrBot 收到请求", req is not None)
    if req:
        check("请求带 Bearer API Key", req["headers"].get("authorization") == "Bearer abk_test")
        check("请求体 message 正确", req["json"].get("message") == "你好")
        check("会话 ID 以 arkbot:u111 开头", str(req["json"].get("session_id", "")).startswith("arkbot:u111"))
    sent = texts(replies(ob, "send_private_msg")[-1] if replies(ob, "send_private_msg") else None)
    check("SSE 两片回复被正确拼接", sent == "回声：你好")

    first_session = str(req["json"].get("session_id", "")) if req else ""

    # ---- 2. 同会话续聊 ----
    await ob.push_private_message(111, "/ai 第二句", message_id=302)
    await asyncio.sleep(1.0)
    req2 = ab.requests[-1]["json"] if ab.requests else {}
    check("同一会话沿用相同 session_id", req2.get("session_id") == first_session)

    # ---- 3. reset 开新会话 ----
    await ob.push_private_message(111, "/ai reset", message_id=303)
    await asyncio.sleep(0.6)
    await ob.push_private_message(111, "/ai 第三句", message_id=304)
    await asyncio.sleep(1.0)
    req3 = ab.requests[-1]["json"] if ab.requests else {}
    check("/ai reset 后 session_id 变化", req3.get("session_id") != first_session)

    # ---- 4. 切换后端 ----
    await ob.push_private_message(111, "/ai backend fish", message_id=305)
    await asyncio.sleep(0.8)
    sent = texts(replies(ob, "send_private_msg")[-1] if replies(ob, "send_private_msg") else None)
    check("切换后端有确认回复", "fish" in sent)

    dsh.requests.clear()
    await ob.push_private_message(111, "/ai 在吗", message_id=306)
    await asyncio.sleep(1.2)
    dreq = dsh.find("/api/chat")
    check("切到 DSH 后请求打到养鱼接口", dreq is not None)
    if dreq:
        check("DSH 请求带会话与来源标记", dreq["json"].get("source") == "arkbot"
              and str(dreq["json"].get("session_id", "")).startswith("arkbot:u111"))
    sent = texts(replies(ob, "send_private_msg")[-1] if replies(ob, "send_private_msg") else None)
    check("DSH 回复送达 QQ", sent == "大鱼说：在吗")

    # ---- 5. 群聊养鱼模式 ----
    # 先把本群切到养鱼后端（后端选择是「按会话」的，群里也要单独切）
    await ob.push_group_message(888, 222, "/ai backend fish", message_id=400)
    await asyncio.sleep(0.7)
    dsh.requests.clear()
    await ob.push_group_message(888, 222, "大鱼在吗", at_bot=True, message_id=401)
    await asyncio.sleep(1.4)
    check("群里被 @ 且含唤醒词 → 自动接话", dsh.find("/api/chat") is not None)
    group_sent = texts(replies(ob, "send_group_msg")[-1] if replies(ob, "send_group_msg") else None)
    check("群回复内容正确", "大鱼说" in group_sent)

    # 冷却：紧接着再 @ 一次应被抑制
    before = len(dsh.requests)
    await ob.push_group_message(888, 222, "大鱼再说一句", at_bot=True, message_id=402)
    await asyncio.sleep(1.0)
    check("冷却期内不重复调用后端", len(dsh.requests) == before)

    # ---- 6. 关闭养鱼模式 ----
    await ob.push_group_message(888, 222, "/ai off", message_id=403)
    await asyncio.sleep(0.7)
    before = len(dsh.requests)
    await ob.push_group_message(888, 222, "大鱼还在吗", at_bot=True, message_id=404)
    await asyncio.sleep(1.0)
    check("/ai off 后不再自动接话", len(dsh.requests) == before)

    # ---- 7. 长回复分段 ----
    await ob.push_private_message(111, "/ai backend astr", message_id=308)
    await asyncio.sleep(0.6)
    ab.reply = "长" * 1200
    ob.received.clear()
    await ob.push_private_message(111, "/ai 说长点", message_id=307)
    await asyncio.sleep(1.6)
    sends = replies(ob, "send_private_msg")
    check("长回复按 max_chars 分段发送", len(sends) >= 3)
    merged = "".join(texts(c) for c in sends)
    check("分段后内容无损", merged.count("长") == 1200)
    ab.reply = None

    # ---- 收尾 ----
    app._stopping.set()
    await asyncio.sleep(0.4)
    runner.cancel()
    try:
        await runner
    except asyncio.CancelledError:
        pass
    await ob.stop()
    await ab.stop()
    await dsh.stop()

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    for f in FAIL:
        print("  失败:", f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
