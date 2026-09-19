# 上下文管理 Plan

> 状态：2026-09-19 用户授权后续文档全部直接批准，本设计已通过。
> 依据：2026-09-19 用户审核通过的 `spec.md`。
> 技术栈沿用 Python 3.12+、asyncio、Textual 和现有两种 provider；不新增运行依赖。
> 按本设计生成 task.md、checklist.md 后直接实现与验证，不再逐份请求批准。

## 架构概览

新增 `potatocode.context` 包，负责上下文预算、工具结果存盘、摘要与重建。
现有 Agent 继续负责普通模型请求和工具执行，Conversation 继续作为历史的唯一写入入口。
TUI 只展示状态、提交操作和传播取消，不自行估算或改写历史。

| 组件 | 职责 | 对应需求 |
| --- | --- | --- |
| `Conversation` | 稳定消息 ID、来源、历史快照、版本检查和原子替换 | F6、F8 |
| `context.UsageLedger` | 请求级 usage 记录、原值查询和可复用锚点 | F4 |
| `context.TokenEstimator` | 字符估算、请求组成成本、前后缀成本与锚点校准 | F4、F5 |
| `context.ArtifactStore` | 工具结果完整落盘、预览替换和聚合限制 | F2、F3 |
| `context.ContextAttachments` | 最近文件、实际加载的 Skill、用户引文来源 | F7、F9 |
| `context.SummaryRunner` | 独立摘要请求、结构校验、草稿丢弃 | F7 |
| `context.ContextManager` | 预检、切分、有界重试、候选重建与提交 | F1、F5、F6、F8、F10、F11 |
| `Agent` | 接入预检、普通请求记账、紧急恢复、工具观察、手动操作入口 | F1、F4、F12、F13 |
| `llm` 两适配器 | 输出上限、usage 归一、停止原因、超限分类、可预算的实际输入形状 | F4、F5、F7、F12 |
| `tui` | `/compact`、压缩状态、取消、usage 和一次操作的收尾 | F13 |

`PotatoCodeApp` 为一份 Conversation 创建并长期持有一个 ContextManager。
当前 TUI 每轮新建 Agent，改为把同一个 ContextManager 注入每个 Agent，
因此不同用户请求共享 usage 台账、存盘索引、文件和 Skill 记录。
Provider 变化不清除会话资料，只更换请求参数并使跨模型 usage 锚点不可用。

依赖方向：

```text
tui → agent → context.manager → {conversation, context 子模块, permission, llm}
                         │
                         └→ tool（只读 FileObservation 类型）
conversation → llm
context.{ledger,estimate,summary,types} → llm / conversation
tool → llm；三个文件工具生成 FileObservation，不依赖 context
llm → config（类型/工厂）；两 provider 封装各自 SDK
```

context 不导入 agent 或 tui；事件类型定义在 context 内，由 Agent 转成现有事件流。
`context/__init__.py` 保持轻量；调用方从具体模块导入入口，不使底层模块反向导入管理器。

## 配置与协议边界

### Provider 配置

在现有 provider 条目增加字段，配置示例和说明只包含占位凭据，不读取或修改用户真实配置。

| 字段 | 默认值 | 校验与用途 |
| --- | --- | --- |
| `context_window` | `None` | 必须显式配置 C；启用请求时要求 `type(C) is int` 且 `C > 33_000` |
| `max_output_tokens` | `4_096` | 普通请求 O，要求正整数且 `O < C` |
| `compact_max_output_tokens` | `20_000` | 摘要请求 A，要求正整数且 `A <= 20_000` |
| `openai_output_limit_parameter` | `max_completion_tokens` | OpenAI 专用枚举：`max_completion_tokens` 或 `max_tokens`，兼容端点显式选择 |

布尔值不能充当整数。三个预算值在配置类型中保留供发送前检查：缺少 C 或预算值非法，
该模型在 TUI 中仍可被选择，但显示配置错误并阻止普通请求和 `/compact`；会话不清空。
整个 YAML 结构、已有必填字段等原有加载错误继续采用既有处理。
未知的 OpenAI 输出参数枚举作为配置错误报告，不猜测或请求失败后自动换参数。

`llm.ContextLimits` 为不可变值对象，含上述 C、O、A；Provider 暴露 `context_limits`。
Provider 另暴露进程内随机 `identity` 和固定 `protocol`，用于隔离不同实例的 usage 锚点；
identity 不含 API Key、headers 或端点凭据。

### Request、Usage、StreamEvent

| 类型 | 本章新增或明确的字段 |
| --- | --- |
| `Request` | `purpose: Literal["normal", "summary"] = "normal"`；`max_output_tokens: int = 4096` |
| `Usage` | `input_tokens` 统一为完整输入；未知输入/输出允许 `None`；保留缓存读写；`raw_counts` 保存原始 usage 数值字段 |
| `StreamEvent` | `stop_reason: StopReason \| None`、`raw_stop_reason: str \| None`；usage 可在流中多次更新 |
| `StopReason` | `stop`、`tool_calls`、`length`、`refusal`、`unknown` |
| `ContextWindowExceeded` | 协议无关的输入超限错误，携带经过筛选的错误码和简短原因 |
| `BudgetShape` | 实际输入的 system/tools 部分及按逻辑消息分组的 wire 消息文本，另含临时提醒造成的变化 |

`Provider.budget_shape(req: Request) -> BudgetShape` 是无网络的纯操作，
复用发送请求所用的消息、system、tools 序列化函数。
不包含鉴权信息、传输 headers、stream 选项或输出上限数字；这些不是待处理输入文本。
估算器不另写一份与实际发送逐渐分叉的协议序列化器。

