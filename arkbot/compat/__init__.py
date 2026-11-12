"""OneBot 11 兼容层：让 ArkBot 自己变成一个 OneBot 实现。

对外提供正向 WS / 反向 WS / HTTP API / HTTP 上报四种接法，
上层任何「以 OneBot 为底层」的软件（NoneBot2、Koishi、AstrBot、老脚本）
都能一行不改接入；QQ 侧换通道时它们完全无感。
"""

from .onebot11 import OneBot11Server
from .ws import HTTPRequest, HTTPResponse, WSConnection, WebSocketServer

__all__ = [
    "OneBot11Server",
    "WebSocketServer",
    "WSConnection",
    "HTTPRequest",
    "HTTPResponse",
]
