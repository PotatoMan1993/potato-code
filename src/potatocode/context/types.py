"""上下文共享值类型；不导入管理器或执行组件。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from ..conversation import HistorySnapshot, UserQuote
from ..llm import Request, System, ToolDefinition, Usage

RequestStatus = Literal["running", "success", "failed", "cancelled"]
CompactReason = Literal["auto", "emergency", "manual"]


@dataclass(frozen=True)
class TokenEstimate:
    tokens: int
    source: Literal["character", "usage_anchored"] = "character"
    anchor_request_id: int | None = None
    rule_version: str = "char-v1"


@dataclass
class RequestRecord:
    request_id: int
    purpose: str
    attempt: int
    provider_key: str
    model: str
    protocol: str
    history_revision: int
    content_epoch: int
    start_index: int
    end_index: int
    boundary_ids: tuple[str | None, str | None]
    input_snapshot_hash: str
    estimated_input: int
    raw_counts: dict[str, int | None] | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    status: RequestStatus = "running"
    stop_reason: str | None = None
    usage: Usage | None = None


@dataclass(frozen=True)
class UsageAnchor:
    request_id: int
    provider_key: str
    model: str
    protocol: str
    session_id: str
    content_epoch: int
    prefix: tuple[tuple[str, str], ...]
    character_cost: int
    input_tokens: int


@dataclass(frozen=True)
class RequestFrame:
    system: System
    tools: tuple[ToolDefinition, ...] = ()
    reminder: str = ""
    max_output_tokens: int = 4096

    def request(self, snapshot: HistorySnapshot) -> Request:
        return Request(
            messages=snapshot.messages(),
            system=self.system,
            tools=list(self.tools),
            reminder=self.reminder,
            max_output_tokens=self.max_output_tokens,
        )


@dataclass
class RunScope:
    circuit_broken: bool = False


@dataclass(frozen=True)
class Cut:
    m: int
    n: int
    early_tokens: int
    recent_tokens: int
    summary_input_tokens: int


@dataclass
class FileAttachment:
    path: str
    text: str = ""
    truncated: bool = False
    omitted_reason: str = ""


@dataclass
class SkillAttachment(FileAttachment):
    source_version: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    complete: bool = False


@dataclass
class AttachmentBundle:
    files: list[FileAttachment] = field(default_factory=list)
    skills: list[SkillAttachment] = field(default_factory=list)


@dataclass(frozen=True)
class SummaryDocument:
    intent: str
    approach: str
    technical_points: str
    files_and_code: str
    user_quotes: tuple[UserQuote, ...]
    unfinished: str
    current_work: str
    next_steps: str

    def render(self) -> str:
        values = [
            self.intent,
            self.approach,
            self.technical_points,
            self.files_and_code,
            "\n".join(f"[{q.source_id}] {q.text}" for q in self.user_quotes) or "无",
            self.unfinished,
            self.current_work,
            self.next_steps,
        ]
        labels = [
            "用户的主要意图",
            "解决问题的思路和方法",
            "讨论过的关键技术点",
            "涉及文件与关键代码",
            "用户说过的原话",
            "还没完成的事情",
            "当前在做什么",
            "接下来打算做什么",
        ]
        return "\n\n".join(
            f"### {label}\n{value}" for label, value in zip(labels, values, strict=True)
        )


@dataclass(frozen=True)
class CompactResult:
    status: Literal["success", "skipped", "failed", "cancelled"]
    reason: str
    m: int = 0
    n: int = 0
    before: int | None = None
    after: int | None = None
    attempts: int = 0


@dataclass(frozen=True)
class ContextEvent:
    kind: Literal["start", "retry", "usage", "success", "skipped", "failed", "cancelled"]
    reason: CompactReason
    message: str = ""
    attempt: int = 0
    record: RequestRecord | None = None
    result: CompactResult | None = None


@dataclass(frozen=True)
class PreparedRequest:
    snapshot: HistorySnapshot
    request: Request | None
    estimate: TokenEstimate
    blocked_reason: str = ""


@dataclass(frozen=True)
class SummaryAttempt:
    document: SummaryDocument | None
    record: RequestRecord
    error: Exception | None = None
    cancelled: bool = False
