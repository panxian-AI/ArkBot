"""ArkBot —— 抗 ban 的 QQ 机器人框架。

插件只面向统一事件与 API；连接 QQ 的方式（OneBot 实现、未来直连方案）
全部封装在可插拔的适配器层。桥被 ban 就换桥，方舟不沉。
"""

from .adapters.base import Adapter, ApiError
from .app import ArkBot
from .bot import Bot
from .config import Config
from .dispatch import Dispatcher, Rule
from .event import Event, MessageEvent, MetaEvent, NoticeEvent, RequestEvent, Sender
from .message import MessageChain, MessageSegment
from .plugin import Plugin
from .scheduler import Scheduler

__version__ = "0.3.0"
__all__ = [
    "ArkBot", "Bot", "Config", "Adapter", "ApiError",
    "Event", "MessageEvent", "NoticeEvent", "RequestEvent", "MetaEvent", "Sender",
    "MessageChain", "MessageSegment", "Plugin", "Rule", "Dispatcher", "Scheduler",
]
