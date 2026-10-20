"""Hook 的规则 / 条件 / 动作数据结构（ch14 F8/F11-F16）。

一条 Hook 规则是「事件 + 条件 + 动作」三要素，外加三个执行控制开关
（only_once / async / timeout）。条件复用 `permission.Matcher` 的四种匹配实现；
动作四类（shell / prompt / http / subagent）各自持有专属字段。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .event import Event

if TYPE_CHECKING:
    from ..permission.matcher import Matcher


class CombineMode(str, enum.Enum):  # noqa: UP042 - plan 技术决策：str+Enum（YAML 字面量直接对应）
    """多原子条件的组合方式：all_of 全部满足 / any_of 任一满足，二选一不混用（F11）。"""

    ALL_OF = "all_of"
    ANY_OF = "any_of"


class ActionType(str, enum.Enum):  # noqa: UP042 - plan 技术决策：str+Enum（YAML 字面量直接对应）
    """四种动作类型（ch14 G7/F16）；subagent 本期占位不实现。"""

    SHELL = "shell"
    PROMPT = "prompt"
    HTTP = "http"
    SUBAGENT = "subagent"


@dataclass
class AtomCondition:
    """单个原子条件：payload 字段路径 + 匹配器（ch14 F12）。"""

    field: str  # 形如 "tool_input.path"，`.` 分隔嵌套
    matcher: Matcher  # 复用 permission.Matcher 的四种实现


@dataclass
class Condition:
    """一组原子条件的组合：mode 二选一（all_of / any_of），不嵌套混用（ch14 G5）。"""

    mode: CombineMode
    atoms: list[AtomCondition]


@dataclass
class ShellAction:
    """shell 动作：命令串由 `sh -c` 解释执行，payload JSON 从 stdin 传入（ch14 F17）。"""

    command: str


@dataclass
class PromptAction:
    """prompt 动作：把 text 注入下一轮 LLM 请求的 reminder 区（ch14 F20）。"""

    text: str


@dataclass
class HttpAction:
    """http 动作：发一次 HTTP 请求，body 可为模板或缺省 payload JSON（ch14 F23）。"""

    url: str
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    body: str | None = None  # str.format_map 模板；None 表示用 payload JSON 做请求体


@dataclass
class SubagentAction:
    """subagent 动作：本期占位，仅加载校验字段完整（ch14 F26）。"""

    agent_name: str
    prompt: str


@dataclass
class Action:
    """动作统一载体：type 决定哪个子字段有效。"""

    type: ActionType
    shell: ShellAction | None = None
    prompt: PromptAction | None = None
    http: HttpAction | None = None
    subagent: SubagentAction | None = None


@dataclass
class Rule:
    """一条已编译的 Hook 规则：加载期校验完成、matcher 与动作子对象就绪。"""

    name: str
    event: Event
    action: Action
    condition: Condition | None = None  # None 表示无条件触发（ch14 G3）
    only_once: bool = False  # 同一会话内只跑一次（ch14 F27）
    asyncio_mode: bool = False  # 对应 YAML 的 `async`（Python 关键字改名，ch14 G8）
    timeout_s: float = 30.0  # 命令 / HTTP 最大执行时长（ch14 F18/F24）
    source: str = ""  # 来源文件路径，供 /hooks 显示（ch14 F34）


# Payload 是事件分派时携带的上下文数据；条件求值与动作输入都用它。
# 序列化为 JSON 时保证 key 字典序（ch14 N6）：json.dumps(payload, sort_keys=True)。
Payload = dict[str, Any]


__all__ = [
    "Action",
    "ActionType",
    "AtomCondition",
    "CombineMode",
    "Condition",
    "HttpAction",
    "Payload",
    "PromptAction",
    "Rule",
    "ShellAction",
    "SubagentAction",
]