适配器具体行为：

- Anthropic 将 `max_output_tokens` 映射为 `max_tokens`；普通请求沿用 thinking 行为。
  当前 thinking 预算为 2,048，只有 `O > 2_048` 且请求没有工具历史时允许开启；
  配置与此冲突时给出明确错误，不发送本地已知非法参数。
  摘要一律关闭 SDK thinking，以正文中的草稿和正式摘要承载本章输出格式。
- OpenAI 按配置只发送一种输出上限参数，不能同时发送两种。
  默认使用 `max_completion_tokens`，旧兼容端点可显式选择 `max_tokens`。
- 摘要使用 `client.with_options(max_retries=0)`，普通请求维持既有 SDK 重试策略。
  不修改共享客户端的全局选项，不单独关闭共享底层连接的 options 副本。
- OpenAI 在判断 `choices` 是否为空之前提取 usage，既处理纯 usage chunk，也处理同时带 choices 的 chunk。
  保存 finish reason；流无正常终止标志就不能把截断的工具调用当成完整结果。
- Anthropic 从 `message_start` / `message_delta` 提取已收到的 usage，正常结束再用最终 usage 校准。
  部分 usage 是累计快照，以字段更新合并，不能把每次事件都相加。
- 两适配器保证异常和取消路径释放正在消费的 stream；已收到的 usage 由台账保留。

停止原因映射：

| Anthropic / OpenAI 原值 | 归一结果 | 摘要处理 |
| --- | --- | --- |
| `end_turn`、`stop_sequence` / `stop` | `stop` | 继续校验摘要结构 |
| `tool_use` / `tool_calls` | `tool_calls` | 摘要失败，绝不执行工具 |
| `max_tokens`、`model_context_window_exceeded` / `length` | `length` | 摘要不完整，按普通摘要失败计数 |
| `refusal` / `content_filter` | `refusal` | 摘要失败 |
| 其它或未收到正常结束信号 | `unknown` | 不提交摘要 |

输出结束原因为 length 不等于输入 `prompt_too_long`。
普通请求输出达上限时可保留已正常结束的纯文本并提示输出受限；不执行不完整工具调用。
仅 API 明确拒绝输入超限才进入紧急压缩。

`llm/errors.py` 按协议、HTTP 状态和结构化错误字段分类：
OpenAI 的明确上下文错误码、Anthropic 的 400 `invalid_request_error` 且消息为输入过长，
以及兼容服务明确的 `prompt_too_long` 才归一。
消息回退匹配使用明确短语（如 `prompt is too long`、`maximum context length`），
排除输出参数非法、鉴权、限流、普通网络失败和单独的响应体过大错误。
记录错误类别和筛选后的信息，不记录整个请求或鉴权响应。

## 核心数据结构与接口

下列为设计签名；未列出的构造参数和原有消息字段沿用现有定义。
RequestRecord、UsageAnchor、TokenEstimate、RequestFrame、Cut、ContextEvent、
附件与摘要等共享值类型统一定义在 `context/types.py`；ledger/estimate/summary 只实现行为。
它们的交叉注解使用 TYPE_CHECKING，types 不反向导入管理器或各行为模块。

### Conversation：快照、来源和提交

```python
@dataclass(frozen=True)
class UserQuote:
    source_id: str
    text: str

@dataclass(frozen=True)
class HistoryEntry:
    id: str
    message: Message
    origin: Literal["user_input", "assistant", "tool", "control", "summary"]
    content_hash: str
    verified_quotes: tuple[UserQuote, ...] = ()

@dataclass(frozen=True)
class HistorySnapshot:
    session_id: str
    revision: int
    content_epoch: int
    entries: tuple[HistoryEntry, ...]

class Conversation:
    def snapshot(self) -> HistorySnapshot: ...
    def replace_tool_results(
        self, expected_revision: int, patches: list[ToolResultPatch]
    ) -> None: ...
    def replace_prefix(
        self, expected_revision: int, count: int,
        summary: str, quotes: tuple[UserQuote, ...]
    ) -> None: ...
```

- `ToolResultPatch` 定义在 conversation 中：消息 ID、结果索引、新 ToolResult。
  原有 add 方法继续可用；`add_user` 新增默认 `origin="user_input"` 参数，
  `/do` 的程序生成执行指令标记为 control，不能伪装成用户逐字说过的话。
- 每次追加或替换递增 revision；仅改写已有内容时递增 content_epoch。
  追加不会使有效的历史前缀 usage 锚点失效。
- ID 单调生成且会话内唯一；摘要替换创建新 ID，近期 entry 的 ID 保持。
  `content_hash` 包含模型可见消息字段；origin 和 verified_quotes 不送到普通请求。
- snapshot 与 messages 返回深复制或等效隔离视图；冻结外层 dataclass 不能代替对嵌套 Message 的隔离。
  所有历史改写只能经上述接口，禁止改浅复制的 ToolResult 而绕过版本检查。
- 提交先校验 expected_revision、调用/结果配对和替换范围，再一次性替换内部列表。
  版本不符报告并终止当前事务，不覆盖较新的用户内容，也不自动重跑摘要。
- verified_quotes 仅保存本次摘要实际采用的原始引文及来源 ID，不额外保留无限量旧用户全文。

### 请求台账与估算结果

