# 运行日志落盘 Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|------|------|------|
| 修改 | `src/potatocode/compact/state.py` | `SessionContext` 增加 `log_path` |
| 新建 | `src/potatocode/logging_setup.py` | 日志出口装配三函数 |
| 修改 | `src/potatocode/agent/runtime.py` | `new_runtime` 支持复用已有会话 |
| 修改 | `src/potatocode/cli.py` | 启动顺序重排、装配与收尾、错误双通道 |
| 修改 | `src/potatocode/mcp/config.py` | `_warn` 打印 → 标准日志接口 |
| 修改 | `src/potatocode/mcp/manager.py` | `_warn` 与连接信息 → 标准日志接口 |
| 修改 | `src/potatocode/mcp/tool.py` | 两处打印 → 标准日志接口 |
| 新建 | `tests/conftest.py` | 包级日志收集器 fixture |
| 修改 | `tests/test_mcp_config.py` | 捕获通道由标准错误流改为日志收集器 |
| 修改 | `tests/test_mcp_manager.py` | 同上 |
| 修改 | `tests/test_mcp_tool.py` | 同上 |
| 新建 | `tests/test_logging_setup.py` | 出口装配的行为测试 |

## T1: SessionContext 增加日志路径

**文件：** `src/potatocode/compact/state.py`
**依赖：** 无

**步骤：**
1. 在 `SessionContext` 数据类中，`spill_dir` 之后新增字段 `log_path: str`，注释说明其为 `.potatocode/sessions/<session_id>/session.log`
2. 在 `new_session_context` 中，由同一个 `session_id` 拼出 `log_path`（父目录与会话目录相同），并入返回值
3. 保持既有的 `spill_dir` 创建动作不变——它已连带建出会话目录，不需要为日志文件再加一次创建

**验证：** `uv run python -c "from potatocode.compact import new_session_context; s=new_session_context('.'); print(s.log_path)"` 输出的路径以 `session.log` 结尾，且其父目录与 `spill_dir` 的祖父目录一致（即同为 `.potatocode/sessions/<id>`）

## T2: 日志装配模块——级别解析

**文件：** `src/potatocode/logging_setup.py`（新建）
**依赖：** 无

**步骤：**
1. 建立模块与模块级 docstring（中文），说明这是唯一决定日志写往何处、写到多详细的地方
2. 定义模块常量：包级日志器名 `potatocode`、日志文件名 `session.log`、环境变量名 `POTATOCODE_LOG_LEVEL`、默认级别 `INFO`、四要素格式串与时间格式串
3. 实现 `resolve_level(raw: str | None) -> int`：空值或非字符串回落到默认级别；否则去首尾空白、转大写后在标准库的级别名表中查找，命中即返回对应级别值，未命中回落默认级别
4. 按项目注释约定，为函数内部写明"功能"注释

**验证：** `uv run python -c "from potatocode.logging_setup import resolve_level as r; import logging; print(r(None)==logging.INFO, r('debug')==logging.DEBUG, r('  WARNING ')==logging.WARNING, r('bogus')==logging.INFO)"` 输出四个 `True`

## T3: 日志装配模块——装配与收尾

**文件：** `src/potatocode/logging_setup.py`
**依赖：** T2

**步骤：**
1. 实现 `setup_logging(log_path: str, level_name: str | None = None) -> logging.Handler | None`：
   - 取包级日志器；先摘除并关闭本模块此前装上的处理器（用一个模块级变量记住当前处理器），避免重复装配叠加
   - 级别：`level_name` 为 `None` 时读环境变量，再交给 `resolve_level`
   - 在日志器上设置级别，并把传播关闭（附注释说明：既满足只收自有日志，又避免记录上浮到根日志器触发兜底输出）
   - 建文件出口，显式指定 UTF-8 编码；挂上四要素格式串
   - 整个过程包在 `try/except Exception` 里，失败时回到"未装配"状态并返回 `None`
2. 实现 `close_logging(handler: logging.Handler | None) -> None`：`handler` 为 `None` 直接返回；否则从日志器摘除、刷新并关闭

**验证：** `uv run python -c` 一次性脚本：在临时目录上装配出口，用包内与包外的两个日志器各记一条，关闭出口后读文件——文件里只有包内那条

## T4: new_runtime 支持复用会话

**文件：** `src/potatocode/agent/runtime.py`
**依赖：** T1

**步骤：**
1. 给 `new_runtime` 增加末位可选参数 `session: SessionContext | None = None`
2. 构造时：传入则直接用，未传才调 `new_session_context(workspace)`
3. 更新函数 docstring，说明该参数用于复用启动早期已建好的会话

**验证：** `uv run python -c` 脚本：传入一个自建会话，断言返回的 `runtime.session` 与传入的是同一对象

## T5: cli 重排启动顺序

**文件：** `src/potatocode/cli.py`
**依赖：** T1、T3、T4

**步骤：**
1. 在 `_amain` 开头（任何配置读取之前）取工作目录、建会话上下文
2. 紧接着装配日志出口，并记一条信息级启动日志（含会话 id 与工作目录）
3. 用 `try/finally` 包住整个主体，`finally` 中调用 `close_logging(handler)`
4. 配置错误分支：在保留原终端提示的同时，额外记一条错误级日志，再 `return 1`
5. 权限引擎降级分支：同样在保留终端提示的同时，额外记一条告警级日志
6. 建运行时处，把已建好的会话传进去，不再让它自建
7. 原有的 `manager.close()` 收尾保持不变，与日志收尾共存于 `finally`

**验证：** `uv run python -m potatocode` 在无配置的临时目录下运行，退出码为 1，且该会话目录下出现 `session.log`，内容含配置错误那条记录

## T6: 收编 mcp/config.py 的输出

