"""示例插件：群管理 + 事件监听 + 定时任务。

演示 on_notice、on_request、管理员规则、schedule_cron/every、插件配置。
"""

from arkbot import MessageSegment, Plugin
from arkbot.dispatch import only_admin, only_group, to_me


class GroupKit(Plugin):
    name = "groupkit"
    # 插件默认配置（可被 bot.toml [plugins.groupkit] 覆盖）
    default_config = {
        "welcome": "欢迎新同学入群！",
        "morning_msg": "早上好！",
    }

    async def onLoad(self):
        # ---- 新人入群欢迎（通知事件） ----
        @self.on_notice("group_increase")
        async def welcome(event):
            if not event.group_id:
                return
            chain = (
                MessageSegment.at(event.user_id)
                + MessageSegment.text(" " + str(self.cfg.get("welcome", "")))
            )
            await self.bot.send_group_msg(event.group_id, chain)

        # ---- 戳一戳会被戳回去 ----
        @self.on_notice("notify")
        async def poke_back(event):
            if event.sub_type == "poke" and event.target_id == self.bot.self_id:
                if event.user_id:
                    await self.bot.friend_poke(event.user_id)

        # ---- 好友请求自动通过（可通过配置关闭） ----
        @self.on_request("friend")
        async def auto_friend(event):
            if self.cfg.get("auto_friend", True):
                await event.approve()

        # ---- 管理员命令：/kick @某人（演示 only_admin + only_group 组合规则） ----
        @self.on_command("kick", rule=only_admin() & only_group(), priority=50)
        async def kick(event, state):
            targets = [
                s.data.get("qq") for s in event.message
                if s.type == "at" and s.data.get("qq") not in (None, "all")
            ]
            if not targets:
                await event.reply("用法：/kick @某人")
                return
            for qq in targets:
                await self.bot.set_group_ban(event.group_id, int(qq), 60)
                await self.bot.set_group_kick(event.group_id, int(qq))
            await event.reply("已处理。")

        # ---- 定时：每天 8:00 问候（演示 schedule_cron） ----
        @self.schedule_cron(minute="0", hour="8")
        async def morning():
            target_group = self.cfg.get("report_group")
            if target_group:
                await self.bot.send_group_msg(int(target_group), str(self.cfg.get("morning_msg", "")))

        # ---- 定时：每小时自检存活（演示 schedule_every） ----
        @self.schedule_every(3600)
        async def health():
            status = await self.bot.get_status()
            print(f"[groupkit] 存活自检 ok: {bool(status)}")
