"""测试用假服务端集合（不依赖真实 QQ、不连外网）。

- `fake_onebot.py`        假 OneBot 11 实现（正向 WS，可推事件、应答 API）
- `fake_official_qq.py`   假 QQ 官方开放平台（WS 网关 + HTTP 主动消息）
- `fake_chat_backends.py` 假 AstrBot / DSH 聊天后端（SSE 与轮询两种形态）

e2e 脚本用 `from fakes.fake_onebot import ...` 引入。
"""

__all__: list[str] = []
