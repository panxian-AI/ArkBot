"""CLI 入口：python -m arkbot [插件目录]"""

from __future__ import annotations

import sys

from .app import ArkBot


def main() -> None:
    plugins_dir = sys.argv[1] if len(sys.argv) > 1 else "plugins"
    app = ArkBot()
    app.config.get("bot", {})["plugins_dir"] = plugins_dir
    app.run()


if __name__ == "__main__":
    main()
