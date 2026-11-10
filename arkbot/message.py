"""ArkBot 消息模型。

平台无关的消息链（MessageChain）与消息段（MessageSegment）。
OneBot 11 的数组格式 <-> ArkBot 内部表示 <-> CQ 字符串 三者互转。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator, Union

# CQ 码转义规则（OneBot 11 规范）
_UNESCAPE_MAP = {"&amp;": "&", "&#91;": "[", "&#93;": "]", "&#44;": ",", "&#10;": "\n"}
_ESCAPE_MAP = {"&": "&amp;", "[": "&#91;", "]": "&#93;", ",": "&#44;"}


def escape(s: str) -> str:
    """转义 CQ 码特殊字符（含逗号，用于参数值）。"""
    return "".join(_ESCAPE_MAP.get(c, c) for c in s)


def unescape(s: str) -> str:
    return re.sub(r"&(amp|#91|#93|#44|#10);", lambda m: _UNESCAPE_MAP[m.group(0)], s)


@dataclass
class MessageSegment:
    """一个消息段。type 之外的所有内容都放在 data 里，保持与协议同构。"""

    type: str
    data: dict[str, Any]

    # ---------- 构造器（常用消息段） ----------
    @classmethod
    def text(cls, text: str) -> "MessageSegment":
        return cls("text", {"text": text})

    @classmethod
    def at(cls, qq: Union[int, str]) -> "MessageSegment":
        return cls("at", {"qq": str(qq)})

    @classmethod
    def at_all(cls) -> "MessageSegment":
        return cls("at", {"qq": "all"})

    @classmethod
    def face(cls, face_id: int) -> "MessageSegment":
        return cls("face", {"id": str(face_id)})

    @classmethod
    def image(
        cls,
        file: str,
        cache: bool = True,
        proxy: bool = True,
        timeout: int | None = None,
    ) -> "MessageSegment":
        data = {"file": file, "cache": "1" if cache else "0", "proxy": "1" if proxy else "0"}
        if timeout is not None:
            data["timeout"] = str(timeout)
        return cls("image", data)

    @classmethod
    def record(cls, file: str, magic: bool = False) -> "MessageSegment":
        return cls("record", {"file": file, "magic": "1" if magic else "0"})

    @classmethod
    def video(cls, file: str) -> "MessageSegment":
        return cls("video", {"file": file})

    @classmethod
    def reply(cls, message_id: int) -> "MessageSegment":
        return cls("reply", {"id": str(message_id)})

    @classmethod
    def share(cls, url: str, title: str, content: str = "", image: str = "") -> "MessageSegment":
        return cls("share", {"url": url, "title": title, "content": content, "image": image})

    @classmethod
    def node(cls, user_id: int, nickname: str, content: "str | MessageChain") -> "MessageSegment":
        return cls("node", {
            "user_id": str(user_id),
            "nickname": nickname,
            "content": str(content) if isinstance(content, str) else content.to_onebot(),
        })

    @classmethod
    def json(cls, data: str) -> "MessageSegment":
        return cls("json", {"data": data})

    # ---------- 序列化 ----------
    def to_onebot(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data}

    def to_cq(self) -> str:
        if self.type == "text":
            return _escape_text(self.data.get("text", ""))
        params = ",".join(
            f"{k}={escape(str(v))}" for k, v in self.data.items() if v is not None
        )
        return f"[CQ:{self.type}{',' if params else ''}{params}]"

    def __str__(self) -> str:
        return self.to_cq()

    def __add__(self, other: "MessageChain | MessageSegment | str") -> "MessageChain":
        if isinstance(other, MessageChain):
            return MessageChain(self, *other.segments)
        if isinstance(other, MessageSegment):
            return MessageChain(self, other)
        return MessageChain(self, MessageSegment.text(other))

    def __radd__(self, other: "MessageChain | MessageSegment | str") -> "MessageChain":
        if isinstance(other, MessageChain):
            return MessageChain(*other.segments, self)
        if isinstance(other, MessageSegment):
            return MessageChain(other, self)
        return MessageChain(MessageSegment.text(other), self)

    def __repr__(self) -> str:
        return f"MessageSegment({self.to_cq()!r})"


def _escape_text(s: str) -> str:
    """纯文本只需转义 & [ ] —— 与 OneBot 11 字符串格式一致。"""
    return s.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")


class MessageChain:
    """消息链：可迭代、可拼接、可序列化为 OneBot 数组 / CQ 字符串。"""

    def __init__(self, *segments: MessageSegment):
        self.segments: list[MessageSegment] = list(segments)

    # ---------- 工厂 ----------
    @classmethod
    def parse(cls, raw: Any) -> "MessageChain":
        """从 OneBot 事件原始 message 字段解析（数组或 CQ 字符串）。"""
        if isinstance(raw, cls):
            return raw
        if raw is None:
            return cls()
        if isinstance(raw, str):
            return cls.parse_cq(raw)
        if isinstance(raw, list):
            return cls(*[MessageSegment(s.get("type", "text"), s.get("data", {})) for s in raw])
        return cls()

    @classmethod
    def parse_cq(cls, text: str) -> "MessageChain":
        """解析 CQ 码字符串为消息链。"""
        chain = cls()
        pos = 0
        for m in re.finditer(r"\[CQ:([a-zA-Z0-9_-]+)((?:,[^,\]]*)*)\]", text):
            if m.start() > pos:
                chain.append(MessageSegment.text(unescape(text[pos:m.start()])))
            params: dict[str, str] = {}
            for part in (m.group(2) or "").split(","):
                if "=" in part:
                    k, v = part.split("=", 1)
                    params[k] = unescape(v)
                elif part:
                    params[part] = ""
            chain.append(MessageSegment(m.group(1), params))
            pos = m.end()
        if pos < len(text):
            chain.append(MessageSegment.text(unescape(text[pos:])))
        return chain

    @classmethod
    def template(cls, text: str, **kwargs: Any) -> "MessageChain":
        """支持 {name} 占位符的模板，参数可为 str/MessageSegment/MessageChain。"""
        chain = cls()
        parts = re.split(r"(\{[a-zA-Z_][a-zA-Z0-9_]*\})", text)
        for part in parts:
            if part.startswith("{") and part.endswith("}"):
                val = kwargs.get(part[1:-1], part)
                chain += val
            elif part:
                chain.append(MessageSegment.text(part))
        return chain

    # ---------- 常用操作 ----------
    def plain_text(self) -> str:
        """提取所有文本段（去 CQ 码后用户看到的内容）。"""
        return "".join(
            s.data.get("text", "")
            for s in self.segments
            if s.type == "text"
        )

    def images(self) -> list[MessageSegment]:
        return [s for s in self.segments if s.type == "image"]

    def at_targets(self) -> list[str]:
        return [s.data["qq"] for s in self.segments if s.type == "at"]

    def strip(self) -> str:
        return self.plain_text().strip()

    def first_text(self, strip_prefixes: tuple[str, ...] = ()) -> str:
        """取首段文本，可选择性地剥离前缀（用于命令解析）。"""
        text = self.plain_text().strip()
        for p in strip_prefixes:
            if text.startswith(p):
                text = text[len(p):]
                break
        return text.strip()

    # ---------- 容器协议 ----------
    def append(self, seg: MessageSegment | str) -> "MessageChain":
        self.segments.append(MessageSegment.text(seg) if isinstance(seg, str) else seg)
        return self

    def extend(self, other: "MessageChain | str | MessageSegment") -> "MessageChain":
        self += other
        return self

    def __add__(self, other: "MessageChain | str | MessageSegment") -> "MessageChain":
        new = MessageChain(*self.segments)
        if isinstance(other, MessageChain):
            new.segments.extend(other.segments)
        elif isinstance(other, MessageSegment):
            new.segments.append(other)
        elif isinstance(other, str):
            new.segments.append(MessageSegment.text(other))
        return new

    __iadd__ = __add__

    def __len__(self) -> int:
        return len(self.segments)

    def __iter__(self) -> Iterator[MessageSegment]:
        return iter(self.segments)

    def __getitem__(self, idx: int) -> MessageSegment:
        return self.segments[idx]

    def __bool__(self) -> bool:
        return bool(self.segments)

    # ---------- 序列化 ----------
    def to_onebot(self) -> list[dict[str, Any]]:
        return [s.to_onebot() for s in self.segments]

    def to_cq(self) -> str:
        return "".join(s.to_cq() for s in self.segments)

    def __str__(self) -> str:
        return self.to_cq()

    def __repr__(self) -> str:
        return f"MessageChain({self.to_cq()!r})"


Msg = Union[str, MessageSegment, MessageChain]
