"""示例插件：状态与正则匹配。

演示 on_regex、on_keyword、meta 事件（连接生命周期）。
"""

import time

from arkbot import Plugin


class Status(Plugin):
    name = "status"
    _connected_at: float | None = None

    async def onLoad(self):
        @self.on_meta("lifecycle", "heartbeat")
        async def on_meta(event):
            if event.meta_event_type == "lifecycle" and event.sub_type == "connect":
                self._connected_at = time.time()
                info = await self.bot.get_login_info()
                self.bot.self_id = int(info.get("user_id") or 0)

        @self.on_command("status")
        async def status(event, state):
            uptime = (
                f"{(time.time() - self._connected_at):.0f}s"
                if self._connected_at else "未知"
            )
            await event.reply(
                f"ArkBot 运行中\n"
                f"账号：{self.bot.self_id}\n"
                f"已连接时长：{uptime}\n"
                f"适配器：{self.bot.adapter.name}"
            )

        @self.on_regex(r"(早上好|早安|晚上好|晚安)")
        async def greet(event, state):
            m = state.get("match")
            text = m.group(0) if m else ""
            await event.reply(text, quote=False)

        # 关键词不阻止后续匹配（block=False）
        @self.on_keyword("在吗", block=False)
        async def here(event, state):
            await event.reply("在的，请讲～", quote=False)
