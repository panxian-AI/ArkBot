"""ArkBot 应用入口：组装多通道适配器、分发器、调度器、插件管理器。

用法::

    from arkbot import ArkBot
    app = ArkBot()                 # 读 bot.toml / 环境变量
    app.run()                      # 阻塞运行，Ctrl+C 优雅退出

多通道：bot.toml 里 [[adapters]] 数组配置任意多条通道（OneBot 逆向 +
官方 API 并行），事件统一分发，回复走来源通道，一条被 ban 其余照常。
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from .adapters.base import Adapter
from .bot import Bot
from .config import Config
from .dispatch import Dispatcher
from .event import Event, MessageEvent
from .plugin import PluginManager
from .scheduler import Scheduler

logger = logging.getLogger("arkbot.app")


class ArkBot:
    def __init__(
        self,
        config: Config | None = None,
        adapter: Adapter | list[Adapter] | None = None,
    ):
        self.config = config or Config.load()
        logging.basicConfig(
            level=getattr(logging, str(self.config.get("bot", {}).get("log_level", "INFO")).upper(), logging.INFO),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
        # 适配器可注入（单条或列表）——测试换桩、未来换直连 QQ 方案都靠这里
        if adapter is None:
            self.adapters: list[Adapter] = self.config.build_adapters()
        elif isinstance(adapter, list):
            self.adapters = adapter
        else:
            self.adapters = [adapter]
        self.bot = Bot(app=self, adapters=self.adapters)
        self.dispatcher = Dispatcher()
        self.scheduler = Scheduler(wait_connected=self._wait_connected)
        self.plugin_manager = PluginManager(self)
        self.loop: asyncio.AbstractEventLoop | None = None
        self._stopping = asyncio.Event()

    # 兼容旧代码
    @property
    def adapter(self) -> Adapter:
        return self.bot.adapter

    # ---------- 事件入口 ----------
    async def _on_event(self, event: Event) -> None:
        event.bot = self.bot
        if isinstance(event, MessageEvent):
            event._resolve_to_me()
        if not self.bot.self_id and event.self_id:
            self.bot.self_id = event.self_id
        await self.dispatcher.dispatch(event)

    async def _wait_connected(self) -> bool:
        """任一通道健康即可。"""
        for a in self.adapters:
            try:
                if await a.wait_connected(timeout=30):
                    return True
            except Exception:
                continue
        return bool(self.bot.healthy())

    # ---------- 启动 ----------
    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except KeyboardInterrupt:
            print("\nArkBot 已退出。")

    async def _main(self) -> None:
        self.loop = asyncio.get_running_loop()
        for a in self.adapters:
            a.on_event(self._on_event)
        logger.info(
            "通道就绪: %s",
            ", ".join(
                f"{a.name}({getattr(a, 'gateway_url', getattr(a, 'url', ''))})"
                for a in self.adapters
            ),
        )

        # 插件加载（已注册类 + 目录扫描）
        self.plugin_manager.load_all()
        pdir = self.config.get("bot", {}).get("plugins_dir", "plugins")
        if isinstance(pdir, str) and pdir:
            loaded = self.plugin_manager.load_directory(pdir)
            logger.info("目录插件加载完成: %s", loaded or "无")

        tasks = [
            asyncio.create_task(a.run_forever(), name=f"ark-adapter-{a.name}")
            for a in self.adapters
        ]
        tasks.append(asyncio.create_task(self._shutdown_watch(), name="ark-shutdown"))
        self.bot.start_outbox()
        await self.scheduler.start()
        try:
            await asyncio.gather(*tasks)
        finally:
            await self._shutdown()

    async def _shutdown_watch(self) -> None:
        """监听 SIGINT/SIGTERM，优雅退出。"""
        loop = asyncio.get_running_loop()
        stop_evt = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_evt.set)
            except (NotImplementedError, RuntimeError):
                # Windows 下 Proactor 不支持 add_signal_handler，由 KeyboardInterrupt 兜底
                pass
        await stop_evt.wait()
        self._stopping.set()

    async def _shutdown(self) -> None:
        logger.info("正在停止……")
        await self.plugin_manager.unload_all()
        await self.scheduler.stop()
        await self.bot.stop_outbox()
        for a in self.adapters:
            await a.stop()
        logger.info("再见。")
