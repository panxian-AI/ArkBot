"""ArkBot 配置。

优先级：环境变量 ARKBOT_* > bot.toml > 内置默认值。
换适配器 = 改一行 adapter 配置，这就是「抗 ban」的全部操作。
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from .adapters.base import Adapter
from .adapters.onebot11 import OneBot11Adapter
from .adapters.official_qq import OfficialQQAdapter

DEFAULT_CONFIG: dict[str, Any] = {
    "adapter": {
        # "onebot11" | 将来可扩展 "native-qq" 等
        "type": "onebot11",
        "url": "ws://127.0.0.1:6700",
        "access_token": "",
        "reconnect_interval": 3.0,
    },
    "bot": {
        "log_level": "INFO",
        "plugins_dir": "plugins",
    },
    # OneBot 11 兼容层（服务端侧）：让「以 OneBot 为底层」的软件直接接进来。
    # enabled=true 后，ArkBot 自己扮演一个 OneBot 实现：
    #   ws://host:port/    正向 WS（NoneBot2 / Koishi / 自研脚本）
    #   http://host:port/  HTTP API（send_msg 等）
    #   reverse_urls       反向 WS（AstrBot / NoneBot2 反向模式）
    # 上层软件一行不改，QQ 侧换通道它们完全无感。
    "compat": {
        "enabled": False,
        "host": "127.0.0.1",
        "port": 6800,
        "access_token": "",
        "reverse_urls": [],
        "http_report_urls": [],
        "heartbeat_interval": 30.0,
        "self_id": 0,
        "nickname": "ArkBot",
    },
    "plugins": {},   # 每个插件的独立配置段：[plugins.<name>]
}


class Config:
    def __init__(self, data: dict[str, Any] | None = None):
        self._data = {**DEFAULT_CONFIG, **(data or {})}

    # ---------- 加载 ----------
    @classmethod
    def load(cls, path: str | Path | None = None, **overrides: Any) -> "Config":
        data: dict[str, Any] = {}
        cfg_path = Path(path or "bot.toml")
        if cfg_path.is_file():
            with open(cfg_path, "rb") as f:
                data = tomllib.load(f)
        # 环境变量覆盖
        env_url = os.getenv("ARKBOT_URL")
        if env_url:
            data.setdefault("adapter", {})["url"] = env_url
        env_token = os.getenv("ARKBOT_TOKEN")
        if env_token:
            data.setdefault("adapter", {})["access_token"] = env_token
        env_level = os.getenv("ARKBOT_LOG_LEVEL")
        if env_level:
            data.setdefault("bot", {})["log_level"] = env_level
        # 兼容层也支持环境变量（容器里改端口/token 不必改文件）
        env_compat_port = os.getenv("ARKBOT_COMPAT_PORT")
        if env_compat_port:
            data.setdefault("compat", {})["port"] = int(env_compat_port)
        env_compat_token = os.getenv("ARKBOT_COMPAT_TOKEN")
        if env_compat_token:
            data.setdefault("compat", {})["access_token"] = env_compat_token
        # 深度合并默认值
        merged = _deep_merge(DEFAULT_CONFIG, data)
        merged = _deep_merge(merged, overrides)
        return cls(merged)

    # ---------- 访问 ----------
    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    @property
    def adapter(self) -> dict[str, Any]:
        return self._data["adapter"]

    @property
    def plugins(self) -> dict[str, Any]:
        return self._data.get("plugins", {})

    @property
    def compat(self) -> dict[str, Any]:
        return self._data.get("compat", {})

    # ---------- 构造 OneBot 兼容层 ----------
    def build_compat(self, app: Any) -> Any:
        """按 [compat] 段构造兼容层；未启用返回 None。"""
        cfg = self.compat or {}
        if not cfg.get("enabled"):
            return None
        from .compat import OneBot11Server

        return OneBot11Server(
            app,
            host=str(cfg.get("host", "127.0.0.1")),
            port=int(cfg.get("port", 6800)),
            access_token=cfg.get("access_token") or None,
            reverse_urls=cfg.get("reverse_urls") or (),
            http_report_urls=cfg.get("http_report_urls") or (),
            heartbeat_interval=float(cfg.get("heartbeat_interval", 30) or 0),
            self_id=int(cfg.get("self_id", 0) or 0),
            nickname=str(cfg.get("nickname", "ArkBot")),
        )

    # ---------- 构造适配器 ----------
    def build_adapter(self) -> Adapter:
        """兼容单通道旧接口。"""
        return self.build_adapters()[0]

    def build_adapters(self) -> list[Adapter]:
        """按配置构造全部通道（多通道并行 = 抗 ban 核心配置）。

        [[adapters]] 数组表 → 多通道；
        兼容旧版单个 [adapter] 段。
        """
        entries = list(self._data.get("adapters") or [])
        if not entries and "adapter" in self._data:
            entries = [self._data["adapter"]]
        if not entries:
            entries = [DEFAULT_CONFIG["adapter"]]

        adapters: list[Adapter] = []
        for entry in entries:
            adapters.append(_build_one(entry))
        return adapters


def _build_one(entry: dict[str, Any]) -> Adapter:
    atype = entry.get("type", "onebot11")
    if atype == "onebot11":
        return OneBot11Adapter(
            url=entry.get("url", "ws://127.0.0.1:6700"),
            access_token=entry.get("access_token") or None,
            reconnect_interval=float(entry.get("reconnect_interval", 3.0)),
        )
    if atype == "official_qq":
        return OfficialQQAdapter(
            app_id=str(entry.get("app_id", "")),
            app_secret=str(entry.get("app_secret", "")),
            gateway_url=str(entry.get("gateway_url", "wss://api.sgroup.qq.com/websocket")),
            api_base=str(entry.get("api_base", "https://api.sgroup.qq.com")),
            access_token=str(entry.get("access_token", "")),
        )
    raise ValueError(
        f"未知适配器类型 {atype!r}。当前支持: onebot11, official_qq。"
    )


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
