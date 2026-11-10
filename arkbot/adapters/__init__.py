"""适配器包：每条 QQ 接入通道一个适配器，可插拔、可并行。"""

from .base import Adapter, ApiError
from .official_qq import OfficialQQAdapter
from .onebot11 import OneBot11Adapter

__all__ = ["Adapter", "ApiError", "OneBot11Adapter", "OfficialQQAdapter"]
