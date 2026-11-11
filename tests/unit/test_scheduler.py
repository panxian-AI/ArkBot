"""调度器单元测试：cron 字段解析与匹配、任务注册。"""

import pytest

from arkbot.scheduler import Scheduler, _CronField


# ---------- _CronField ----------

@pytest.mark.parametrize("raw,lo,hi,expected", [
    ("*", 0, 59, frozenset(range(0, 60))),          # 全部
    ("*/15", 0, 59, frozenset(range(0, 60, 15))),   # 步进
    ("9-11", 0, 23, frozenset({9, 10, 11})),        # 区间
    ("0,30", 0, 59, frozenset({0, 30})),            # 列表
    ("5", 0, 59, frozenset({5})),                   # 单值
])
def test_cron_field_parse(raw, lo, hi, expected):
    field = _CronField.parse(raw, lo, hi)
    assert field.values == expected


@pytest.mark.parametrize("raw,v,ok", [
    ("*/10", 30, True),
    ("*/10", 31, False),
    ("8-10", 9, True),
    ("8-10", 11, False),
    ("1,3,5", 5, True),
    ("1,3,5", 2, False),
    ("*", 59, True),
])
def test_cron_field_match(raw, v, ok):
    assert _CronField.parse(raw, 0, 59).match(v) is ok


def test_scheduler_registers_tasks():
    s = Scheduler()
    calls = []

    s.every(60, lambda: None, name="tick")
    s.cron(minute="0", hour="8", job=lambda: None, name="morning")
    kinds = {t.name: t.kind for t in s.tasks} if hasattr(s, "tasks") else {}
    # 兼容私有属性
    if not kinds:
        attrs = [a for a in vars(s).values() if isinstance(a, list)]
        for lst in attrs:
            for t in lst:
                if hasattr(t, "kind"):
                    kinds[t.name] = t.kind
    assert kinds.get("tick") == "every"
    assert kinds.get("morning") == "cron"


def test_cron_task_fields_parsed():
    s = Scheduler()
    s.cron(minute="*/5", hour="6-8", job=lambda: None, name="demo")
    task = None
    for attr in vars(s).values():
        if isinstance(attr, list):
            for t in attr:
                if hasattr(t, "name") and t.name == "demo":
                    task = t
    assert task is not None
    assert task.minute.values == frozenset(range(0, 60, 5))
    assert task.hour.values == frozenset({6, 7, 8})