```python
@dataclass
class RequestRecord:
    request_id: int
    purpose: Literal["normal", "summary"]
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
    raw_counts: dict[str, int | None] | None
    input_tokens: int | None
    output_tokens: int | None
    status: Literal["running", "success", "failed", "cancelled"]
    stop_reason: str | None

@dataclass(frozen=True)
class TokenEstimate:
    tokens: int
    source: Literal["character", "usage_anchored"]
    anchor_request_id: int | None
    rule_version: str = "char-v1"

class UsageLedger:
    def begin(self, provider: Provider, req: Request,
              snapshot: HistorySnapshot, start: int, end: int,
              attempt: int, estimate: TokenEstimate) -> int: ...
    def observe(self, request_id: int, usage: Usage) -> None: ...
    def finish(self, request_id: int, status: str,
               stop_reason: str | None) -> RequestRecord: ...
    def get(self, request_id: int) -> RequestRecord: ...

class TokenEstimator:
    def estimate_request(self, provider: Provider, req: Request,
                         snapshot: HistorySnapshot,
                         ledger: UsageLedger) -> TokenEstimate: ...
    def estimate_fresh(self, provider: Provider, req: Request) -> int: ...
    def history_prefix_costs(self, provider: Provider,
                             snapshot: HistorySnapshot) -> tuple[int, ...]: ...
```

台账在当前会话内持久于多轮请求，不写磁盘。
原始 usage 字段保留实际字段名；只保留数值及已知的未知项，不把 SDK 对象、正文或 headers 存入台账。
`finish` 幂等，同一请求只能结算一次；Agent 仅把最终结算记录转换为一次 UI usage 事件，不逐个累计流中的 usage 快照。
摘要、普通重发和有 usage 的失败/取消均有独立记录。
没有收到 usage 的记录保持 `None`；界面显示已知累计值，并可标注存在未返回用量的请求。

普通成功请求另保存轻量 `UsageAnchor`：request_id、provider_key/model/protocol、content_epoch、
输入前缀长度与指纹、当时完整输入字符成本、固定部分成本和归一后的输入 usage。
只需要最近有效锚点，不为每次请求复制完整历史；历史台账保留边界与数值即可。

### 文件与 Skill 观察

```python
# 定义在 tool/file_observation.py；Result 增加默认 None 的 file_observation。
@dataclass(frozen=True)
class FileObservation:
    path: str
    operation: Literal["read", "write", "edit"]
    delivered_text: str | None
    start_line: int | None
    end_line: int | None
    complete: bool
    truncated: bool
    source_version: str | None

class ContextAttachments:
    def observe_file(self, call_id: str, observation: FileObservation) -> None: ...
    def record_loaded_skill(self, path: str, text: str,
                            source_version: str | None,
                            complete: bool) -> None: ...
    async def collect(self, mode: Mode,
                      cancel: asyncio.Event) -> AttachmentBundle: ...
```

`AttachmentBundle` 包含按最近优先排列的 `FileAttachment` 和 `SkillAttachment`。
每项都有 path、text、truncated、omitted_reason；Skill 另有版本、范围和 complete。
同一真实路径按 Windows 大小写规整规则去重，展示路径保留可读形式。

三个内置文件工具仅在操作成功时生成结构化观察；MCP/bash/glob/grep 不生成该字段。
read_file 保留实际返回的带行号文本、真实范围、完整性和截断状态；
write/edit 只需要成功路径与写后版本，不把“已修改”展示文本当文件内容。
source_version 使用实际已读取/写入文本的摘要，不暴露文本到日志。

### 管理器与事件

```python
@dataclass(frozen=True)
class RequestFrame:
    system: System
    tools: tuple[ToolDefinition, ...]
    reminder: str
    max_output_tokens: int

@dataclass
class RunScope:
    circuit_broken: bool = False

@dataclass(frozen=True)
class CompactResult:
    status: Literal["success", "skipped", "failed", "cancelled"]
    reason: str
    m: int = 0
    n: int = 0
    before: int | None = None
    after: int | None = None
    attempts: int = 0

class ContextManager:
    def __init__(self, conv: Conversation, engine: Engine) -> None: ...
    async def preflight(self, provider: Provider, frame: RequestFrame,
                        mode: Mode, cancel: asyncio.Event,
                        scope: RunScope
                        ) -> AsyncIterator[ContextEvent | PreparedRequest]: ...
    async def compact(self, provider: Provider, frame: RequestFrame,
                      mode: Mode, cancel: asyncio.Event, scope: RunScope,
                      reason: Literal["auto", "emergency", "manual"]
                      ) -> AsyncIterator[ContextEvent | CompactResult]: ...
```

`PreparedRequest` 包含最新 snapshot、组装好的 Request、估算及可选阻断原因；
阻断时 request 为 None，调用方不能继续发送。
`ContextEvent` 的 kind 为 `start/retry/usage/success/skipped/failed/cancelled`，
附简短状态、触发原因、尝试次数和 CompactResult；usage 事件只携带已结算 RequestRecord。
每个生成器最终只产生一个结果对象。错误与结果有明确区别，不使用空消息代表失败。

管理器持有 ledger、estimator、artifacts、attachments，生命周期与 Conversation 一致。
管理器内有操作锁；正常 UI 已禁止并发，锁作为调用侧保护，不在锁内等待新的用户批准。
preflight 与 compact 两个公开入口各自获取一次锁，自动分支调用不再加锁的私有
`_compact_locked`，禁止持锁调用另一个会获取同一锁的公开入口。
最终 PreparedRequest / CompactResult 在锁释放后发出；事件消费者退出时通过 aclosing
关闭生成器，异常或取消也必须执行释放，避免取得结果后停止迭代而悬挂锁。
RunScope 每次 Agent.run 或手动命令新建，用于当前用户请求的熔断状态。
普通请求的紧急重发次数是该次 iteration 的局部状态，不和摘要尝试次数混用。

## 模块设计

### 1. `context/estimate.py` 与 `ledger.py`

