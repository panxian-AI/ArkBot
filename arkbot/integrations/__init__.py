"""上层集成：把外部 Agent / 聊天服务接进 ArkBot。

只做「文本进、文本出」的对接，不改变框架核心与适配器层。
"""

from .chat import (
    AstrBotBackend,
    ChatBackend,
    ChatError,
    ChatHub,
    ChatReply,
    DshBackend,
    build_backend,
    build_hub,
    extract_text,
    get_json,
    post_json,
    post_sse,
)

__all__ = [
    "ChatBackend", "ChatHub", "ChatReply", "ChatError",
    "AstrBotBackend", "DshBackend",
    "build_backend", "build_hub",
    "extract_text", "post_json", "get_json", "post_sse",
]
