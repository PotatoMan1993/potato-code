# 运行日志落盘 Plan

## 架构概览

本章不引入新架构层，只做三件事：**新增一个日志装配模块**、**把会话标识的生成提前到启动最前端**、**把 MCP 的临时告警通道收编到标准日志接口**。

- **`logging_setup`（新增模块）** —— 唯一负责"日志往哪写、写到多详细"的地方。对外只暴露装配、解析级别、收尾三个函数。它把文件出口挂在包级日志器 `potatocode` 上，而不是根日志器上——这是满足 F5 的关键（见技术决策 1）。
- **`cli`（改造）** —— 启动顺序由"配置 → MCP → 会话"改为"会话 → 日志 → 配置 → MCP"，使 F2 的早期覆盖成立。它是全应用唯一装配日志出口的地方。
- **`SessionContext`（扩展）** —— 增加日志文件路径字段，让"会话 id"同时决定"日志写哪"，两者天然绑定，不需要额外的查找或映射。
- **MCP 三个模块（收编）** —— 删除各自的打印辅助函数，改用标准日志接口。消息文本保持不变（只去掉 `[mcp] warn:` 前缀，由日志的来源字段承担该信息），使既有断言只需换捕获通道。

数据流：

```
cli._amain
  ├─ new_session_context(root)        → session_id、spill_dir、log_path
  ├─ setup_logging(session.log_path)  → 在 potatocode 日志器上挂文件出口
  │     └─ 此后所有 potatocode.* 的记录都落入 session.log
  ├─ config.load ──失败──→ logger.error + stderr 提示 → return 1
  ├─ mcp.new_manager ──告警──→ logger.warning ──→ session.log
  ├─ permission.new_engine ──降级──→ logger.warning ──→ session.log
  ├─ new_runtime(session=session)     → 复用前面建好的会话，不重复建
  └─ finally: close_logging(handler)
```

## 核心数据结构

### SessionContext（扩展）

现有数据类，新增一个字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 形如 `<unix_ts>-<短随机串>`，已有 |
| `spill_dir` | `str` | `.potatocode/sessions/<session_id>/tool-results`，已有 |
| `log_path` | `str` | **新增**：`.potatocode/sessions/<session_id>/session.log` |

`log_path` 与 `spill_dir` 同父目录，因此 `new_session_context` 原有的目录创建动作已经覆盖了日志文件的父目录，不需要额外的创建逻辑。

### logging_setup（新增模块）

模块级常量：

| 常量 | 值 | 说明 |
|------|-----|------|
| `PACKAGE_LOGGER` | `"potatocode"` | 包级日志器名，全部模块记录的公共祖先 |
| `LOG_FILENAME` | `"session.log"` | 会话日志文件名 |
| `LEVEL_ENV` | `"POTATOCODE_LOG_LEVEL"` | 详细度环境变量名 |
| `DEFAULT_LEVEL` | `"INFO"` | 默认详细度 |
| `LOG_FORMAT` | 见下 | 四要素格式 |

对外接口（三个函数）：

**`resolve_level(raw: str | None) -> int`**
把环境变量取到的级别名解析为标准库级别值。大小写不敏感，允许首尾空白；无法识别或为空时回落到 `DEFAULT_LEVEL`。提供给环境变量解析与测试分别调用。

**`setup_logging(log_path: str, level_name: str | None = None) -> logging.Handler | None`**
在包级日志器上装配文件出口，返回装好的处理器，供收尾时摘除。行为要点：
- 级别取 `level_name`，为 `None` 时读环境变量 `LEVEL_ENV`
- 装配前先摘除本模块此前装上的处理器，保证重复调用不叠加（测试反复装配同一进程时必需）
- 文件以 UTF-8 打开
- 任何失败（目录不可写、路径非法等）都不上抛，返回 `None`

**`close_logging(handler: logging.Handler | None) -> None`**
刷新并关闭装配时返回的处理器，并从日志器上摘除。接受 `None` 直接返回，允许调用方无条件调用。

