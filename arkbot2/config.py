"""ArkBot NG 配置：bot.toml + 环境变量。

支持两种通道声明方式：
  [adapter]              旧式单通道（兼容 bot.toml 原有写法）
  [[channels]]           新式多通道数组（推荐，可并行任意多条）
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ChannelCfg:
    type: str = "onebot11"
    url: str = "ws://127.0.0.1:6700"
    access_token: str = ""
    reconnect_interval: float = 3.0
    # official_qq 专用
    app_id: str = ""
    app_secret: str = ""
    intents: int | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ChannelCfg":
        env = lambda k, d="": os.environ.get(k, d)  # noqa: E731
        return cls(
            type=str(raw.get("type", "onebot11")),
            url=str(raw.get("url", "ws://127.0.0.1:6700")),
            access_token=str(raw.get("access_token") or env("ARKBOT_TOKEN", "")),
            reconnect_interval=float(raw.get("reconnect_interval", 3.0)),
            app_id=str(raw.get("app_id") or env("ARKBOT_QQ_APP_ID", "")),
            app_secret=str(raw.get("app_secret") or env("ARKBOT_QQ_APP_SECRET", "")),
            intents=raw.get("intents"),
        )


@dataclass
class Ark2Config:
    channels: list[ChannelCfg] = field(default_factory=list)
    plugins_dir: str = "plugins"
    log_level: str = "INFO"
    plugins: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path = "bot.toml") -> "Ark2Config":
        p = Path(path)
        data: dict[str, Any] = {}
        if p.is_file():
            with open(p, "rb") as f:
                data = tomllib.load(f)

        channels: list[ChannelCfg] = []
        for raw in data.get("channels", []):        # 新式 [[channels]]
            channels.append(ChannelCfg.from_dict(raw))
        if not channels and "adapter" in data:      # 旧式 [adapter] 单通道
            channels.append(ChannelCfg.from_dict(data["adapter"]))
        if not channels:
            channels.append(ChannelCfg())           # 默认本地 OneBot

        bot_cfg = data.get("bot", {})
        plugins_raw = data.get("plugins", {})
        return cls(
            channels=channels,
            plugins_dir=str(bot_cfg.get("plugins_dir", "plugins")),
            log_level=str(bot_cfg.get("log_level", "INFO")),
            plugins={str(k): dict(v) if isinstance(v, dict) else {} for k, v in plugins_raw.items()},
        )
