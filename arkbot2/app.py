"""ArkBot NG 应用入口。

启动顺序（顺序即正确性的关键）：
  1. 构建通道 —— import 真实 arkbot.adapters（此时影子切换尚未发生）；
  2. 启用旧插件兼容导入 —— sys.modules 中 arkbot.* 换成影子模块；
  3. 加载插件（目录 + entry_points）—— 旧插件 import 到影子 API，
     注册进 NG 内核；插件代码零改动；
  4. asyncio.TaskGroup 结构化并发运行所有通道与调度器。
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

from .bot import Bot
from .config import Ark2Config
from .kernel import Kernel
from .plugin import PluginLoader
from .scheduler import Scheduler

logger = logging.getLogger("arkbot2.app")


class ArkBot2:
    """NG 内核宿主。channels 可从外部注入（测试用），默认从 bot.toml 构建。"""

    def __init__(
        self,
        config_path: str | Path = "bot.toml",
        channels: list[Any] | None = None,
        plugins_dir: str | Path | None = None,
        persist: Any = None,
    ):
        self.config = Ark2Config.load(config_path)
        self.kernel = Kernel()
        self.channels: list[Any] = list(channels) if channels else []
        # 持久化：从配置构建（生产模式）时默认开启；注入通道（测试）时默认关闭。
        if persist is not None:
            self.persist = persist
        elif not channels:
            from arkbot.persist import Persist

            self.persist = Persist(Path(self.config.data_dir) / "arkbot.db")
        else:
            self.persist = None
        self.bot = Bot(self, self.channels, persist=self.persist)
        # OneBot 兼容层（可选）：NG 内核同样能对外扮演 OneBot 实现，
        # 让现成的 OneBot 上层软件直接接入。必须在 enable_legacy_import 之前
        # 构建 —— 那之后 sys.modules['arkbot'] 会指向影子模块。
        self.compat = self._build_compat()
        self.scheduler = Scheduler(wait_connected=self._any_connected)
        self.plugin_loader = PluginLoader(self)
        self._plugins_dir = Path(plugins_dir) if plugins_dir else Path(self.config.plugins_dir)
        self._stopping = asyncio.Event()

    # ---------- 插件基类（供加载器识别旧/新插件） ----------
    @property
    def legacy_plugin_cls(self) -> type:
        from .legacy import LegacyPlugin
        return LegacyPlugin

    # ---------- 通道 ----------
    def _build_channels(self) -> None:
        """从配置构建通道。必须在 enable_legacy_import() 之前调用。"""
        if self.channels:
            return
        from arkbot.adapters.onebot11 import OneBot11Adapter

        for c in self.config.channels:
            if c.type == "onebot11":
                ch = OneBot11Adapter(
                    url=c.url,
                    access_token=c.access_token or None,
                    reconnect_interval=c.reconnect_interval,
                )
            elif c.type == "official_qq":
                from arkbot.adapters.official_qq import OfficialQQAdapter

                kw: dict[str, Any] = {
                    "app_id": c.app_id,
                    "app_secret": c.app_secret,
                    "reconnect_interval": c.reconnect_interval,
                }
                if c.intents is not None:
                    kw["intents"] = c.intents
                ch = OfficialQQAdapter(**kw)
            else:
                raise ValueError(f"未知通道类型 {c.type!r}（支持 onebot11 / official_qq）")
            self.channels.append(ch)
            logger.info("通道已配置: %s", ch.name)

    # ---------- 事件 ----------
    async def _on_event(self, event: Any) -> None:
        event.bot = self.bot
        if not self.bot.self_id and event.self_id:
            self.bot.self_id = event.self_id
        try:
            await self.kernel.dispatch(event)
        except Exception:
            logger.exception("事件分发异常")
        if self.compat is not None:
            try:
                await self.compat.push_event(event)
            except Exception:
                logger.exception("兼容层事件推送失败（忽略）")

    # ---------- 兼容层 ----------
    def _build_compat(self) -> Any:
        cfg = self.config.compat or {}
        if not cfg.get("enabled"):
            return None
        from arkbot.compat import OneBot11Server

        return OneBot11Server(
            self,
            host=str(cfg.get("host", "127.0.0.1")),
            port=int(cfg.get("port", 6800)),
            access_token=cfg.get("access_token") or None,
            reverse_urls=cfg.get("reverse_urls") or (),
            http_report_urls=cfg.get("http_report_urls") or (),
            heartbeat_interval=float(cfg.get("heartbeat_interval", 30) or 0),
            self_id=int(cfg.get("self_id", 0) or 0),
            nickname=str(cfg.get("nickname", "ArkBot")),
        )

    # ---------- 生命周期 ----------
    async def run(self) -> None:
        self._setup_logging()
        self._build_channels()
        # 通道级持久化挂载（官方通道的 openid 映射表）
        for ch in self.channels:
            if hasattr(ch, "attach_persist"):
                ch.attach_persist(self.persist)
        from .legacy import enable_legacy_import
        enable_legacy_import()

        loaded = self.plugin_loader.load_directory(self._plugins_dir)
        loaded += self.plugin_loader.load_entrypoints()
        logger.info("插件加载完成: %s", loaded or "（无）")

        self.bot.start_outbox()
        if self.compat is not None:
            try:
                await self.compat.start()
            except Exception:
                logger.exception("兼容层启动失败（QQ 通道继续工作）")
        logger.info("ArkBot NG 启动：%d 条通道", len(self.channels))
        async with asyncio.TaskGroup() as tg:
            for ch in self.channels:
                ch.on_event(self._on_event)
                tg.create_task(ch.run_forever(), name=f"ark2-ch-{ch.name}")
            tg.create_task(self.scheduler.start(), name="ark2-scheduler")
            tg.create_task(self._shutdown_watch(), name="ark2-shutdown")

    async def shutdown(self) -> None:
        self._stopping.set()

    async def _shutdown_watch(self) -> None:
        await self._stopping.wait()
        logger.info("正在停止……")
        await self.bot.stop_outbox()
        if self.compat is not None:
            await self.compat.stop()
        for ch in self.channels:
            await ch.stop()
        if self.persist is not None and hasattr(self.persist, "close"):
            self.persist.close()
        logger.info("再见。")

    async def _any_connected(self) -> bool:
        for ch in self.channels:
            try:
                if await asyncio.wait_for(ch.wait_connected(), timeout=1):
                    return True
            except Exception:
                continue
        return False

    def _setup_logging(self) -> None:
        level = getattr(logging, self.config.log_level.upper(), logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
            stream=sys.stdout,
        )