## 模块设计

### logging_setup

**职责：** 决定日志写往何处、写到多详细、格式如何。
**对外接口：** `setup_logging` / `resolve_level` / `close_logging`
**依赖：** 仅标准库（`logging`、`os`）。不依赖项目的任何其他模块——它必须在配置层可用之前就能工作。

### cli

**职责：** 重排启动顺序，装配与收尾日志出口。
**对外接口：** 保持 `main()` 不变。
**依赖：** `logging_setup`、`compact.new_session_context`、`agent.new_runtime`。

启动顺序调整为：取工作目录 → 建会话上下文 → 装配日志 → 加载配置 → 连接 MCP → 建权限引擎 → 建运行时（复用会话）→ 启动 TUI。整个主体包在一个 `try/finally` 里，`finally` 中关闭日志出口，保证早期 `return 1` 的路径也收尾。

配置错误与权限降级两条路径：在保留原有终端提示的同时，**额外**记一条日志。

### compact/state

**职责：** `SessionContext` 增加 `log_path`，`new_session_context` 一并算出。
**对外接口：** 不变（只是数据类多一个字段）。
**依赖：** 无新增。

### agent/runtime

**职责：** `new_runtime` 增加可选的会话参数，允许复用调用方已建好的会话。
**对外接口：** `new_runtime(workspace=".", context_window=200000, session=None)`——新增参数带默认值，既有的全部调用点（含测试）无需改动。
**依赖：** 无新增。

### mcp/config、mcp/manager、mcp/tool

**职责：** 删除各自的 `_warn` 打印辅助，改用标准日志接口。
**对外接口：** 不变。
**依赖：** 新增标准库 `logging`；`sys` 若不再使用则一并移除（`manager.py` 与 `tool.py` 的 `sys` 仅服务于打印，可删；`config.py` 的 `sys` 同理）。

消息文本映射：

| 位置 | 原输出 | 新调用 |
|------|--------|--------|
| `config._warn` 各调用点 | `[mcp] warn: <消息>` | `logger.warning("<消息>")` |
| `manager._warn` 各调用点 | `[mcp] warn: <消息>` | `logger.warning("<消息>")` |
| `manager` 连接成功 | `[mcp] connected server X: N tool(s)` | `logger.info("connected server X: N tool(s)")` |
| `tool` 非法工具名 | `[mcp] warn: skip tool ...` | `logger.warning("skip tool ...")` |
| `tool` 非文本内容块 | `[mcp] warn: tool ... returned ...` | `logger.warning("tool ... returned ...")` |

## 模块交互

1. **装配时机**：`setup_logging` 必须在 `config.load` 之前调用，否则 F2 的早期事件无法覆盖。
2. **传播边界**：包级日志器的传播被关闭（见技术决策 1），此后只有 `potatocode.*` 的记录能进入文件出口。
3. **会话一致性**：`new_session_context` 产出的会话对象同时交给日志装配与 `new_runtime`，保证"日志文件所属会话"与"运行时所属会话"是同一个。
4. **收尾**：`close_logging` 在 `finally` 中调用，接受 `None`，因此装配失败（N4 降级）时不需要分支判断。

## 文件组织

```
src/potatocode/
├── logging_setup.py       [新增] 日志出口装配：resolve_level / setup_logging / close_logging
├── cli.py                 [修改] 启动顺序重排；装配与收尾出口；错误双通道
├── compact/
│   └── state.py           [修改] SessionContext 增加 log_path
├── agent/
│   └── runtime.py         [修改] new_runtime 支持复用已有会话
└── mcp/
    ├── config.py          [修改] _warn 打印 → logger.warning
    ├── manager.py         [修改] _warn 打印 → logger.warning；连接成功 → logger.info
    └── tool.py            [修改] 两处 print → logger.warning

tests/
├── conftest.py            [新增] 包级日志收集器 fixture（绕开传播关闭）
├── test_logging_setup.py  [新增] 出口装配、级别解析、隔离、降级、幂等
├── test_mcp_config.py     [修改] 5 处 capsys → 日志收集器
├── test_mcp_manager.py    [修改] 3 处 capsys → 日志收集器
└── test_mcp_tool.py       [修改] 2 处 capsys → 日志收集器

.gitignore                 [不改] 日志位于 .potatocode/sessions/ 下，已有规则覆盖
```

