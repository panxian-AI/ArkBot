"""ArkBot NG CLI。

python -m arkbot2                  # 按 bot.toml 启动
python -m arkbot2 --capabilities   # 打印 NG 依赖候选清单
"""

from __future__ import annotations

import asyncio
import sys

from .config import Ark2Config


def main() -> int:
    if "--capabilities" in sys.argv:
        from .capabilities import report
        print(report())
        return 0

    from .app import ArkBot2

    cfg_path = "bot.toml"
    if "-c" in sys.argv:
        cfg_path = sys.argv[sys.argv.index("-c") + 1]

    cfg = Ark2Config.load(cfg_path)
    if cfg.log_level.upper() == "DEBUG" or not sys.stdout.isatty():
        pass  # App 内部会统一配置日志

    app = ArkBot2(config_path=cfg_path)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