字符规则固定为 `char-v1`：

```text
h(text) = ceil(ASCII 字符数 / 4) + 2 × 非 ASCII 字符数
JSON = ensure_ascii=False、sort_keys=True、separators=(",", ":")
每条 wire 消息成本 = h(规范化消息 JSON) + 8
完整请求字符估算 = 16 + system/tools 部分成本 + 所有 wire 消息成本
```

这是可复现的启发式，不是精确 tokenizer 或严格数学上界。
Anthropic system 与 tools 规范化后计入固定部分；OpenAI 的 system 消息按其实际 wire 形状计数。
环境和 reminder 按实际位置进入预算，工具名、ID、JSON 参数、错误结果也在内。
一条内部 tool 消息在 OpenAI 展开为多条 wire 消息时，各自计包装成本。
临时 reminder 改变末条消息时，重新计算受影响的末条；不污染历史或把提醒累积进去。

按协议、entry ID/content_hash 和转换形状缓存消息字符成本，维护历史前缀和。
固定部分以内容指纹缓存；新增、替换和提醒变动的部分才重新按字符计数。
序列化和发送仍按完整请求构造，但不重复对不变历史做字符估算。

普通输入估算可采用最近锚点：

```text
E_current = anchor.input_tokens
            + char_cost(current_request) - char_cost(anchor_request)
```

实际差值由新增消息成本及 system/tools/environment/reminder 的变化组成。
锚点必须满足：普通请求成功、完整输入 usage 已知、相同 provider identity/model/protocol、
相同 content_epoch 与 char-v1、原输入前缀的 ID 和 hash 未改变。
差值异常或估算变负就废弃锚点，回退完整字符估算。
不能直接加 `output_tokens`：新 assistant 按实际保留的正文/调用参数估算，
被丢弃的 thinking 或不可见推理不充当下一轮可见正文。

Anthropic 完整输入为 input + cache creation + cache read；不再叠加缓存细分类。
OpenAI 完整输入为 prompt_tokens，cached_tokens 是其内部明细，不再相加。
raw_counts 保存原值，input_tokens 保存归一值。
缺失整体 usage 为未知；只有协议明确规定缺失即零的可选缓存字段才按零处理。

轻量存盘和摘要提交使 content_epoch 增长，新的完整历史暂时使用字符估算。
摘要 usage 只计消耗；普通成功请求随后建立新锚点。
历史查询 `ledger.get(id)` 返回真实 API 观测；任意前后缀查询使用字符前缀和，明确标注估算。

### 2. `context/artifacts.py`：先落盘，再替换

存储目录为 `<engine.root>/.potatocode/context/<session-id>/tool-results/`，纳入 `.gitignore`。
文件名只使用内部消息 ID、结果序号与内容 SHA-256，不使用模型提供的文件名或路径。
预览展示绝对路径，后续 read_file 正常经过 Engine。

处理一条工具消息：

1. 在深复制的候选上，先选正文 `len(text) > 50_000` 的每个结果。
2. 写入 UTF-8 临时文件，再原子改名；保持换行原文，读回不能因 Windows 换行转换改变内容。
   只有完整写入成功后生成替换正文。
3. 替换正文包含：前 1,500 字符、原始字符数、明确的截断说明、完整路径。
   只有正文变化，调用 ID 与 is_error 不变。
4. 按替换后的正文合计字符数；超过 200,000 时，对未存盘结果按长度降序、原位置升序处理。
   仅当替换会净减少字符时应用；达到上限后停止。
5. 若不存在还能缩减的候选，报告无法满足聚合限制；不反复保存同一个结果。
6. 复核取消与快照 revision 后，以 patches 原子提交这批替换。

存盘索引以 `(entry_id, result_index)` 查找，保存 original_hash、preview_hash、artifact 路径和原长度。
当前正文匹配 preview_hash 时直接认定已存盘；匹配 original_hash 时可复用之前写好但尚未提交的文件。
两者均不匹配表示正文已变化，为新内容生成新的存盘计划，不复用旧预览。
不通过正文是否含“已存盘”判断幂等性，避免把工具原文误识别成内部预览。
已存在的对应文件需要存在性及内容指纹匹配，不覆盖无关文件。

存盘是应用内部会话存储，不伪造 write_file 工具调用，不触发新的批准或写入 allow 规则。
创建目录和最终提交前解析路径，必须位于当前项目根且没有越界符号链接。
用现有 Engine 对目标构造只读判定，只有 ALLOW 才能作为可回读的存盘位置。
写入失败、Read 被拒或路径无效时保留真实历史原文并阻止本次发送。
文件 I/O 在工作线程执行；取消后可以留下尚未引用的会话文件，但绝不提交历史替换。
不自动删除会话结果文件，也不把普通退出变成磁盘清理流程。

### 3. `context/attachments.py`：可追溯的补充上下文

Agent 在内置文件工具成功返回、转换为 llm.ToolResult 之前调用 observe_file。
完成先后用会话递增序号记录，不依赖并发工具在界面上的展示顺序。
失败、权限拒绝或没有成功结果的取消不更新“最近成功访问”。

普通文件收集步骤：

1. 规范化路径去重，选择最近的至多 5 个；已作为 Skill 注入的路径不重复注入正文。
2. 排除已知凭据来源：项目 `.potatocode` 下的运行 YAML、项目 `.potatocode.yaml`、
   本地 `settings.local.*`、`.env` / `.env.*`、`api_keys.txt`、`*.pem` / `*.key` 和认证文件 `auth.json`。
   该排除只作用于自动重注入，不修改原工具权限或主动扩大文件访问范围。
