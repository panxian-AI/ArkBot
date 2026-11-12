"""AI 聊天插件：把 DSH「养鱼」和 AstrBot 接进 QQ。

设计目标（也是「共创」的边界）：

* 插件只做编排——会话映射、触发策略、冷却、分段发送；
  真正的智能来自外部后端（AstrBot Agent / DSH 大肥鱼 / 任意 OpenAI 兼容模型）。
* 换后端不改代码：``/ai backend fish`` 一句话切到 DSH，``/ai backend astr`` 切回 AstrBot。
* 会话 ID 是确定性生成的，进程重启后仍能续上原来的上下文（历史存在后端那边）。

命令：

    /ai <内容>            和当前后端聊一句
    /ai backend           查看可用后端与当前选择
    /ai backend <名字>    切换后端（astr / fish / model / 自定义名字）
    /ai on | /ai off      开/关本群「养鱼模式」（被 @ 或唤醒词自动接话）
    /ai reset             开一段新会话（清空上下文）
    /ai who               查看当前会话状态

群聊「养鱼模式」：开启后，被 @ 或消息里含唤醒词（默认「大鱼」「肥鱼」）就会接话，
带冷却时间避免刷屏；关闭后只响应 /ai 命令。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from arkbot import Plugin, Rule
from arkbot.integrations.chat import ChatError, ChatHub, build_hub

logger = logging.getLogger("arkbot.plugin.ai_chat")


class AiChat(Plugin):
    name = "ai_chat"

    default_config: dict[str, Any] = {
        "default_backend": "",
        "wake_words": ["大鱼", "肥鱼"],
        "relay": True,          # 群内默认开启养鱼模式
        "cooldown": 5.0,        # 同一会话两次自动接话的最小间隔（秒）
        "max_chars": 600,       # 单条消息最大长度，超出分段发送
        "backends": {},
    }

    def __init__(self, bot, cfg: dict[str, Any] | None = None):
        super().__init__(bot, cfg)
        self.hub: ChatHub = build_hub(self.cfg)
        self._backend_by_conv: dict[str, str] = {}
        self._relay_on: dict[str, bool] = {}
        self._gen: dict[str, int] = {}
        self._last_reply: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ---------------- 生命周期 ----------------

    async def onLoad(self) -> None:
        names = self.hub.names()
        if not names:
            logger.warning(
                "ai_chat 未配置任何聊天后端；请在 bot.toml 的 "
                "[plugins.ai_chat.backends.*] 里配置 astrbot / dsh"
            )
        else:
            logger.info("ai_chat 就绪，后端: %s（默认 %s）", ", ".join(names), self.hub.default)

        @self.on_command("ai", "聊天")
        async def ai_cmd(event, state):
            args = str(state.get("args") or "").strip()
            conv = self._conv(event)
            if not args:
                await event.reply(self._help_text(conv))
                return
            head, _, rest = args.partition(" ")
            head_l = head.lower()
            if head_l in {"backend", "bg", "后端", "切换"}:
                await self._cmd_backend(event, conv, rest.strip())
            elif head_l in {"on", "off"}:
                await self._cmd_relay(event, conv, head_l == "on")
            elif head_l in {"reset", "清空", "新会话"}:
                self._gen[conv] = self._gen.get(conv, 0) + 1
                await event.reply("好，这段对话翻篇了。")
            elif head_l in {"who", "status", "状态"}:
                await event.reply(self._status_text(event, conv))
            elif head_l in {"help", "?", "帮助"}:
                await event.reply(self._help_text(conv))
            else:
                await self._chat_and_reply(event, conv, args, force=True)

        @self.on_message(rule=Rule(check=self._should_relay), priority=190, block=False)
        async def relay(event, state):
            conv = self._conv(event)
            text = self._clean_text(event)
            if not text:
                return
            await self._chat_and_reply(event, conv, text)

    async def onUnload(self) -> None:
        await self.hub.close()

    # ---------------- 触发判断 ----------------

    def _should_relay(self, event) -> bool:
        if not getattr(event, "is_message", True):
            return False
        text = self._clean_text(event)
        if not text or text.startswith("/"):
            return False
        conv = self._conv(event)
        if not self._relay_enabled(conv):
            return False
        if getattr(event, "to_me", False):
            return True
        words = self.cfg.get("wake_words") or []
        return any(w and w in text for w in words)

    def _relay_enabled(self, conv: str) -> bool:
        if conv in self._relay_on:
            return self._relay_on[conv]
        return bool(self.cfg.get("relay", True))

    def _clean_text(self, event) -> str:
        text = event.message.plain_text().strip()
        # 去掉群聊里 @机器人 留下的前缀（不同实现格式不一，统一剥一层）
        while text.startswith("@"):
            parts = text.split(maxsplit=1)
            if len(parts) < 2:
                return ""
            text = parts[1].strip()
        return text

    # ---------------- 聊天主流程 ----------------

    async def _chat_and_reply(self, event, conv: str, text: str, force: bool = False) -> None:
        backend = self._backend_by_conv.get(conv) or self.hub.default
        if not backend:
            await event.reply(
                "还没配置聊天后端哦。在 bot.toml 里加 [plugins.ai_chat.backends.astr] 或 .fish 就行。"
            )
            return
        cooldown = float(self.cfg.get("cooldown", 5) or 0)
        now = time.monotonic()
        if not force and now - self._last_reply.get(conv, 0.0) < cooldown:
            return  # 冷却中，静默跳过（群里刷屏保护）
        lock = self._locks.setdefault(conv, asyncio.Lock())
        if lock.locked():
            return  # 上一句还在想，别让它同时说两段
        async with lock:
            self._last_reply[conv] = time.monotonic()
            session = self._session_id(event, conv)
            user = str(getattr(event, "user_id", "") or "")
            nickname = getattr(getattr(event, "sender", None), "nickname", "") or ""
            try:
                reply = await self.hub.chat(
                    text, backend=backend, session=session, user=user, nickname=nickname
                )
            except ChatError as e:
                logger.warning("后端 %s 调用失败: %s", backend, e)
                await event.reply(f"（{backend} 没有回应：{e}）")
                return
            except Exception:
                logger.exception("后端 %s 异常", backend)
                await event.reply(f"（{backend} 出了点问题，看下日志？）")
                return
        if not reply.text:
            await event.reply(f"（{backend} 想了一会儿，什么也没说）")
            return
        await self._send_long(event, reply.text)

    async def _send_long(self, event, text: str) -> None:
        limit = max(80, int(self.cfg.get("max_chars", 600) or 600))
        chunks = [text[i : i + limit] for i in range(0, len(text), limit)] or [text]
        for i, chunk in enumerate(chunks):
            if i == 0:
                await event.reply(chunk)
            else:
                await asyncio.sleep(0.3)
                try:
                    await event.reply(chunk, quote=False)
                except TypeError:  # NG 内核或其他实现的 reply 可能没有 quote 参数
                    await event.reply(chunk)

    def _session_id(self, event, conv: str) -> str:
        gen = self._gen.get(conv, 0)
        base = f"arkbot:{conv}"
        return base if not gen else f"{base}:r{gen}"

    @staticmethod
    def _conv(event) -> str:
        if getattr(event, "is_group", False):
            return f"g{event.group_id}"
        return f"u{getattr(event, 'user_id', 0)}"

    # ---------------- 子命令 ----------------

    async def _cmd_backend(self, event, conv: str, name: str) -> None:
        if not name:
            cur = self._backend_by_conv.get(conv) or self.hub.default
            lines = [f"当前后端：{cur or '（未配置）'}", f"可用后端：{', '.join(self.hub.names()) or '无'}"]
            lines.append("切换：/ai backend fish 或 /ai backend astr")
            await event.reply("\n".join(lines))
            return
        resolved = self.hub.resolve(name)
        if not resolved:
            await event.reply(f"没有叫 {name} 的后端。可用：{', '.join(self.hub.names())}")
            return
        self._backend_by_conv[conv] = resolved
        await event.reply(f"这个会话交给「{resolved}」了。")

    async def _cmd_relay(self, event, conv: str, on: bool) -> None:
        self._relay_on[conv] = on
        if on:
            words = "、".join(self.cfg.get("wake_words") or []) or "（未设置）"
            await event.reply(f"养鱼模式已打开：@我 或提到「{words}」我就接话。")
        else:
            await event.reply("养鱼模式已关闭，只响应 /ai 命令。")

    def _status_text(self, event, conv: str) -> str:
        backend = self._backend_by_conv.get(conv) or self.hub.default
        return (
            f"会话：{conv}\n"
            f"后端：{backend or '（未配置）'}\n"
            f"养鱼模式：{'开' if self._relay_enabled(conv) else '关'}\n"
            f"冷却：{self.cfg.get('cooldown', 5)} 秒"
        )

    def _help_text(self, conv: str) -> str:
        backend = self._backend_by_conv.get(conv) or self.hub.default or "未配置"
        return (
            f"当前后端：{backend}\n"
            "/ai <内容> 聊一句\n"
            "/ai backend [名字] 查看/切换后端\n"
            "/ai on | off 开关养鱼模式（被@或唤醒词自动接话）\n"
            "/ai reset 开新会话\n"
            "/ai who 查看状态"
        )
