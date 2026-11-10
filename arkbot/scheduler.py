"""轻量异步调度器：every 间隔任务 + 简化 cron。

cron 字段支持 "*"、"5"、"*/n"、"1,15"、"1-5"（分钟粒度足够机器人场景）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("arkbot.scheduler")

Job = Callable[[], Awaitable[None]]


class _CronField:
    def __init__(self, raw: str, values: frozenset[int]):
        self.raw = raw
        self.values = values

    def match(self, v: int) -> bool:
        return v in self.values

    @classmethod
    def parse(cls, raw: str, lo: int, hi: int) -> "_CronField":
        vals: set[int] = set()
        for part in raw.split(","):
            part = part.strip()
            if part == "*":
                return cls(raw, frozenset(range(lo, hi + 1)))
            if part.startswith("*/"):
                step = max(1, int(part[2:]))
                vals.update(range(lo, hi + 1, step))
            elif "-" in part:
                a, b = part.split("-", 1)
                vals.update(range(int(a), int(b) + 1))
            else:
                vals.add(int(part))
        return cls(raw, frozenset(vals))


@dataclass
class _Task:
    name: str
    job: Job
    kind: str          # every | cron
    interval: float = 0.0
    minute: Any = None
    hour: Any = None
    day: Any = None
    month: Any = None
    weekday: Any = None


class Scheduler:
    def __init__(self, wait_connected: Callable[[], Awaitable[bool]] | None = None):
        self._tasks: list[_Task] = []
        self._running = False
        self._wait_connected = wait_connected

    def every(self, seconds: float, job: Job, name: str = "") -> None:
        self._tasks.append(_Task(name=name or job.__name__, job=job, kind="every", interval=seconds))

    def cron(self, minute: str = "*", hour: str = "*", day: str = "*",
             month: str = "*", weekday: str = "*", job: Job | None = None, name: str = "") -> None:
        if job is None:
            raise ValueError("job 不能为空")
        self._tasks.append(_Task(
            name=name or job.__name__, job=job, kind="cron",
            minute=_CronField.parse(minute, 0, 59),
            hour=_CronField.parse(hour, 0, 23),
            day=_CronField.parse(day, 1, 31),
            month=_CronField.parse(month, 1, 12),
            weekday=_CronField.parse(weekday, 0, 6),
        ))

    async def start(self) -> None:
        self._running = True
        for t in self._tasks:
            asyncio.get_running_loop().create_task(self._run(t), name=f"ark-sched-{t.name}")

    async def stop(self) -> None:
        self._running = False

    async def _run(self, t: _Task) -> None:
        # 先等底层连接就绪（最多等 60s，等不到也照常运行，让任务自行容错）
        if self._wait_connected is not None:
            try:
                await asyncio.wait_for(self._wait_connected(), timeout=60)
            except asyncio.TimeoutError:
                logger.warning("调度任务 %s 启动时连接未就绪，仍继续运行", t.name)

        if t.kind == "every":
            while self._running:
                try:
                    await asyncio.sleep(t.interval)
                    if self._running:
                        await self._safe_run(t)
                except asyncio.CancelledError:
                    return
        else:
            # cron：对齐到下一个分钟边界检查
            last_fired: tuple[int, ...] | None = None
            while self._running:
                try:
                    await asyncio.sleep(_seconds_to_next_minute())
                    now = time.localtime()
                    sig = (now.tm_min, now.tm_hour, now.tm_mday, now.tm_mon, now.tm_wday)
                    if sig == last_fired:
                        continue
                    if (
                        t.minute.match(now.tm_min)
                        and t.hour.match(now.tm_hour)
                        and t.day.match(now.tm_mday)
                        and t.month.match(now.tm_mon)
                        and t.weekday.match(now.tm_wday)
                    ):
                        last_fired = sig
                        await self._safe_run(t)
                except asyncio.CancelledError:
                    return
                except Exception:
                    logger.exception("调度任务 %s 异常", t.name)
                    await asyncio.sleep(5)

    async def _safe_run(self, t: _Task) -> None:
        try:
            await t.job()
        except Exception:
            logger.exception("定时任务 %s 执行失败", t.name)


def _seconds_to_next_minute() -> float:
    now = time.localtime()
    return 60 - now.tm_sec + 0.05