**文件：** `src/potatocode/mcp/config.py`
**依赖：** 无

**步骤：**
1. 模块顶部建立标准库日志器，说明模块用途的 docstring 不变
2. 删除 `_warn` 辅助函数，其各调用点改为告警级日志调用，消息正文不变、去掉 `[mcp] warn: ` 前缀
3. `sys` 若无其他用途则从导入中移除

**验证：** `uv run ruff check src/potatocode/mcp/config.py` 通过，无未使用导入告警

## T7: 收编 mcp/manager.py 的输出

**文件：** `src/potatocode/mcp/manager.py`
**依赖：** 无

**步骤：**
1. 模块顶部建立标准库日志器
2. 删除 `_warn`，其调用点改为告警级日志调用（消息正文不变、去前缀）
3. 连接成功那条改为信息级日志调用（消息正文不变、去前缀）
4. `sys` 若无其他用途则移除

**验证：** `uv run ruff check src/potatocode/mcp/manager.py` 通过

## T8: 收编 mcp/tool.py 的输出

**文件：** `src/potatocode/mcp/tool.py`
**依赖：** 无

**步骤：**
1. 模块顶部建立标准库日志器
2. 非法工具名、非文本内容块两处打印改为告警级日志调用（消息正文不变、去前缀）
3. `sys` 若无其他用途则移除

**验证：** `uv run ruff check src/potatocode/mcp/tool.py` 通过

## T9: 测试用日志收集器 fixture

**文件：** `tests/conftest.py`（新建）
**依赖：** 无

**步骤：**
1. 定义一个继承标准库处理器基类的收集器：把收到的记录存进列表，`emit` 方法追加记录
2. 提供 `text()` 之类的便捷读取，把所有记录拍平成一个字符串，便于沿用"某子串在不在里面"的断言语义
3. 定义名为 `pkg_logs` 的 fixture：把收集器挂到包级日志器上，同时把该日志器的级别临时提到调试级（保证信息级记录也能被收集），并把原级别记下来；`yield` 后在收尾中摘除收集器、还原原级别
4. 附文件级 docstring，说明为何不能依赖测试框架默认的根日志器捕获（传播已被关闭）

**验证：** `uv run pytest tests/ --collect-only -q` 无导入错误（此时 T6–T8 已改动 MCP 输出，相关用例尚未更新，因此只验证 fixture 可被正常加载，不跑用例）

## T10: 更新 test_mcp_config.py 的捕获通道

**文件：** `tests/test_mcp_config.py`
**依赖：** T6、T9

**步骤：**
1. 逐处替换：`capsys` 参数改为 `pkg_logs`
2. 断言从"标准错误流内容"改为"收集到的日志文本"，断言的子串去掉 `[mcp] warn: ` 前缀、保留正文
3. 那处断言"没有任何输出"的用例，改为断言收集到的记录为空
4. 注意有一处断言同时读了标准错误流与其它内容，逐行核对语义等价后再改

**验证：** `uv run pytest tests/test_mcp_config.py -q` 通过

## T11: 更新 test_mcp_manager.py 的捕获通道

**文件：** `tests/test_mcp_manager.py`
**依赖：** T7、T9

**步骤：**
1. 三处用例的 `capsys` 参数改为 `pkg_logs`
2. 断言子串去掉 `[mcp] warn: ` 前缀、保留正文
3. 注意其中有同名的用例参数需一并调整

**验证：** `uv run pytest tests/test_mcp_manager.py -q` 通过

## T12: 更新 test_mcp_tool.py 的捕获通道

**文件：** `tests/test_mcp_tool.py`
**依赖：** T8、T9

**步骤：**
1. 两处用例的 `capsys` 参数改为 `pkg_logs`
2. 断言子串去掉 `[mcp] warn: ` 前缀、保留正文；保留"只告警一次"的计数语义

**验证：** `uv run pytest tests/test_mcp_tool.py -q` 通过

## T13: 日志装配的行为测试

**文件：** `tests/test_logging_setup.py`（新建）
**依赖：** T3、T9

**步骤：**
1. 每个用例都在 `tmp_path` 上装配出口，用后即调收尾函数关闭（避免句柄泄漏到后续用例）
2. 覆盖：默认级别下信息级落盘、调试级不落盘；显式传调试级后调试级落盘
3. 覆盖：环境变量被读到（用 monkeypatch 设置）
4. 覆盖：日志器名不属于本项目包的记录不落盘（第三方隔离）
5. 覆盖：中文内容落盘后按 UTF-8 读回无乱码
6. 覆盖：以异常级记录时，堆栈写入文件
7. 覆盖：连续装配两次，只产生一份出口，记录不重复
8. 覆盖：路径不可写时返回 `None` 且不抛异常；收尾函数接受 `None`
9. 覆盖：传播已关闭（记录不会上浮到根日志器）

**验证：** `uv run pytest tests/test_logging_setup.py -q` 全部通过

## T14: 全量验证

**文件：** 无（只跑命令）
**依赖：** T1–T13

**步骤：**
1. 运行格式检查、静态检查、基于类型的静态检查
2. 运行全量测试，与改动前基线比对

**验证：** 格式与静态检查无输出；全量测试结果与基线 `2 failed, 341 passed, 2 skipped` 相比，失败项仍为原来那 2 个（无新增），通过数增加

## 执行顺序

```
T2 → T3 ─┐
T1 ──────┼→ T5 ─┐
T4 ──────┘      │
                ├→ T9 → T10/T11/T12 ─┐
T6, T7, T8 ─────┘                    ├→ T14
                     T13 ────────────┘
```

可并行：T1、T2、T4、T6、T7、T8 互不依赖；T10–T12 三者互不依赖。
