"""插件系统。

一个插件 = 一个 Python 模块 + 一个 Plugin 子类。
通过装饰器注册 matcher，框架负责发现与加载。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import inspect
import logging
import pkgutil
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar

from .dispatch import Dispatcher, Matcher, NoticeMatcher, Rule
from .dispatch import (
    command, from_group, from_user, keyword, only_admin, only_group,
    only_private, regex, startswith, to_me,
)
from .event import Event, MessageEvent
from .message import MessageChain, MessageSegment
from .scheduler import Scheduler

logger = logging.getLogger("arkbot.plugin")

Handler = Callable[[MessageEvent, dict[str, Any]], Awaitable[None | bool]]


class Plugin:
    """插件基类。子类实现 onLoad() 注册规则，或用装饰器。"""

    name: ClassVar[str] = ""
    # 插件可读配置的默认值（从 config 中按插件名注入 self.cfg）
    default_config: ClassVar[dict[str, Any]] = {}

    def __init__(self, bot, cfg: dict[str, Any] | None = None):
        self.bot = bot
        self.cfg: dict[str, Any] = {**self.default_config, **(cfg or {})}
        if not self.name:
            self.name = type(self).__name__
        self._app = bot.app

    # ---------- 生命周期 ----------
    async def onLoad(self) -> None:  # noqa: N802
        """插件加载时调用（异步初始化）。"""

    async def onUnload(self) -> None:  # noqa: N802
        """插件卸载时调用。"""

    # ---------- 装饰器：注册消息处理器 ----------
    def on_command(self, *cmds: str, rule: Rule | None = None,
                   priority: int = 100, block: bool = True, **kw: Any) -> Callable:
        """@plugin.on_command('签到')  →  handler(event, {'args': ...})"""
        r = command(*cmds, **kw)
        if rule is not None:
            r = r & rule

        def deco(fn: Handler) -> Handler:
            self._register(r, fn, priority, block)
            return fn
        return deco

    def on_message(self, rule: Rule | None = None, priority: int = 200,
                   block: bool = False) -> Callable:
        def deco(fn: Handler) -> Handler:
            self._register(rule or Rule(), fn, priority, block)
            return fn
        return deco

    def on_regex(self, pattern: str, flags: int = 0, rule: Rule | None = None,
                 priority: int = 150, block: bool = False) -> Callable:
        r = regex(pattern, flags)
        if rule is not None:
            r = r & rule

        def deco(fn: Handler) -> Handler:
            self._register(r, fn, priority, block)
            return fn
        return deco

    def on_keyword(self, *words: str, rule: Rule | None = None,
                   priority: int = 180, block: bool = False) -> Callable:
        r = keyword(*words)
        if rule is not None:
            r = r & rule

        def deco(fn: Handler) -> Handler:
            self._register(r, fn, priority, block)
            return fn
        return deco

    def on_startswith(self, *texts: str, rule: Rule | None = None,
                      priority: int = 170, block: bool = False) -> Callable:
        r = startswith(*texts)
        if rule is not None:
            r = r & rule

        def deco(fn: Handler) -> Handler:
            self._register(r, fn, priority, block)
            return fn
        return deco

    # ---------- 装饰器：通知/请求/元事件 ----------
    def on_notice(self, *notice_types: str) -> Callable:
        def deco(fn: Callable[[Event], Awaitable[None]]) -> Callable:
            self._app.dispatcher.add_listener(
                NoticeMatcher("notice", "notice_type", tuple(notice_types), fn, self.name)
            )
            return fn
        return deco

    def on_request(self, *request_types: str) -> Callable:
        def deco(fn: Callable[[Event], Awaitable[None]]) -> Callable:
            self._app.dispatcher.add_listener(
                NoticeMatcher("request", "request_type", tuple(request_types), fn, self.name)
            )
            return fn
        return deco

    def on_meta(self, *meta_types: str) -> Callable:
        def deco(fn: Callable[[Event], Awaitable[None]]) -> Callable:
            self._app.dispatcher.add_listener(
                NoticeMatcher("meta_event", "meta_event_type", tuple(meta_types), fn, self.name)
            )
            return fn
        return deco

    # ---------- 定时任务 ----------
    def schedule_every(self, seconds: float) -> Callable:
        """@plugin.schedule_every(3600) → 每 3600 秒执行一次。"""
        def deco(fn: Callable[[], Awaitable[None]]) -> Callable:
            self._app.scheduler.every(seconds, fn, self.name)
            return fn
        return deco

    def schedule_cron(self, minute: str = "*", hour: str = "*",
                      day: str = "*", month: str = "*", weekday: str = "*") -> Callable:
        """简化 cron：@plugin.schedule_cron(minute='0', hour='8') → 每天 8 点。"""
        def deco(fn: Callable[[], Awaitable[None]]) -> Callable:
            self._app.scheduler.cron(minute, hour, day, month, weekday, fn, self.name)
            return fn
        return deco

    # ---------- 内部 ----------
    def _register(self, rule: Rule, fn: Handler, priority: int, block: bool) -> None:
        self._app.dispatcher.add_matcher(
            Matcher(rule=rule, handler=fn, priority=priority, block=block, plugin=self.name)
        )


class PluginManager:
    """从目录发现并加载插件。"""

    def __init__(self, app):
        self.app = app
        self.plugins: dict[str, Plugin] = {}
        self._plugin_cls: list[type[Plugin]] = []

        # 插件类注册表（供 import 式插件使用）
        PluginMeta.registry.append(self)

    def register_class(self, cls: type[Plugin]) -> None:
        """注册插件类（代码方式或外部包方式）。"""
        if cls not in self._plugin_cls:
            self._plugin_cls.append(cls)

    def load_directory(self, path: str | Path) -> list[str]:
        """扫描目录下所有 .py 文件，找出 Plugin 子类并实例化加载。"""
        loaded = []
        root = Path(path).resolve()
        if not root.is_dir():
            logger.warning("插件目录不存在: %s", root)
            return loaded
        for py in sorted(root.glob("*.py")):
            if py.name.startswith("_"):
                continue
            try:
                loaded += self.load_file(py)
            except Exception:
                logger.exception("加载插件文件失败: %s", py)
        return loaded

    def load_file(self, py: Path) -> list[str]:
        module_name = f"arkbot_plugin_{py.stem}"
        spec = importlib.util.spec_from_file_location(module_name, py)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法载入 {py}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded = []
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, Plugin) and obj is not Plugin and obj.__module__ == module_name:
                loaded += self._instantiate(obj)
        return loaded

    def load_all(self) -> list[str]:
        """加载通过 register_class 注册的所有插件类。"""
        loaded = []
        for cls in list(self._plugin_cls):
            loaded += self._instantiate(cls)
            self._plugin_cls.remove(cls)
        return loaded

    def _instantiate(self, cls: type[Plugin]) -> list[str]:
        if cls.name in self.plugins:
            return []
        plugin = cls(self.app.bot, self.app.config.plugins.get(cls.name, {}))
        self.plugins[cls.name] = plugin
        self.app.loop.create_task(self._safe_on_load(plugin))
        logger.info("插件已加载: %s", cls.name)
        return [cls.name]

    async def _safe_on_load(self, plugin: Plugin) -> None:
        try:
            await plugin.onLoad()
        except Exception:
            logger.exception("onLoad 异常 (plugin=%s)", plugin.name)

    async def unload_all(self) -> None:
        for plugin in self.plugins.values():
            try:
                await plugin.onUnload()
            except Exception:
                logger.exception("onUnload 异常 (plugin=%s)", plugin.name)


class PluginMeta:
    """全局注册表：允许外部模块 import arkbot.plugin 后直接注册。"""
    registry: list[PluginManager] = []


def plugin(cls: type[Plugin]) -> type[Plugin]:
    """类装饰器：@plugin 注册插件类（等价 register_class）。

    使用方式（在任意被扫描到的模块中）::

        @plugin
        class Echo(Plugin):
            @Plugin.on_command ...   # 或在 onLoad 中注册
    """
    return cls
