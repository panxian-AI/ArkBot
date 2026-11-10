"""ArkBot NG —— 全新微内核（旧插件零改动兼容）。

新能力：
  - asyncio.TaskGroup 结构化并发分发
  - 中间件管道（事件限流 / 审计 / 改写）
  - 依赖注入处理器（按类型注解注入 Bot / Event / state）
  - entry_points 插件发现（pip 包即插件）
  - 旧 arkbot 插件经影子导入零改动接入（见 legacy.py）

运行: python -m arkbot2           按 bot.toml 启动
      python -m arkbot2 --capabilities   查看 NG 依赖候选清单
"""

from .app import ArkBot2
from .bot import Bot, Channel
from .config import Ark2Config
from .kernel import Kernel, Listener, Matcher
from .plugin import PluginLoader, ENTRY_POINT_GROUP
from .scheduler import Scheduler

__version__ = "0.2.0-ng"
__all__ = [
    "ArkBot2", "Bot", "Channel", "Ark2Config",
    "Kernel", "Matcher", "Listener", "Scheduler",
    "PluginLoader", "ENTRY_POINT_GROUP",
]
