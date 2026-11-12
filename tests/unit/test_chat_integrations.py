"""聊天集成层单测：文本抽取、SSE 分片解析、后端构造与路由。"""

from __future__ import annotations

import pytest

from arkbot.integrations.chat import (
    AstrBotBackend,
    ChatError,
    ChatHub,
    DshBackend,
    build_backend,
    build_hub,
    extract_text,
    _sse_chunk,
)


# ---------------- extract_text ----------------

@pytest.mark.parametrize(
    "payload,expected",
    [
        ("你好", "你好"),
        ({"text": "你好"}, "你好"),
        ({"reply": "大鱼在此"}, "大鱼在此"),
        # AstrBot 组件数组
        ([{"type": "plain", "data": {"text": "第一段"}}], "第一段"),
        # OpenAI chat completions
        ({"choices": [{"message": {"role": "assistant", "content": "模型回答"}}]}, "模型回答"),
        # OpenAI 流式 delta
        ({"choices": [{"delta": {"content": "流式"}}]}, "流式"),
        # AstrBot SSE 事件结构
        ({"type": "message", "data": {"content": {"type": "text", "data": {"text": "组件"}}}}, "组件"),
        # 嵌套兜底
        ({"result": {"payload": {"answer": "兜底"}}}, "兜底"),
        # 类型名不应被当正文
        ({"type": "plain", "status": "ok"}, ""),
        ({}, ""),
        (None, ""),
    ],
)
def test_extract_text(payload, expected):
    assert extract_text(payload) == expected


def test_extract_text_depth_guard():
    deep: dict = {"a": {}}
    node = deep["a"]
    for _ in range(20):
        node["a"] = {}
        node = node["a"]
    assert extract_text(deep) == ""


# ---------------- SSE 分片 ----------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"text":"片段"}', "片段"),
        ('{"choices":[{"delta":{"content":"流"}}]}', "流"),
        ("[DONE]", ""),
        ("", ""),
        ("纯文本片段", "纯文本片段"),
    ],
)
def test_sse_chunk(raw, expected):
    assert _sse_chunk(raw) == expected


# ---------------- 后端构造 ----------------

def test_build_astrbot_backend():
    b = build_backend("astr", {"type": "astrbot", "base_url": "http://127.0.0.1:6185/", "api_key": "abk_x"})
    assert isinstance(b, AstrBotBackend)
    assert b.base_url == "http://127.0.0.1:6185"
    assert b._headers()["Authorization"] == "Bearer abk_x"
    body = b._body("hi", "arkbot:u1", "42")
    assert body["message"] == "hi"
    assert body["username"] == "42"
    assert body["session_id"] == "arkbot:u1"


def test_astrbot_username_defaults():
    b = AstrBotBackend(username="arkbot")
    assert b._body("hi", "", "")["username"] == "arkbot"
    assert "session_id" not in b._body("hi", "", "")


def test_build_dsh_backend_modes():
    a = build_backend("fish", {"type": "dsh", "mode": "aurbot", "base_url": "http://x:1"})
    assert isinstance(a, DshBackend) and a.path == "/api/chat"
    b = build_backend("fish", {"type": "dsh", "mode": "bridge"})
    assert b.mode == "bridge"
    c = build_backend("model", {"type": "openai", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"})
    assert c.mode == "openai" and c.model == "deepseek-chat"


def test_build_backend_unknown_type():
    with pytest.raises(ChatError):
        build_backend("x", {"type": "not-a-backend"})


def test_dsh_body_has_aliases():
    b = DshBackend()
    body = b._base_body("在吗", "arkbot:g1", "7", "小明")
    for key in ("message", "text", "content", "prompt"):
        assert body[key] == "在吗"
    assert body["source"] == "arkbot"
    assert body["session_id"] == "arkbot:g1"
    assert body["nickname"] == "小明"


# ---------------- 路由 ----------------

def test_hub_alias_and_default():
    hub = ChatHub({"astr": AstrBotBackend(), "fish": DshBackend()}, default="astr")
    assert hub.resolve("astrbot") == "astr"
    assert hub.resolve("astr") == "astr"
    assert hub.resolve("dsh") == "fish"
    assert hub.resolve("大鱼") == "fish"
    assert hub.resolve("养鱼") == "fish"
    assert hub.resolve("不存在的后端") == "astr"
    assert hub.names() == ["astr", "fish"]


def test_build_hub_from_config():
    hub = build_hub({
        "default": "fish",
        "backends": {
            "astr": {"type": "astrbot", "base_url": "http://127.0.0.1:6185"},
            "fish": {"type": "dsh", "base_url": "http://127.0.0.1:8770"},
            "bad": {"type": "unknown-kind"},
        },
    })
    assert set(hub.names()) == {"astr", "fish"}   # 坏配置被跳过而不是整体崩溃
    assert hub.default == "fish"


def test_build_hub_empty():
    hub = build_hub({})
    assert hub.names() == [] and hub.default == ""
