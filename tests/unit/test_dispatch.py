"""规则引擎单元测试：命令前缀、正则、关键词、to_me、规则组合。"""

import asyncio

import pytest

from arkbot.dispatch import command, keyword, only_group, regex, startswith, to_me
from arkbot.event import Event


def make_event(text: str, *, group: bool = True, to_me: bool = False,
               at_self: bool = False, self_id: int = 100, role: str = "member") -> Event:
    segments = []
    if at_self:
        segments.append({"type": "at", "data": {"qq": str(self_id)}})
    segments.append({"type": "text", "data": {"text": text}})
    raw = {
        "post_type": "message",
        "message_type": "group" if group else "private",
        "self_id": self_id,
        "user_id": 555,
        "group_id": 888 if group else 0,
        "message_id": 1,
        "message": segments,
        "sender": {"user_id": 555, "nickname": "t", "role": role},
    }
    ev = Event.from_raw(raw)
    if to_me:
        ev.to_me = True
    return ev


def run(rule, ev):
    return asyncio.run(rule(ev))


# ---------- command ----------

@pytest.mark.parametrize("text,expected", [
    ("/echo hi", (True, "hi")),
    ("/echo", (True, "")),
    ("/echola", (False, None)),         # 命令按整词匹配，不误触发前缀相似命令
    ("echo hi", (False, None)),         # 无前缀
    ("/othr x", (False, None)),         # 非目标命令
])
def test_command_group_needs_to_me(text, expected):
    rule = command("echo")
    ok, state = run(rule, make_event(text))
    assert ok == expected[0]
    if expected[0]:
        assert state["cmd"] == "echo"
        assert state["args"] == expected[1]


def test_command_private_no_to_me_needed():
    ok, _ = run(command("echo"), make_event("/echo x", group=False))
    assert ok


def test_command_at_prefix_resolved_to_me():
    ev = make_event("/echo x", at_self=True, self_id=100)
    assert ev.to_me  # at 机器人首段 → to_me 自动判定
    ok, state = run(command("echo"), ev)
    assert ok and state["args"] == "x"


def test_command_custom_prefix():
    ok, _ = run(command("echo", prefixes=("!")), make_event("!echo y"))
    assert ok


# ---------- regex / keyword / startswith ----------

def test_regex_extracts_named_groups():
    ok, state = run(regex(r"天气\s+(?P<city>\S+)"), make_event("天气 北京"))
    assert ok and state["match"]["city"] == "北京"


def test_regex_no_match():
    ok, _ = run(regex(r"\d{3}"), make_event("abc"))
    assert not ok


def test_keyword_any_word():
    rule = keyword("天气", "温度")
    assert run(rule, make_event("查天气"))[0]
    assert run(rule, make_event("温度计"))[0]
    assert not run(rule, make_event("无聊"))[0]


def test_startswith():
    rule = startswith("帮我")
    assert run(rule, make_event("帮我个忙"))[0]
    assert not run(rule, make_event("请帮我"))[0]


# ---------- 上下文过滤与组合 ----------

def test_to_me_rule():
    assert run(to_me(), make_event("hi", to_me=True))[0]
    assert run(to_me(), make_event("hi", group=False))[0]
    assert not run(to_me(), make_event("hi", to_me=False))[0]


def test_only_group():
    assert run(only_group(), make_event("x"))[0]
    assert not run(only_group(), make_event("x", group=False))[0]


def test_rule_composition_and():
    rule = command("ban") & only_group()
    ok, _ = run(rule, make_event("/ban 123"))
    assert ok
    ok, _ = run(rule, make_event("/ban 123", group=False))
    assert not ok


def test_rule_composition_requires_both():
    rule = to_me() & keyword("帮助")
    assert run(rule, make_event("求帮助", to_me=True))[0]
    assert not run(rule, make_event("求帮助", to_me=False))[0]