3. 在当前 mode 下构造 read_file 调用并执行 Engine.check；仅 ALLOW 才读取，
   ASK/DENY 返回说明，不创建 ApprovalRequest。
4. 在已授权路径上读取最多 10,001 字符，保留前 10,000；多出的一个字符用于判断截断。
   解析后路径必须仍在项目沙箱内；路径改变导致不再授权时停止读取。
5. 文件不存在、不可读或内容不可用则只保留路径及说明。内部补读不更新“最近使用”次序，避免自反馈。

Skill 使用记录只来自成功读取的 SKILL.md 或 record_loaded_skill。
保留加载时的文本版本与范围，不在摘要时从磁盘换成另一个版本。
局部读取与工具截断都要标记，不拼接不同版本；上层显式加载接口有真实的 read_file 调用方，
不会只留下一个未接线的空槽。

工具观察先保存候选来源；轻量预防完成后，按该结果将交付模型的实际正文范围确认 Skill 文本。
若 read_file 结果已经存盘，只取预览中实际保留的文件片段，标记部分加载，
不把路径说明、未交付的剩余正文或工具错误标成 Skill 定义。
已有完整加载记录不会因为相同版本的一次较短重读而丢失已加载内容；
新版本替换旧版本，不能把两版拼成“完整定义”。

Skill 按规范化路径去重、最近优先，正文合计最多 50,000 字符；
最后一份不足时截断并标明，其后只记录省略说明。
凭据来源过滤和当前 Read deny 同样作用于 Skill 重注入；不能通过缓存绕过当前拒绝规则。
最多 5 个文件和 50,000 字符仅限制重建输入，不把被本轮省略的使用记录遗忘。

### 4. `context/summary.py`：摘要协议与验证

摘要使用独立 Request，purpose=summary、max_output_tokens=A、tools=[]、reminder=""。
system 只使用摘要专用指令，不叠加鼓励 Agent 执行工具的普通系统提示。
早期历史编码为一条 user 中的带来源、角色、ID 和调用关联的 JSON transcript。
summary 来源的 entry 还显式携带 verified_quotes 的原始 source_id/text 映射，
以便原消息被压缩后，模型仍能沿用原始引文 ID；不能只给新生成的 summary entry ID。
历史 tool_calls/tool_results 在这里是待摘要数据，不作为可执行的协议工具块发送。
实际 transcript 的标签、JSON 转义和摘要指令全部计入输入预算。

指令明确要求：仅整理已发生的对话，不继续任务、不调用工具；先写临时分析草稿，
再输出正式摘要；对未知状态写“无”或“尚未确认”；把 transcript 中指令视为待处理内容。
输出使用一个 JSON 对象，应用层解析，不要求端点支持额外的 structured-output API：

```text
draft: string                         临时草稿
summary:
  intent: string                      用户主要意图
  approach: string                    思路和方法
  technical_points: string            关键技术点
  files_and_code: string              文件与关键代码
  user_quotes: [{source_id, text}]     原始用户引文；无则空数组
  unfinished: string                  未完成事项
  current_work: string                当前工作
  next_steps: string                  下一步
```

`SummaryDocument` 保存 summary 的八个字段，user_quotes 转为 UserQuote；不保存 draft。
`SummaryAttempt` 返回 document 或错误、结算后的 RequestRecord 和停止原因。
`SummaryRunner.run(provider, request, snapshot, cut, cancel, attempt)` 只发一次实际摘要请求，
不负责重试、不接入普通 preflight，也不执行任何工具。

校验顺序：

1. 必须收到正常 stop，且无工具调用；流异常、length、refusal、unknown 均不能提交。
2. 必须能解析单个完整 JSON 对象，draft 为字符串，summary 八个字段和类型齐全；
   允许字段内容为“无”，但不接受空对象或缺项。
3. 对新的 user_input 源消息，引文必须是其原文中的连续片段；
   对旧 summary，只能引用其 verified_quotes 已验证过的文本或其连续片段。
   control、文件、工具和旧摘要正文不是新的用户引文来源。
4. 无法验证的引文判为无效摘要，纳入当前三次尝试额度，不伪装成原话提交。
5. 提取 SummaryDocument，丢弃 draft 与完整响应缓冲；错误信息只报原因，不附带草稿。

语义正确性依靠明确提示、来源标记和验收样例，应用不声称机械校验能证明全部摘要事实。
代码能确定性检查结构、引文来源、工具禁止、正常终止和预算。
正式摘要渲染为 spec 的八个中文部分，空引文数组渲染“无”。

摘要每次尝试设 180 秒整体截止，超时计为一次普通摘要失败。
取消事件与流读取并行等待；不需要等下一个网络 chunk 才响应取消。
关闭流并回收等待任务；已收到 usage 仍记账，取消不继续用完剩余次数。

### 5. `context/manager.py`：切分与压缩事务

#### 请求前检查

每次普通 iteration 组装完整 RequestFrame，然后调用 preflight：

```text
配置有效 → 轻量预防并提交 → 更新 Skill 实际可见片段 → 取得新快照
  → 估算完整请求 E
  → E < T 且 E + O <= C：返回 PreparedRequest
  → 否则：执行一次 auto compact
       success：以新快照重建请求并复核预算
       skipped：仅 E + O <= C 时允许原请求继续
       failed/cancelled：阻断本轮
```

每次预检至多启动一个自动压缩事务，不通过 while 循环持续摘要。
同一次用户任务后续新增工具结果达到阈值时，可以在新的普通 iteration 再次自动压缩。
这是新请求前的检查，不是失败事务的无限恢复。

#### 唯一切点

`Cut` 包含 m、n、early_tokens、recent_tokens、summary_input_tokens。
`select_cut(snapshot, provider, limits, previous_cut=None) -> Cut | None`：