## 技术决策

| 决策点 | 选择 | 理由 |
|--------|------|------|
| 1. 出口挂在哪个日志器 | 挂在**包级日志器 `potatocode`** 并关闭其传播 | F5 由此**结构性成立**：依赖库的日志器不是 `potatocode` 的后代，记录永远不会到达这个出口，无需维护名字黑名单。同时关闭传播可避免记录继续上浮到根日志器——根上无处理器时标准库会启用兜底处理器把告警级写回标准错误流，那正是本章要消灭的花屏。 |
| 2. 装配失败的表现 | 返回 `None`，调用方继续运行 | N4：日志是观测手段，不是启动前提。目录不可写不应让整个助手起不来。 |
| 3. 详细度来源 | 只读环境变量，不进配置文件 | 已确认的决策。临时排查不必改动含密钥的配置文件，也不必动配置层的数据类与校验。 |
| 4. 重复装配的处理 | 装配前摘除本模块此前装的处理器 | 测试会在同一进程内反复装配；不摘除就会叠加多份出口，出现重复行，且文件句柄泄漏。 |
| 5. 文件编码 | 显式 UTF-8 | Windows 上 `FileHandler` 默认取本地编码（简体中文环境为 GBK），日志里的中文会乱码或抛编码异常。 |
| 6. MCP 消息文本 | 保留正文，去掉 `[mcp] warn:` 前缀 | 前缀的职能（"这是 MCP 模块的告警"）由日志的来源字段承担，保留会成为冗余。正文不变可让既有断言只换捕获通道、不换断言语义。 |
| 7. `new_runtime` 的参数 | 新增可选参数，不改既有签名 | 全部既有调用点（`cli` 一处、测试约十处）无需改动，降低改动面。 |
| 8. 测试的捕获通道 | 新建包级日志收集器 fixture | 传播被关闭后，测试框架默认挂在根日志器上的捕获器收不到记录。改为直接把收集器挂在包级日志器上，作为本项目的标准做法沉淀到根 `conftest.py`。 |
| 9. 日志文件的父目录 | 复用 `new_session_context` 已有的创建动作 | `spill_dir` 的创建已经连带建出会话目录，不必新增创建逻辑。 |
| 10. 异常堆栈的来源 | 不额外处理，由标准库的格式化机制自带 | 以异常级记录的调用会把堆栈附着在记录上，格式化时自动展开写入（F6）。项目现有唯一一处异常级调用无需改动。 |
| 11. 写入的同步性 | 用同步文件出口，不引入队列或异步出口 | N1：自有日志量小（单会话数十至数百行），同步写本地文件的开销远低于一次模型请求，不会造成可感知卡顿。引入异步出口会带来退出时刷盘时序与丢记录的新问题，不划算。 |
| 12. 并发安全 | 依赖标准库日志设施自带的锁，项目侧不加锁 | N2：处理器写入在标准库内部加锁，多线程并发记录不会交错或丢行。`compact/state.py` 里那些持锁临界区中的记录调用因此是安全的。 |
| 13. 会话间隔离 | 由"会话 id 决定日志路径"天然保证 | N5：不同实例的会话 id 不同，落到不同文件，不存在争抢或串写。这也是选择"每会话一文件"而非"全局单文件"带来的直接收益。 |
| 14. 不新增泄漏面 | 仅靠不采集依赖库日志，不额外做脱敏 | N3：现有 13 处调用点的参数是路径、计数、异常对象，均不含密钥；而会带出密钥与完整请求体的依赖库日志已被决策 1 结构性排除。因此无需引入脱敏规则及其误伤风险。 |
