"""ArkBot NG 插件加载器。

两种发现方式并存：
  1. 目录扫描：plugins/ 下每个 .py 中的 Plugin 子类（旧机制，保持不变）；
  2. entry_points：pip 包在 pyproject.toml 里声明
     [project.entry-points."arkbot2.plugins"]
     my_plugin = "my_pkg.plugin:MyPlugin"
     即可被自动发现 —— 插件可以作为独立包分发。
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import inspect
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .app import ArkBot2

logger = logging.getLogger("arkbot2.plugin")

ENTRY_POINT_GROUP = "arkbot2.plugins"


class PluginLoader:
    def __init__(self, app: "ArkBot2"):
        self.app = app
        self.plugins: dict[str, Any] = {}

    # ---------- 目录扫描 ----------
    def load_directory(self, path: str | Path) -> list[str]:
        loaded: list[str] = []
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
        module_name = f"arkbot2_plugin_{py.stem}"
        spec = importlib.util.spec_from_file_location(module_name, py)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法载入 {py}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return self._collect_from_module(module, module_name)

    # ---------- entry_points 发现 ----------
    def load_entrypoints(self, group: str = ENTRY_POINT_GROUP) -> list[str]:
        loaded: list[str] = []
        try:
            eps = importlib.metadata.entry_points()
            candidates = eps.select(group=group) if hasattr(eps, "select") else eps.get(group, [])
        except Exception:
            return loaded
        for ep in candidates:
            try:
                obj = ep.load()
            except Exception:
                logger.exception("加载 entry_point 插件失败: %s", ep.name)
                continue
            try:
                if inspect.isclass(obj) and hasattr(obj, "onLoad"):
                    loaded += self._instantiate(obj)
                elif inspect.ismodule(obj):
                    loaded += self._collect_from_module(obj, obj.__name__)
            except Exception:
                logger.exception("实例化 entry_point 插件失败: %s", ep.name)
        return loaded

    # ---------- 内部 ----------
    def _collect_from_module(self, module: Any, module_name: str) -> list[str]:
        loaded: list[str] = []
        plugin_cls = self.app.legacy_plugin_cls
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                inspect.isclass(obj)
                and issubclass(obj, plugin_cls)
                and obj is not plugin_cls
                and obj.__module__ == module_name
            ):
                loaded += self._instantiate(obj)
        return loaded

    def _instantiate(self, cls: type) -> list[str]:
        name = getattr(cls, "name", "") or cls.__name__
        if name in self.plugins:
            return []
        cfg = self.app.config.plugins.get(name, {})
        plugin = cls(self.app.bot, cfg)
        self.plugins[name] = plugin
        asyncio.get_running_loop().create_task(self._safe_on_load(plugin), name=f"ark2-load-{name}")
        logger.info("插件已加载: %s", name)
        return [name]

    async def _safe_on_load(self, plugin: Any) -> None:
        try:
            await plugin.onLoad()
        except Exception:
            logger.exception("onLoad 异常 (plugin=%s)", plugin.name)
