"""消息链单元测试：CQ 码转义边界、解析、拼接、OneBot 数组互转。"""

import pytest

from arkbot.message import MessageChain, MessageSegment, escape, unescape


# ---------- CQ 转义 ----------

@pytest.mark.parametrize("raw,escaped", [
    ("a&b", "a&amp;b"),
    ("[x]", "&#91;x&#93;"),
    ("a,b", "a&#44;b"),
    ("&[]", "&amp;&#91;&#93;"),
    ("", ""),
])
def test_escape_basic(raw, escaped):
    assert escape(raw) == escaped


@pytest.mark.parametrize("raw", ["a&b", "[x]", "a,b", "&[]", "混合&文[字],串"])
def test_escape_roundtrip(raw):
    assert unescape(escape(raw)) == raw


def test_unescape_unknown_entity_left_alone():
    assert unescape("&#999;") == "&#999;"
    assert unescape("&ampersand;") == "&ampersand;"[:1] + "ampersand;"


# ---------- 文本段 / 构造器 ----------

def test_text_segment_onebot_shape():
    seg = MessageSegment.text("hi")
    assert seg.to_onebot() == {"type": "text", "data": {"text": "hi"}}
    assert seg.to_cq() == "hi"


def test_at_and_image_builders():
    assert MessageSegment.at(123).to_onebot() == {"type": "at", "data": {"qq": "123"}}
    assert MessageSegment.image("a.jpg").to_onebot()["type"] == "image"
    assert MessageSegment.reply(7).to_onebot() == {"type": "reply", "data": {"id": "7"}}


# ---------- CQ 码解析 ----------

def test_parse_cq_mixed_text_and_code():
    chain = MessageChain.parse_cq("你好[CQ:at,qq=100][CQ:image,file=a.jpg]")
    types = [s.type for s in chain.segments]
    assert types == ["text", "at", "image"]
    assert chain.plain_text() == "你好"


def test_parse_cq_escapes_inside_text():
    chain = MessageChain.parse_cq("a&amp;b&#91;c&#93;")
    assert chain.plain_text() == "a&b[c]"


def test_parse_cq_multiple_codes_no_text():
    chain = MessageChain.parse_cq("[CQ:face,id=1][CQ:face,id=2]")
    assert [s.data.get("id") for s in chain.segments] == ["1", "2"]


def test_parse_array_format():
    chain = MessageChain.parse([
        {"type": "text", "data": {"text": "hi "}},
        {"type": "at", "data": {"qq": "9"}},
    ])
    assert chain.plain_text() == "hi "
    assert chain.to_cq().startswith("hi [CQ:at,qq=9]")


def test_parse_plain_string_is_text():
    chain = MessageChain.parse("纯文本")
    assert len(chain.segments) == 1
    assert chain.segments[0].type == "text"


# ---------- 拼接 ----------

def test_concat_segment_chain_str():
    chain = MessageSegment.text("a") + MessageSegment.text("b") + "c"
    assert chain.plain_text() == "abc"


def test_concat_reply_then_text():
    chain = MessageSegment.reply(5) + "hello"
    assert chain.segments[0].type == "reply"
    assert chain.plain_text() == "hello"


def test_to_onebot_roundtrip():
    chain = MessageChain.parse_cq("[CQ:at,qq=1] 前缀 [CQ:face,id=3]")
    arr = chain.to_onebot()
    rebuilt = MessageChain.parse(arr)
    assert rebuilt.plain_text() == chain.plain_text()
    assert [s.type for s in rebuilt.segments] == [s.type for s in chain.segments]
