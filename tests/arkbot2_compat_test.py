"""ArkBot NG 兼容性端到端测试。

验证核心承诺：plugins/ 目录下原有三个插件（echo / groupkit / status）
在 NG 微内核上零改动运行，且 NG 新能力（中间件、依赖注入）同时生效。

运行: python tests/arkbot2_compat_test.py
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(ROOT))
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
for n in ("websockets", "asyncio"):
    logging.getLogger(n).setLevel(logging.WARNING)

from fake_onebot import FakeOneBotServer, SELF_ID

# 注意导入顺序：先拿到真实 arkbot 适配器（NG 启动时会切换 'arkbot' 为影子模块）
from arkbot.adapters.onebot11 import OneBot11Adapter
from arkbot.dispatch import command as rule_command
from arkbot.event import MessageEvent

from arkbot2 import ArkBot2, Matcher
from arkbot2.bot import Bot

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"

PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "✅" if ok else "❌"
    print(f"  {mark} {name}" + (f"  ({detail})" if detail and not ok else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def calls(server: FakeOneBotServer, action: str) -> list[dict]:
    return [c for c in server.received if c.get("action") == action]


def seg_texts(call: dict) -> list[str]:
    return [
        s["data"].get("text", "")
        for s in call["params"]["message"]
        if s["type"] == "text"
    ]


async def main() -> int:
    server = FakeOneBotServer(port=6712, token="test-token")
    await server.start()

    channel = OneBot11Adapter("ws://127.0.0.1:6712", "test-token", reconnect_interval=1.0)
    app = ArkBot2(channels=[channel], plugins_dir=str(PLUGIN_DIR))

    # ---- NG 能力 1：中间件 ----
    seen_types: list[str] = []

    async def audit(event, nxt):
        seen_types.append(event.post_type)
        await nxt(event)

    app.kernel.middleware(audit)

    # ---- NG 能力 2：依赖注入处理器 ----
    di_hits: list[tuple] = []

    async def di_handler(bot: Bot, ev: MessageEvent, state: dict):
        di_hits.append((state.get("args", ""), type(bot).__name__, ev.user_id))
        await ev.reply("DI ok", quote=False)

    app.kernel.add_matcher(Matcher(
        rule=rule_command("di"), handler=di_handler, priority=90, block=True, plugin="ng-test",
    ))

    runner = asyncio.create_task(app.run())
    await asyncio.sleep(1.8)  # 等连接 + lifecycle + 插件加载

    check("原有插件全部加载（echo/groupkit/status）",
          set(app.plugin_loader.plugins) == {"echo", "groupkit", "status"},
          str(list(app.plugin_loader.plugins)))
    check("lifecycle connect → get_login_info 被调用", calls(server, "get_login_info") != [])
    check("bot.self_id 已从元事件获取", app.bot.self_id == SELF_ID, str(app.bot.self_id))

    # ---- 旧插件：echo ----
    await server.push_private_message(111, "/echo 你好世界")
    await asyncio.sleep(0.6)
    echo_calls = calls(server, "send_private_msg")
    hit = next((c for c in echo_calls if "你好世界" in seg_texts(c)), None)
    check("旧插件 /echo → 回复原文", hit is not None)
    check("回复自动带引用段", hit is not None and hit["params"]["message"][0]["type"] == "reply")

    # ---- 旧插件：status（whoami + status 命令） ----
    await server.push_group_message(888, 222, "/whoami")
    await asyncio.sleep(0.6)
    who = next((c for c in calls(server, "send_group_msg")
                if any(s["type"] == "at" and s["data"].get("qq") == "222"
                       for s in c["params"]["message"])), None)
    check("旧插件 /whoami → 带 AT 段回复", who is not None)

    await server.push_private_message(111, "/status")
    await asyncio.sleep(0.6)
    st = next((c for c in calls(server, "send_private_msg")
               if any("ArkBot 运行中" in t for t in seg_texts(c))), None)
    check("旧插件 /status → 运行信息", st is not None)

    # ---- 旧插件：正则 + 关键词 ----
    await server.push_private_message(111, "大家早安")
    await asyncio.sleep(0.6)
    check("旧插件 regex 早安 → 回复", any(
        "早安" in seg_texts(c) for c in calls(server, "send_private_msg")))
    await server.push_private_message(333, "在吗？")
    await asyncio.sleep(0.6)
    check("旧插件 keyword 在吗 → 回复", any(
        "在的，请讲～" in seg_texts(c) for c in calls(server, "send_private_msg")))

    # ---- 旧插件：通知与请求事件 ----
    await server.push_group_increase(888, 444)
    await asyncio.sleep(0.6)
    welcome = next((c for c in calls(server, "send_group_msg")
                    if any("欢迎新同学入群" in t for t in seg_texts(c))), None)
    check("旧插件群欢迎（配置注入生效）", welcome is not None)
    check("欢迎消息先 AT 新人", welcome is not None
          and any(s["type"] == "at" and s["data"].get("qq") == "444"
                  for s in welcome["params"]["message"]))

    await server.push_friend_request(555)
    await asyncio.sleep(0.6)
    req = calls(server, "set_friend_add_request")
    check("旧插件自动通过好友请求（approve=True）",
          bool(req) and req[-1]["params"].get("approve") is True)

    await server.push_custom({
        "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
        "group_id": 0, "user_id": 111, "target_id": SELF_ID,
    })
    await asyncio.sleep(0.6)
    check("旧插件戳一戳回戳（friend_poke）", calls(server, "friend_poke") != [])

    # ---- NG 新能力验证 ----
    await server.push_private_message(111, "/di 依赖注入")
    await asyncio.sleep(0.6)
    check("DI 处理器按注解注入 (Bot/Event/state)",
          bool(di_hits) and di_hits[0][1] == "Bot" and di_hits[0][2] == 111,
          str(di_hits[:1]))
    check("DI 处理器正常回复", any(
        "DI ok" in seg_texts(c) for c in calls(server, "send_private_msg")))
    check("中间件已审计所有事件类型",
          {"message", "notice", "request", "meta_event"}.issubset(set(seen_types)),
          str(sorted(set(seen_types))))
    check("NG 内核结构化分发计数", app.kernel.event_count >= 8, str(app.kernel.event_count))
    check("调度器任务已注册（旧插件 cron/every）",
          len(app.scheduler._tasks) >= 2, str(len(app.scheduler._tasks)))

    # ---- 清理 ----
    await app.shutdown()
    await asyncio.sleep(0.8)
    if not runner.done():
        runner.cancel()
        try:
            await runner
        except asyncio.CancelledError:
            pass
    await server.stop()

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
