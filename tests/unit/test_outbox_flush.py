"""发件箱补发回归测试。

背景（CI 抓到的真 bug）：补发失败时调用 `call_api` 会重新入队一份新副本，
于是每轮补发都「成功」地把旧条目换成新条目，队列永远清不空、日志刷屏。
修复方式：补发路径传 `_allow_outbox=False`，失败就留在队列里。
"""

from __future__ import annotations

import asyncio

import pytest

from arkbot.adapters.base import ApiError
from arkbot.bot import Bot


class UnsupportedAdapter:
    """永远返回 1404（通道不支持该动作），模拟官方通道收到 send_group_msg。"""

    name = "fake-unsupported"

    def __init__(self) -> None:
        self.is_connected = True
        self.calls: list[str] = []

    async def call_api(self, action, params):
        self.calls.append(action)
        raise ApiError(action, 1404, "not supported")

    async def send_reply(self, event, chain):  # pragma: no cover - 本测试不用
        return False


def make_bot() -> tuple[Bot, UnsupportedAdapter]:
    adapter = UnsupportedAdapter()
    bot = Bot(app=object(), adapters=[adapter])
    return bot, adapter


def test_send_action_enqueues_when_no_channel_can_handle():
    async def run():
        bot, _ = make_bot()
        result = await bot.call_api("send_group_msg", group_id=1, message=[])
        return result, list(bot._outbox)

    result, outbox = asyncio.run(run())
    assert result is None
    assert len(outbox) == 1 and outbox[0]["action"] == "send_group_msg"


def test_flush_path_does_not_reenqueue():
    """补发失败必须抛错（留在队列），不能悄悄再入队一份。"""

    async def run():
        bot, _ = make_bot()
        await bot.call_api("send_group_msg", group_id=1, message=[])
        before = len(bot._outbox)
        with pytest.raises(ApiError):
            await bot.call_api(
                "send_group_msg", _allow_outbox=False, group_id=1, message=[]
            )
        return before, len(bot._outbox)

    before, after = asyncio.run(run())
    assert before == 1
    assert after == before, "补发失败不应新增队列条目"


def test_flush_loop_keeps_single_pending_item():
    """连跑多轮补发，队列长度必须稳定在 1（修 bug 前的表现是越补越多）。"""

    async def run():
        bot, adapter = make_bot()
        await bot.call_api("send_group_msg", group_id=1, message=[])
        for _ in range(5):
            still_pending = []
            for item in bot._outbox:
                try:
                    await bot.call_api(
                        item["action"], _allow_outbox=False, **item["params"]
                    )
                except Exception:
                    still_pending.append(item)
            bot._outbox = still_pending
        return len(bot._outbox), len(adapter.calls)

    pending, calls = asyncio.run(run())
    assert pending == 1
    assert calls == 6  # 1 次原始调用 + 5 次补发尝试
