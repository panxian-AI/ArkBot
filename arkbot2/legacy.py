"""旧插件兼容层 —— 「原有插件零改动可用」的核心机制。

原理：旧插件通过 `from arkbot import Plugin, MessageSegment` 和
`from arkbot.dispatch import only_admin` 导入 API。NG 内核启动时：
  1. 先正常 import 真实 arkbot 包（构建通道也需要它）；
  2. 然后把 sys.modules 里的真实 arkbot.* 移除，替换为「影子模块」：
     - 'arkbot'           → 影子包：Plugin = NG 兼容基类，消息/事件类原样透传
     - 'arkbot.dispatch'  → 影子模块：规则函数原样透传（实现完全一致）
  3. 此后加载的插件模块 import 到的是影子 API，注册进 NG 内核而非旧分发器。

已构造的通道对象持有真实类的引用，不受 sys.modules 清理影响——
所以传输层（OneBot11Adapter 等）继续工作，一切照旧。
"""

from __future__ import annotations

import logging
import sys
from types import ModuleType
from typing import Any, Callable

logger = logging.getLogger("arkbot2.legacy")


class LegacyPlugin:
    """与 arkbot.plugin.Plugin API 完全一致的插件基类（注册到 NG 内核）。"""

    name: str = ""
    default_config: dict[str, Any] = {}

    def __init__(self, bot: Any, cfg: dict[str, Any] | None = None):
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

    # ---------- 内部 ----------
    def _kernel(self):
        return self._app.kernel

    def _register(self, rule: Any, fn: Callable[..., Any], priority: int, block: bool) -> None:
        from .kernel import Matcher

        self._kernel().add_matcher(Matcher(
            rule=rule, handler=fn, priority=priority, block=block, plugin=self.name,
        ))

    # ---------- 消息处理器装饰器（与旧版签名一致） ----------
    def on_command(self, *cmds: str, rule: Any = None,
                   priority: int = 100, block: bool = True, **kw: Any) -> Callable:
        from arkbot.dispatch import command

        r = command(*cmds, **kw)
        if rule is not None:
            r = r & rule

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._register(r, fn, priority, block)
            return fn
        return deco

    def on_message(self, rule: Any = None, priority: int = 200,
                   block: bool = False) -> Callable:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._register(rule, fn, priority, block)
            return fn
        return deco

    def on_regex(self, pattern: str, flags: int = 0, rule: Any = None,
                 priority: int = 150, block: bool = False) -> Callable:
        from arkbot.dispatch import regex

        r = regex(pattern, flags)
        if rule is not None:
            r = r & rule

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._register(r, fn, priority, block)
            return fn
        return deco

    def on_keyword(self, *words: str, rule: Any = None,
                   priority: int = 180, block: bool = False) -> Callable:
        from arkbot.dispatch import keyword

        r = keyword(*words)
        if rule is not None:
            r = r & rule

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._register(r, fn, priority, block)
            return fn
        return deco

    def on_startswith(self, *texts: str, rule: Any = None,
                      priority: int = 170, block: bool = False) -> Callable:
        from arkbot.dispatch import startswith

        r = startswith(*texts)
        if rule is not None:
            r = r & rule

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._register(r, fn, priority, block)
            return fn
        return deco

    # ---------- 通知 / 请求 / 元事件 ----------
    def on_notice(self, *notice_types: str) -> Callable:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            from .kernel import Listener

            self._kernel().add_listener(Listener(
                "notice", "notice_type", tuple(notice_types), fn, self.name,
            ))
            return fn
        return deco

    def on_request(self, *request_types: str) -> Callable:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            from .kernel import Listener

            self._kernel().add_listener(Listener(
                "request", "request_type", tuple(request_types), fn, self.name,
            ))
            return fn
        return deco

    def on_meta(self, *meta_types: str) -> Callable:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            from .kernel import Listener

            self._kernel().add_listener(Listener(
                "meta_event", "meta_event_type", tuple(meta_types), fn, self.name,
            ))
            return fn
        return deco

    # ---------- 定时任务 ----------
    def schedule_every(self, seconds: float) -> Callable:
        def deco(fn: Callable[[], Any]) -> Callable[[], Any]:
            self._app.scheduler.every(seconds, fn, self.name)
            return fn
        return deco

    def schedule_cron(self, minute: str = "*", hour: str = "*",
                      day: str = "*", month: str = "*", weekday: str = "*") -> Callable:
        def deco(fn: Callable[[], Any]) -> Callable[[], Any]:
            self._app.scheduler.cron(minute, hour, day, month, weekday, fn, self.name)
            return fn
        return deco


# ---------------- 导入切换 ----------------

_LEGACY_DISPATCH_NAMES = (
    "Rule", "command", "startswith", "keyword", "regex",
    "to_me", "only_private", "only_group", "only_admin", "from_user", "from_group",
)

_LEGACY_TOP_NAMES = (
    "Event", "MessageEvent", "NoticeEvent", "RequestEvent", "MetaEvent", "Sender",
    "MessageChain", "MessageSegment", "Msg",
)


def enable_legacy_import() -> None:
    """把 sys.modules 里的真实 arkbot 换成影子模块（幂等）。"""
    import arkbot                      # noqa: F401  触发真实包完整初始化
    import arkbot.dispatch             # noqa: F401
    import arkbot.event                # noqa: F401
    import arkbot.message              # noqa: F401

    real_top = sys.modules["arkbot"]
    real_event = sys.modules["arkbot.event"]
    real_message = sys.modules["arkbot.message"]
    real_dispatch = sys.modules["arkbot.dispatch"]

    # --- 影子 'arkbot' ---
    shim_top = ModuleType("arkbot")
    shim_top.__doc__ = (
        "影子模块：NG 内核兼容层。真实 arkbot 包已在后台作为传输层运行，"
        "此处 API 面向 NG 内核注册，旧插件代码无需任何改动。"
    )
    for name in _LEGACY_TOP_NAMES:
        setattr(shim_top, name, getattr(real_message if name in
                ("MessageChain", "MessageSegment", "Msg") else real_event, name))
    shim_top.Plugin = LegacyPlugin
    shim_top.Rule = getattr(real_dispatch, "Rule")
    shim_top.__version__ = f"{getattr(real_top, '__version__', '0')}+ng"
    shim_top.__all__ = [*_LEGACY_TOP_NAMES, "Plugin", "Rule"]

    # --- 影子 'arkbot.dispatch'（规则实现原样透传） ---
    shim_dispatch = ModuleType("arkbot.dispatch")
    shim_dispatch.__doc__ = "影子模块：规则函数与真实 arkbot.dispatch 完全一致。"
    for name in _LEGACY_DISPATCH_NAMES:
        setattr(shim_dispatch, name, getattr(real_dispatch, name))
    shim_dispatch.__all__ = list(_LEGACY_DISPATCH_NAMES)

    # --- 清掉真实 'arkbot' 与 'arkbot.dispatch'，替换为影子。
    # 其余真实子模块（arkbot.message / arkbot.event / arkbot.adapters.* 等）
    # 保留在 sys.modules —— 传输层运行时的相对导入（如 base.send_reply 里的
    # from ..message import MessageSegment）必须继续可用。
    import os
    for key in ("arkbot", "arkbot.dispatch"):
        sys.modules.pop(key, None)
    shim_top.__path__ = [os.path.dirname(real_top.__file__)]  # 兜底：未缓存的子模块可按真实目录加载
    sys.modules["arkbot"] = shim_top
    sys.modules["arkbot.dispatch"] = shim_dispatch
    logger.info("旧插件兼容导入已启用：'arkbot' → NG 内核影子模块")