1. 计算历史消息字符成本前缀和，得到每个后缀的估算 token。
2. 扫描工具调用 ID 与结果关联，记录没有悬空调用的合法边界。
3. 从 `M=L-5` 向前选择最大的正整数 M，使后缀首条不是 tool、后缀成本严格大于 10,000，
   且早期和近期均不拆散调用与结果；N 恒为 L−M。
4. 用当前前缀构造真实摘要 Request，要求字符估算 `<= C−20_000`。
   不满足时继续减 M、增 N；消息没有被丢弃或移到两个区间之外。
5. 如果有 previous_cut（上次摘要输入超限），还要求 `M < previous.M` 且
   `early_tokens <= floor(previous.early_tokens * 0.9)`，仍取满足条件的最大 M。

early_tokens 固定使用早期逻辑消息的字符成本前缀和，供 10% 缩减比较；
summary_input_tokens 则计算完整摘要请求，包括 transcript 格式和固定指令。
两者不混用；不把削减固定摘要指令算作早期消息减少。
可缓存 transcript 单项序列化及前缀累计成本，最终候选仍用真实构造的请求复核。

没有合法非空前缀返回 skipped，不发空摘要请求。
严格的 5 条与 10,000 下限对三种入口一致，不额外钉住最近用户消息以来的全部工具循环。
手动压缩无可用前缀只显示跳过；自动预检按上表决定是否仍可发送；
紧急压缩无可用前缀报告“无法执行压缩：没有满足近期保留条件的早期消息”。

#### 三次尝试和候选提交

1. 所有 compact 入口先执行轻量预防（自动入口已处理的内容幂等跳过），再固定 snapshot 和 RequestFrame。
   保存 E_before_effective（可能含 usage 锚点，用于触发记录）与 E_before_fresh（旧快照的完整字符估算）；
   压缩进展只和相同口径的 E_before_fresh 比较。随后选择切点。
2. 发 start 事件。attempt 从 1 到 3，每次调用一次 SummaryRunner。
3. 先登记并发出该次 usage 事件，再处理失败或成功，避免错误收尾吞掉已返回用量。
4. 输入超限才重新选切点；网络/API/格式/结构/输出截断失败保持切点。
   重试前显示次数和错误类别；可取消的等待依次为 0.5 秒、1 秒。
5. 无合法缩减切点提前失败；三次失败则设置 RunScope.circuit_broken，阻断本轮。
6. 摘要有效后收集附件，生成一条候选 summary 来源的 user 消息，加原近期 entry。
7. 用普通 RequestFrame 做全新字符估算，不能套用摘要 usage 或旧 epoch 的普通锚点。
8. 要求 `E_after < T`、`E_after + O <= C` 且 `E_after < E_before_fresh`。
   不满足时依次从最旧文件开始删除正文，再从最旧 Skill 开始删除正文，保留来源与省略说明。
   每次移除重新检查，最多遍历有限附件列表，不产生新的摘要调用。
9. 全部可移除正文都省略后仍不满足，返回 failed，保留轻量预防后的历史。
10. 最后检查取消、revision 和合法配对；调用 replace_prefix 一次提交。
    更新 epoch 后报告 success 和实际 M、N、前后估算，清理临时响应缓冲。
    CompactResult.before/after 与成功提示均使用 fresh 字符口径并注明估算；
    触发时的 E_before_effective 保存在请求/事务记录中，不能与 fresh 值混比来认定无进展。

候选失败和摘要 API 失败分开：附件或最终预算失败直接结束事务，不重新烧摘要请求。
事务失败会终止当前 Agent.run；新用户请求或新的手动命令可建立新的 RunScope。
已有存盘与 usage 是已经发生的事实，不回滚；尚未提交的摘要候选不进入真实历史。

## Agent、TUI 与完整调用流程

### 普通请求及紧急恢复

`Agent.__init__` 新增 context 参数；普通请求组装从 `_stream_once` 移到预检前。
`_stream_once` 改为消费已准备好的 Request 和 snapshot，并负责一次流的台账。
摘要不复用这条转发普通正文的路径。

```text
Agent.run → 新 RunScope
  每个 iteration：
    固定该轮 mode/tools/system/environment/reminder
    context.preflight → Request + snapshot
    普通 attempt 1 → stream → 台账结算
      成功：提交 assistant → 有工具才执行一次 → 回灌工具结果 → 下个 iteration
      明确输入超限：
        丢弃未确认的回复/调用，通知 UI 清空本次动态文本
        context.compact(reason=emergency) → 最多三次摘要调用
          success：同一 iteration、同一 frame，仅重建历史并重发普通 attempt 2
          无法执行/失败/取消：结束本轮
        普通 attempt 2 成功：走正常提交与工具执行
        普通 attempt 2 再超限：直接结束，不再压缩
      其它失败：走既有错误收尾
```

紧急分支在 `_ensure_assistant_tail` 之前拦截；不能先追加“请求出错”消息再重发。
重发不重复 add_user，不重放已经完成的工具；MAX_ITERATIONS 统计任务推进轮，
摘要和同一次普通重发不额外制造工具执行 iteration。
自动摘要成功后普通请求仍可能实际超限，允许上述一次紧急恢复，不无限交替。

台账 begin 在发送前，observe 在 usage 到达时，finish 在 finally；
失败前已收到的 usage 要作为独立 Event 发出，再发 err，
因为当前 TUI 遇 err 会立即终止消费。
同一请求的累计 usage 快照只在 finish 后向 UI 结算一次。
失败流不提交未确认的 assistant 或 calls；紧急重发前新增 `Event(reset_reply=True)` 清理动态文本。

