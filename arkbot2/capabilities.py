"""ArkBot NG 依赖能力清单（2026-11 调研）。

 ArkBot 当前实际依赖：websockets==17.4 + 标准库。
 本模块把「新内核可以考虑、但当前完全没用到的依赖」整理成可检测的能力表：
   - 外部库：仅软探测（find_spec），不强制安装；缺失时内核走标准库回退
   - 标准库特性：Python 3.13 已提供但项目尚未使用，零安装成本、应优先吃透

 运行: python -m arkbot2 --capabilities
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    name: str           # 依赖名
    layer: str          # 用在哪一层
    value: str          # 给新内核带来什么
    risk: str           # 风险 / 代价
    verdict: str        # 结论
    kind: str = "external"


CANDIDATES: tuple[Candidate, ...] = (
    # ---- 标准库里「还没用到」的能力：零成本，优先采用 ----
    Candidate(
        "asyncio.TaskGroup", "运行时",
        "结构化并发：任一子任务失败自动取消兄弟任务，替代散落的 create_task/gather",
        "需 Python 3.11+（当前 3.13 满足）", "立即采用", "stdlib",
    ),
    Candidate(
        "importlib.metadata.entry_points", "插件系统",
        "让插件可作为独立 pip 包发布并被自动发现，不依赖目录扫描",
        "需要 pyproject.toml 打包；与目录扫描共存即可", "立即采用", "stdlib",
    ),
    Candidate(
        "dataclass(slots=True, frozen=True)", "事件模型",
        "事件对象不可变 + 内存下降，避免插件误改事件导致跨通道污染",
        "与现有可变字段（event.bot/adapter）需拆分为上下文对象", "渐进采用", "stdlib",
    ),
    Candidate(
        "typing.override / PEP 695", "代码质量",
        "编译期捕获适配器接口漂移，类型检查可覆盖插件 API",
        "需要 pyright/mypy 配合", "立即采用", "stdlib",
    ),
    # ---- 外部候选：按「抗 ban 解耦」标准筛选 ----
    Candidate(
        "msgspec", "协议边界",
        "C 实现，JSON/MessagePack 解码 + 校验一遍过；高频事件解析比 json+dataclass 快 2-10 倍",
        "0.x 版本 API 仍可能变动；校验能力弱于 Pydantic",
        "可选加速层（软依赖，缺失回退 stdlib json）", "external",
    ),
    Candidate(
        "pydantic v2 + pydantic-settings", "配置 / 插件元数据",
        "带校验的分层配置（TOML+环境变量）、自动 JSON Schema、生态标准",
        "Rust 核心引入 C 扩展依赖；纯 I/O 型 bot 收益有限",
        "可选：配置复杂时启用，当前 tomllib 够用", "external",
    ),
    Candidate(
        "httpx[http2]", "官方 API 适配器",
        "异步 HTTP/2 客户端，替代 official_qq.py 里的 urllib + to_thread 组合",
        "多一个网络栈依赖；需连接池生命周期管理",
        "推荐（新内核官方通道默认用它，缺失回退 urllib）", "external",
    ),
    Candidate(
        "anyio", "运行时",
        "结构化并发抽象，同一套代码可跑 asyncio / Trio；TaskGroup 语义更严",
        "与现有纯 asyncio 代码需适配层",
        "可选；若不考虑 Trio，TaskGroup 已足够", "external",
    ),
    Candidate(
        "pluggy", "插件系统",
        "pytest 同款 hookspec/hookimpl，支持插件间钩子与调用顺序",
        "引入新范式会破坏现有 Plugin 装饰器 API —— 与「旧插件可用」冲突",
        "不采用（改用 entry_points + 兼容门面）", "external",
    ),
    Candidate(
        "watchfiles", "开发体验",
        "Rust 文件监听，实现插件热重载，改插件不用重启（uvicorn 同款后端）",
        "运行时热卸载 Python 模块易残留引用，需谨慎设计",
        "可选，仅开发模式启用", "external",
    ),
    Candidate(
        "structlog / OpenTelemetry", "可观测性",
        "结构化日志 + 链路追踪，能按 channel/event_id 追踪「消息走了哪条通道」",
        "额外依赖与采样配置",
        "推荐用于双通道排障（软依赖，缺失回退 logging）", "external",
    ),
    Candidate(
        "orjson", "序列化",
        "更快的 JSON；但 msgspec 在解码同时校验，通常更划算",
        "与 msgspec 功能重叠", "不重复引入", "external",
    ),
    Candidate(
        "APScheduler / taskiq / arq", "定时任务",
        "成熟 cron、错过触发补偿、持久化任务；当前 scheduler.py 是最小实现",
        "APScheduler 是同步栈；分布式任务需要 Redis",
        "单进程 bot 暂不需要，多实例时再上 taskiq", "external",
    ),
    Candidate(
        "SQLAlchemy 2 async / redis", "状态存储",
        "插件持久化数据（签到、积分）、跨实例会话与发件箱共享",
        "引入数据库运维成本",
        "按需：单实例先用 SQLite（标准库 sqlite3）", "external",
    ),
    Candidate(
        "uvloop / 3.13t free-threading", "性能",
        "uvloop 降低事件循环开销；无 GIL 构建对 CPU 密集插件有意义",
        "C 扩展兼容性是主要障碍；bot 是 I/O 密集，收益有限",
        "观望，不进核心依赖", "external",
    ),
    Candidate(
        "pytest / pytest-asyncio / ruff", "工程质量",
        "把现有脚本式 e2e 测试升级为可回归测试套件，类型与静态检查",
        "仅开发期依赖", "推荐（dev 依赖）", "external",
    ),
)


def probe(name: str) -> str:
    """返回依赖状态：已安装 / 未安装 / 标准库可用。"""
    if name.startswith(("asyncio.", "importlib.", "dataclass", "typing.")):
        return "标准库可用"
    mod = {
        "pydantic v2 + pydantic-settings": "pydantic",
        "httpx[http2]": "httpx",
        "structlog / OpenTelemetry": "structlog",
        "APScheduler / taskiq / arq": "apscheduler",
        "SQLAlchemy 2 async / redis": "sqlalchemy",
        "uvloop / 3.13t free-threading": "uvloop",
        "pytest / pytest-asyncio / ruff": "pytest",
    }.get(name, name.split(" /")[0].split(" ")[0])
    try:
        found = importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        found = False
    return "已安装" if found else "未安装"


def report() -> str:
    lines = [
        f"Python {sys.version.split()[0]} | 当前框架依赖：websockets + 标准库",
        "",
        f"{'依赖':<38}{'状态':<12}{'结论'}",
        "-" * 96,
    ]
    for c in CANDIDATES:
        status = "标准库可用" if c.kind == "stdlib" else probe(c.name)
        lines.append(f"{c.name:<38}{status:<12}{c.verdict}")
    lines += [
        "-" * 96,
        "核心原则：所有外部依赖均为「软依赖」——缺失时内核自动回退标准库，",
        "保证在 QQ 实现被 ban、临时换机器部署时不会因装不上包而起不来。",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(report())
