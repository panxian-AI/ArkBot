"""轻量持久化：SQLite KV 存储，供离线发件箱与官方通道 openid 映射落盘。

设计约束：
  - 只用标准库 sqlite3，零第三方依赖（ArkBot 传统）；
  - 值以 JSON 序列化存储，读出自动反序列化；
  - 线程安全（适配器可能在工作线程里调用）；
  - 故障静默降级：磁盘不可用时打警告、退化为纯内存，机器人照常跑。

用法::

    p = Persist("data/arkbot.db")
    p.set("greeting", "你好")           # 任意 JSON 可序列化值
    p.get("greeting")                    # -> "你好"
    p.save_outbox(items) / p.load_outbox()
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("arkbot.persist")

_SCHEMA = "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"


class Persist:
    """SQLite-backed KV 存储。打开失败自动退化为内存模式（get 全返默认值）。"""

    OUTBOX_KEY = "outbox"

    def __init__(self, path: str | Path = "data/arkbot.db"):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self.path), check_same_thread=False)
            self._db.execute(_SCHEMA)
            self._db.commit()
        except Exception as e:
            logger.warning("持久化不可用，退化为内存模式: %s", e)
            self._db = None

    # ---------- KV ----------
    def get(self, key: str, default: Any = None) -> Any:
        if self._db is None:
            return default
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM kv WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except Exception:
            return default

    def set(self, key: str, value: Any) -> None:
        if self._db is None:
            return
        try:
            with self._lock:
                self._db.execute(
                    "INSERT INTO kv(key, value) VALUES(?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)),
                )
                self._db.commit()
        except Exception as e:
            logger.warning("持久化写入失败 %s: %s", key, e)

    def delete(self, key: str) -> None:
        if self._db is None:
            return
        try:
            with self._lock:
                self._db.execute("DELETE FROM kv WHERE key=?", (key,))
                self._db.commit()
        except Exception as e:
            logger.warning("持久化删除失败 %s: %s", key, e)

    def keys(self, prefix: str = "") -> list[str]:
        """返回匹配前缀的全部 key（用于按命名空间扫描）。"""
        if self._db is None:
            return []
        with self._lock:
            rows = self._db.execute(
                "SELECT key FROM kv WHERE key LIKE ? ORDER BY key",
                (prefix.replace("%", r"\%").replace("_", r"\_") + "%",),
            ).fetchall()
        return [r[0] for r in rows]

    # ---------- 发件箱 ----------
    def save_outbox(self, items: list[dict[str, Any]]) -> None:
        self.set(self.OUTBOX_KEY, items)

    def load_outbox(self) -> list[dict[str, Any]]:
        return self.get(self.OUTBOX_KEY, [])

    def clear_outbox(self) -> None:
        self.delete(self.OUTBOX_KEY)

    def close(self) -> None:
        if self._db is not None:
            try:
                with self._lock:
                    self._db.close()
            except Exception:
                pass
            self._db = None