工具执行的现有权限、并发分批和保序回灌不改变。
只有实际成功的文件工具结果进入观察；ToolResult 的正文替换发生在全部回灌后的下一次 preflight。
原始大工具结果在 UI 仍走现有摘要展示，不为存盘额外回显全文。

### `/compact` 与取消

`Agent.compact(conv, mode, cancel)` 为手动操作入口，构造正常 RequestFrame 供重建预算，
但只调用 manager.compact(reason=manual)，不进入普通 Agent.run，不追加用户消息。
手动预算使用当前模式工具集、环境和该模式下一轮的完整 reminder。
下一次实际普通请求仍按其真实 frame 再做预检。

TUI 在 `add_user` 前识别 `/compact`；仅 IDLE 且 provider 已选择时接受。
新增 SessionState.COMPACTING，统一覆盖计时器、忙碌输入、模式切换禁用及取消快捷键。
状态变化：

| 场景 | 状态变化 | 收尾 |
| --- | --- | --- |
| 普通请求触发自动/紧急摘要 | STREAMING → COMPACTING → STREAMING | 成功后继续普通请求 |
| 手动摘要 | IDLE → COMPACTING → IDLE | 只显示结果，不渲染空 assistant 答复 |
| 无合法前缀 | 按入口恢复原工作状态或 IDLE | 明确跳过/无法执行 |
| 摘要失败或熔断 | COMPACTING → IDLE | 显示原因，保留历史 |
| Esc / Ctrl+C | 设置 cancel → 关闭流 → IDLE | 不提交候选；保留已完成 usage 和工具事实 |

新增专用 `_finish_context_operation`，不调用 `_finish_turn("")`，
手动操作不追加取消 assistant 或空 assistant；普通用户任务终止可沿用已有合法尾部收尾。
自动/紧急压缩的状态结束事件带返回目标，防止提前将仍在执行的任务设为 IDLE。
压缩草稿永不产生 Event.text；成功提示仅展示 M/N、估算变化和简短状态。
已有状态栏累计 input/output 纳入摘要消耗；若展示当前占用，明确标为“上下文约”，
不把它与累计消耗放在同一无标签数值中。

### 取消与资源释放的共同规则

- 普通流和摘要流均在读取下一事件时与 cancel.wait 竞争，网络静默时也能响应 Esc。
- 取消胜出后取消读取任务、关闭 stream、等待取消任务回收；已收 usage 在 finally 结算。
- 文件 I/O 通过 asyncio.to_thread，工作线程只生成文件/候选，不持有可修改的 Conversation。
  取消不能强行中断线程，但可以立即放弃候选；后台结束不得回调提交历史。
- App 退出取消当前操作并等待异步流清理，复用现有 MCP 退出管理。
- 管理器锁在 finally 释放；不得留下审批 Future、定时器或流读取 task 阻止后续输入。

## 文件组织

```text
src/potatocode/
├── context/
│   ├── __init__.py       公共入口
│   ├── types.py          frame、结果、事件、Cut、附件及摘要值类型
│   ├── stream.py         普通与摘要请求复用的可取消流适配
│   ├── ledger.py         请求记录、归一 usage 查询与锚点
│   ├── estimate.py       char-v1、请求形状成本、前缀和
│   ├── artifacts.py      完整工具结果存盘与轻量替换
│   ├── attachments.py    文件/Skill 使用记录、授权补读、重注入材料
│   ├── summary.py        专用提示、单次摘要调用、JSON 和引文校验
│   └── manager.py        预检、切分、事务、重试和候选提交
├── conversation.py      HistoryEntry/Snapshot、版本与原子修改
├── config.py            新预算和 OpenAI 输出参数配置
├── llm/
│   ├── __init__.py       ContextLimits、Request/Usage/StreamEvent/Provider 扩展
│   ├── errors.py         输入超限归一与筛选后的错误信息
│   ├── anthropic_provider.py
│   └── openai_provider.py
├── agent/__init__.py     注入管理器、记账、预检/恢复/手动入口、观察和取消
├── tool/
│   ├── __init__.py       Result 的可选 file_observation
│   ├── file_observation.py
│   ├── read_file.py      成功读取内容和范围的结构化观察
│   ├── write_file.py     成功写入的结构化观察
│   └── edit_file.py      成功修改的结构化观察
└── tui/
    ├── app.py           管理器生命周期、命令与状态
    ├── stream.py        事件消费、压缩显示和专用收尾
    └── view.py          COMPACTING 状态与状态文本

tests/
├── test_context_estimate.py
├── test_context_ledger.py
├── test_context_artifacts.py
├── test_context_attachments.py
├── test_context_summary.py
├── test_context_manager.py
├── test_context_providers.py
└── 现有 test_conversation/config/tool/agent/tui.py 等按接口扩展

docs/ch08/
├── spec.md
├── plan.md
├── task.md              实现任务与执行记录
├── checklist.md         验收设计与实际结果
└── evidence/context_e2e.py  实现阶段新增 Windows Textual 端到端脚本
```

另外更新 `.potatocode/config.yaml.example`、README 的配置/命令说明，以及 `.gitignore`。
不读取真实 `.potatocode/config.yaml`，不修改本地密钥或权限配置。
permission、MCP 和普通 prompt 模块复用现有公开行为，不新增工具授权旁路。

## 验证设计与需求覆盖

以下定义验证边界；具体有序任务、可勾选验收和实际结果见 task.md、checklist.md 及 evidence/verification.md。

