"""适配器加载器：内置类型 + 第三方包（entry point / 点分路径）。

这是「协议外挂」的装配点。任何独立仓库（如 ArkLink）只要做两件事之一，
就能把新协议挂到本框架上，**内核一行不改**：

1. 声明 entry point::

       [project.entry-points."arkbot.adapters"]
       milky = "arklink.milky:create"

   `bot.toml` 里写 `type = "milky"` 即可。

2. 或者直接写点分路径::

       [[adapters]]
       type = "arklink.milky:MilkyAdapter"
       base_url = "http://127.0.0.1:3000"

约定：工厂/类接受 **kwargs（就是该 entry 除 `type` 外的所有字段），
返回一个满足 Adapter 契约的对象（鸭子类型即可，不强制继承）。
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Callable

from .base import Adapter

logger = logging.getLogger("arkbot.adapter.loader")

#: 类型名 → 工厂
BUILTIN: dict[str, Callable[..., Adapter]] = {}


def register(name: str, factory: Callable[..., Adapter]) -> None:
    """注册一个内置/自建适配器类型。"""
    BUILTIN[name] = factory


#: 内置类型允许的字段。第三方工厂若用 **kwargs，自己决定是否严格校验。
_ONEBOT11_KEYS = {"url", "access_token", "reconnect_interval"}
_OFFICIAL_QQ_KEYS = {"app_id", "app_secret", "gateway_url", "api_base", "access_token"}


def _reject_unknown(kind: str, cfg: dict[str, Any], allowed: set[str]) -> None:
    extra = sorted(set(cfg) - allowed)
    if extra:
        raise TypeError(
            f"适配器 {kind!r} 不支持字段 {extra}，可用字段: {sorted(allowed)}"
        )


def _onebot11(**cfg: Any) -> Adapter:
    from .onebot11 import OneBot11Adapter

    _reject_unknown("onebot11", cfg, _ONEBOT11_KEYS)
    return OneBot11Adapter(
        url=cfg.get("url", "ws://127.0.0.1:6700"),
        access_token=cfg.get("access_token") or None,
        reconnect_interval=float(cfg.get("reconnect_interval", 3.0)),
    )


def _official_qq(**cfg: Any) -> Adapter:
    from .official_qq import OfficialQQAdapter

    _reject_unknown("official_qq", cfg, _OFFICIAL_QQ_KEYS)
    return OfficialQQAdapter(
        app_id=str(cfg.get("app_id", "")),
        app_secret=str(cfg.get("app_secret", "")),
        gateway_url=str(cfg.get("gateway_url", "wss://api.sgroup.qq.com/websocket")),
        api_base=str(cfg.get("api_base", "https://api.sgroup.qq.com")),
        access_token=str(cfg.get("access_token", "")),
    )


register("onebot11", _onebot11)
register("official_qq", _official_qq)


def import_object(spec: str) -> Any:
    """`"pkg.mod:Attr"` → 对象。"""
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise ValueError(f"适配器路径格式应为 'module:Attr'，收到 {spec!r}")
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attr)
    except AttributeError as e:
        raise ImportError(f"{module_name} 里没有 {attr!r}") from e


def _from_entry_points(name: str) -> Callable[..., Adapter] | None:
    from importlib.metadata import entry_points

    try:
        eps = entry_points(group="arkbot.adapters")
    except TypeError:  # pragma: no cover - 老 Python
        eps = entry_points().get("arkbot.adapters", [])  # type: ignore[attr-defined]
    for ep in eps:
        if ep.name == name:
            logger.debug("通过 entry point 加载适配器 %s → %s", name, ep.value)
            return ep.load()
    return None


def load_adapter(entry: dict[str, Any]) -> Adapter:
    """按一条配置构造适配器。"""
    entry = dict(entry or {})
    atype = str(entry.pop("type", "onebot11") or "onebot11")
    cfg = {k: v for k, v in entry.items() if v is not None}

    factory: Callable[..., Adapter] | None = None
    if ":" in atype:
        factory = import_object(atype)
    else:
        factory = BUILTIN.get(atype) or _from_entry_points(atype)

    if factory is None:
        known = ", ".join(sorted(BUILTIN))
        raise ValueError(
            f"未知适配器类型 {atype!r}。内置: {known}；"
            f"第三方包请声明 entry point group 'arkbot.adapters'，"
            f"或直接写点分路径 type = 'pkg.module:Class'。"
        )
    try:
        adapter = factory(**cfg)
    except TypeError as e:
        raise TypeError(
            f"构造适配器 {atype!r} 失败（参数不匹配）：{e}。"
            f"收到的字段: {sorted(cfg)}"
        ) from e
    if not isinstance(adapter, Adapter) and not hasattr(adapter, "call_api"):
        raise TypeError(f"{atype!r} 返回的对象不满足 Adapter 契约（缺少 call_api）")
    return adapter


def available() -> dict[str, str]:
    """已注册的内置类型（不含 entry point 发现结果）。"""
    return {name: getattr(f, "__qualname__", repr(f)) for name, f in BUILTIN.items()}
