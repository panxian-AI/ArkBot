"""适配器包：每条 QQ 接入通道一个适配器，可插拔、可并行。

第三方协议包（如 ArkLink 的 Milky / Satori / 原生 QQ）通过
`load_adapter` 装配，见 `loader.py`。
"""

from .base import Adapter, ApiError
from .loader import available, load_adapter, register
from .official_qq import OfficialQQAdapter
from .onebot11 import OneBot11Adapter

__all__ = [
    "Adapter",
    "ApiError",
    "OneBot11Adapter",
    "OfficialQQAdapter",
    "load_adapter",
    "register",
    "available",
]
