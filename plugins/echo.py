"""示例插件：基础命令。

演示 on_command、命令参数、消息链（文本+AT）回复。
"""

from arkbot import MessageSegment, Plugin


class Echo(Plugin):
    name = "echo"

    async def onLoad(self):
        @self.on_command("echo", "复读")
        async def echo(event, state):
            args = state.get("args", "")
            if not args:
                await event.reply("用法：/echo <内容>")
            else:
                await event.reply(args)

        @self.on_command("whoami")
        async def whoami(event, state):
            chain = (
                MessageSegment.text("你是 ")
                + MessageSegment.at(event.user_id)
                + MessageSegment.text(f"（{event.sender.nickname}）\n")
                + MessageSegment.text(
                    f"来源：{'群 ' + str(event.group_id) if event.is_group else '私聊'}"
                )
            )
            await event.reply(chain)