| Spec 需求 | 设计归属 | 主要验证 |
| --- | --- | --- |
| F1 | preflight、Agent 每次 iteration | 用户首轮和工具续调都先轻量后重压；摘要不递归 |
| F2 | ArtifactStore、Conversation patches | 50,000/50,001、全文回读、换行、幂等、失败/取消不改历史 |
| F3 | 同消息聚合与稳定排序 | 200,000 边界、同长保序、跨消息不聚合、无净缩减结束 |
| F4 | Ledger、Estimator、provider | raw 与归一值、缓存、部分/缺失 usage、差量、epoch、摘要锚点隔离 |
| F5 | ContextLimits、完整 BudgetShape | C/O/A 校验、阈值相等、工具定义成本、普通输出预留不足 |
| F6 | select_cut、Conversation | 严格后缀、最大 M、完整工具配对、长工具循环、无合法切点 |
| F7 | SummaryRunner 与引文来源 | 八字段、草稿丢弃、工具返回/截断拒绝、二次摘要原话校验 |
| F8 | 候选 + replace_prefix | 五部分/角色顺序、深复制、revision 竞争、失败/取消不提交 |
| F9 | FileObservation、Attachments | 成功操作才记录、最近去重、局部 Skill、凭据/权限过滤、内容上限 |
| F10 | 最终普通预算与附件裁减 | 从旧文件到旧 Skill、无进展停止、绝不改近期原文 |
| F11 | manager 尝试循环与 RunScope | 实际请求最多 3 次、混合失败共享额度、每次超限至少减 10%、熔断 |
| F12 | Agent 单 iteration 恢复 | 真超限才触发、原请求只重发一次、UI 清失败文本、工具无重复副作用 |
| F13 | Agent.compact 与 TUI | 不入历史、不自动续跑、模式保持、取消、忙碌保护、用量显示 |

先用 stub provider 记录请求及故障序列验证控制器；再用 SDK 流事件和请求捕获验证两个适配器，
不要只用绕开适配器的假 provider 宣称跨协议通过。
工具存盘和附件使用临时项目根、真实文件及真实权限 Engine 验证。
端到端脚本通过 Textual `App.run_test()` 驱动完整输入、工具结果、压缩和继续任务，
覆盖自动、紧急、手动、取消、三次失败、未知窗口；使用受控 provider，无需真实密钥。

后续实现开始先记录现有基线；完成后执行 `uv run ruff format --check .`、
`uv run ruff check .`、`uv run pytest`、`uv run mypy src/potatocode`，
并运行 `uv run python docs/ch08/evidence/context_e2e.py`，在 checklist 中记录实际结果。
本章涉及公共类型和多层异步接口，mypy 纳入必要检查。

## 技术决策

| 决策 | 选择与理由 |
| --- | --- |
| 状态归属 | App 持有与 Conversation 一一对应的管理器，避免每轮新 Agent 丢失统计 |
| 消息版本 | revision 用于原子提交，content_epoch 用于历史变更失效；普通追加仍可复用锚点 |
| 精度边界 | 请求级 API 值真实保存，任意消息切分使用字符估算，不强行分摊输入 usage |
| 摘要输入 | 带来源的引用 transcript，工具历史当数据，避免以无工具定义请求携带待执行调用 |
| 摘要输出 | 普通文本 JSON 封装草稿与八项摘要，可跨兼容端点，不要求额外格式 API |
| 用户原话 | 用来源 ID 和原文片段验证；旧重建消息不因 user 角色获得原始用户身份 |
| 无可切前缀 | 严格下限；自动预检可在实际预算允许时跳过，真实超限仍判定并明确报告无法压缩 |
| 存盘位置 | 项目内的专属目录，符合现有文件沙箱；不自动放行路径或写权限规则 |
| 工具观察 | Result 携带结构化元数据，避免解析人类展示文本或 shell/MCP 输出猜测 |
| 附件失败 | 保留路径与真实说明；最终预算只裁减附带正文，不改近期消息 |
| 重试边界 | 摘要器一次网络尝试，管理器三次共享额度，Agent 独立限制一次普通重发 |
| 取消 | 竞争 cancel 与流读取，线程只产候选，提交前最后复核，保证静默网络也可中止 |
| 依赖与范围 | 不新增库、tokenizer、在线模型表、完整 Skill 系统或会话跨进程恢复 |

## 协议依据

- OpenAI 输出上限参数、stream usage 和请求结构依据
  [Chat Completions 官方接口](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。
- Anthropic 完整输入的缓存归一公式依据
  [官方 Prompt caching 文档](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)。
- Anthropic 流式 usage 累计事件依据
  [官方 Streaming 文档](https://platform.claude.com/docs/en/build-with-claude/streaming)。
- 原因分类不把输出截断当输入超限；具体输入窗口错误参考
  [Anthropic Context windows 文档](https://platform.claude.com/docs/en/build-with-claude/context-windows)。

## 实现对齐记录（2026-09-19）

- `context/stream.py` 抽出普通/摘要共同的取消竞争与关闭逻辑，避免静默网络无法取消；策略仍由 manager 和 Agent 各自控制。
- 请求指纹覆盖实际协议输入（包括 system、tools、reminder），使用 usage 时仍按会话、provider、模型与 epoch 校验。
- App 的 MCP manager 可省略，空 manager 时显示零服务器和零远程工具，保持既有 headless 构造调用可用。
- 新增 `tests/test_context_agent.py`、`test_context_tui.py`、`test_context_protocol_e2e.py` 和受控测试辅助模块。端到端脚本复用这些场景的断言，不读取真实配置。
- 四份文档均已按用户统一授权批准。实现与本章行为测试完成；全量基线遗留问题如实记录在验收清单，未扩展到无关 MCP 修复。
