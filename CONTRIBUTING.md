# 参与贡献

先读 **[docs/guides/共创指南.md](docs/guides/共创指南.md)** —— 分层契约、代码地图、扩展方式、PR 清单都在里面。
这里只放最短路径。

## 环境

```bash
pip install -e .[dev]      # Python 3.11+；运行时只依赖 websockets
```

## 提交前自测

```bash
pytest tests/unit -q                    # 纯逻辑单测
python tests/e2e/e2e_test.py            # 单通道全链路
python tests/e2e/failover_test.py       # 双通道抗 ban
python tests/e2e/compat_test.py         # 上层 OneBot 软件接入
python tests/e2e/arkbot2_compat_test.py # 旧插件跑在 NG 内核
python tests/e2e/persist_test.py        # 持久化
python tests/e2e/ai_chat_test.py        # AstrBot / DSH 聊天
```

全部测试都用 `tests/fakes/` 里的假服务端，**不需要真实 QQ 号、不连外网**，
所以本地能跑通，CI 就一定能跑通。

## 改动放哪里

| 你想做的事 | 改这里 |
|---|---|
| 加业务功能 | `plugins/`（不要碰核心） |
| 接新的 QQ 接入方式 | `arkbot/adapters/`（实现 `connect / call_api / send_reply`） |
| 让新的 OneBot 上层软件接进来 | `arkbot/compat/`（默认透传，必要时在 `_dispatch()` 特判） |
| 接入新的聊天模型/Agent | `arkbot/integrations/chat.py` |
| 改事件、消息链、规则 | `arkbot/` 核心（务必先看共创指南第 8 节） |

## PR 约定

- 一个 PR 只做一件事；commit message 写清「改了什么 + 为什么」。
- 新增能力必须有测试：纯逻辑进 `tests/unit/`，链路进 `tests/e2e/`。
- 不引入新的运行时依赖；确需引入时先在 `arkbot2/capabilities.py` 登记，并做成软依赖。
- 用户可见行为变化要同步文档（`README.md` 或 `docs/guides/`），配置项同步 `bot.toml.example`。

## 报告问题

请附：运行方式（`arkbot` / `arkbot2`）、`bot.toml` 中去掉密钥后的相关片段、
完整报错日志（`[bot] log_level = "DEBUG"`）、以及是否能稳定复现。
