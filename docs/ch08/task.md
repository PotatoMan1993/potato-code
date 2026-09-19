# 上下文管理 Tasks

> 状态：依据用户“后续文件全部直接批准”授权，于 2026-09-19 自动通过；按已批准 spec/plan 执行。
> 不再设置逐份审批停点。仅在真正改变需求范围时说明。

## 文件清单

具体文件组织沿用 plan.md；本章新增 context 包、协议错误分类、文件观察、对应测试与端到端脚本，修改会话/Agent/TUI/配置与说明。
不读取或修改真实密钥配置，不改无关代码。

## T1：记录基线与配置说明

- 状态：已完成，验证见文末记录。
- 文件：config.py、配置示例、README。
- 依赖：无。
- 步骤：运行现有 Ruff/pytest/mypy；记录已存在问题。增加 C/O/A 与兼容输出参数的配置说明。
- 验证：`uv run pytest tests/test_config.py`；期望：配置缺失/非法预算可被发送前识别，既有加载行为可用。

## T2：扩展 LLM 公共请求类型

- 状态：已完成，验证见文末记录。
- 文件：llm/__init__.py。
- 依赖：T1。
- 步骤：定义 ContextLimits、BudgetShape、StopReason；扩展 Request、Usage、StreamEvent 和 Provider。
- 验证：`uv run pytest tests/test_context_providers.py`；期望：请求用途、输出上限、缺失 usage 可表达。

## T3：接入 Anthropic 预算与用量

- 状态：已完成，验证见文末记录。
- 文件：llm/anthropic_provider.py。
- 依赖：T2。
- 步骤：复用序列化生成预算形状；处理缓存输入、流式局部 usage、停止原因、摘要关闭思考与SDK重试。
- 验证：`uv run pytest tests/test_anthropic_system.py tests/test_context_providers.py`；期望：缓存不漏计，实际请求输出上限正确。

## T4：接入 OpenAI 预算与用量

- 状态：已完成，验证见文末记录。
- 文件：llm/openai_provider.py、llm/errors.py。
- 依赖：T2。
- 步骤：显式选择输出参数；处理含 choices 的 usage、停止原因、资源关闭与输入超限分类。
- 验证：`uv run pytest tests/test_context_providers.py`；期望：输出截断不冒充输入超限，摘要无隐藏重试。

## T5：实现历史快照与稳定来源

- 状态：已完成，验证见文末记录。
- 文件：conversation.py。
- 依赖：T2。
- 步骤：增加 HistoryEntry、UserQuote、HistorySnapshot；追加分配ID，深复制视图，区分 revision 与 epoch。
- 验证：`uv run pytest tests/test_conversation.py`；期望：外部修改副本不会改变历史，普通追加不使epoch失效。

## T6：实现历史原子替换

- 状态：已完成，验证见文末记录。
- 文件：conversation.py。
- 依赖：T5。
- 步骤：添加 ToolResultPatch、结果替换和摘要前缀提交；校验版本、范围和工具配对。
- 验证：`uv run pytest tests/test_conversation.py`；期望：非法切分/版本变化不部分写入，近期ID和原文保持。

## T7：定义上下文共享值类型

- 状态：已完成，验证见文末记录。
- 文件：context/types.py、context/__init__.py。
- 依赖：T2、T5。
- 步骤：定义 frame、usage记录、估算、摘要、附件、事件、准备结果和RunScope；保持依赖无环。
- 验证：`uv run python -c "import potatocode.context.types"`；期望：公共值类型可导入。

## T8：实现逐次请求台账

- 状态：已完成，验证见文末记录。
- 文件：context/ledger.py。
- 依赖：T7。
- 步骤：实现begin/observe/finish/get；保留原usage与请求边界，累计快照更新而不相加，只有成功普通输入能作锚点。
- 验证：`uv run pytest tests/test_context_ledger.py`；期望：成功/失败/取消/摘要记录独立，结算幂等。

## T9：实现字符估算与usage锚点

- 状态：已完成，验证见文末记录。
- 文件：context/estimate.py。
- 依赖：T7、T8。
- 步骤：实现char-v1、实际协议形状成本缓存、历史前缀和、输入锚点差量及epoch失效。
- 验证：`uv run pytest tests/test_context_estimate.py`；期望：固定增量可复现，不直接把output加进历史，摘要usage不作普通锚点。

## T10：实现工具结果落盘

- 状态：已完成，验证见文末记录。
- 文件：context/artifacts.py、.gitignore。
- 依赖：T6、T7。
- 步骤：会话目录中保存完整UTF-8原文；原子替换、50,000边界、1,500预览、权限与路径检查。
- 验证：`uv run pytest tests/test_context_artifacts.py`；期望：全文和换行可读回；失败/取消保留真实历史。

## T11：实现聚合限制与幂等

- 状态：已完成，验证见文末记录。
- 文件：context/artifacts.py。
- 依赖：T10。
- 步骤：按单条消息执行200,000聚合限制；长度降序同长保序，记录原文与预览双指纹。
- 验证：`uv run pytest tests/test_context_artifacts.py`；期望：多次预检不嵌套预览，无净缩减能终止。

## T12：记录成功文件操作

- 状态：已完成，验证见文末记录。
- 文件：tool/file_observation.py、tool/__init__.py、三个文件工具。
- 依赖：T7。
- 步骤：增加默认空的结构化观察；成功读提供交付文本/范围/版本，成功写改提供路径与版本。
- 验证：`uv run pytest tests/test_tool.py`；期望：仅成功操作有观察，既有输出和工具内部上限保持。

## T13：收集最近文件与Skill

- 状态：已完成，验证见文末记录。
- 文件：context/attachments.py。
- 依赖：T12。
- 步骤：按成功次序去重；已有权限下补读5个文件各10,000字符；保留实际Skill版本/片段，合计50,000字符。
- 验证：`uv run pytest tests/test_context_attachments.py`；期望：无新增审批，拒绝/敏感来源不注入，局部Skill不称完整。

## T14：定义摘要协议与引文校验

- 状态：已完成，验证见文末记录。
- 文件：context/summary.py。
- 依赖：T7。
- 步骤：实现引用transcript、专用提示、草稿+八字段JSON、旧摘要verified_quotes映射与逐字引文验证。
- 验证：`uv run pytest tests/test_context_summary.py`；期望：草稿不进入正式文档，工具/旧摘要不冒充用户原话。

## T15：实现单次可取消摘要请求

- 状态：已完成，验证见文末记录。
- 文件：context/summary.py、context/stream.py。
- 依赖：T3、T4、T8、T14。
- 步骤：一次SDK调用收集输出和usage；正常stop才接受；180秒截止、竞争取消、释放流。
- 验证：`uv run pytest tests/test_context_summary.py`；期望：静默流可取消，失败usage保留，无工具执行或递归摘要。

## T16：实现严格历史切分

- 状态：已完成，验证见文末记录。
- 文件：context/manager.py。
- 依赖：T6、T9、T14。
- 步骤：选择最大合法M，近期>=5且>10,000，不拆工具配对；检查完整摘要预算和10%缩减。
- 验证：`uv run pytest tests/test_context_manager.py`；期望：M+N=L且无遗漏；没有合法前缀返回明确结果。

## T17：实现摘要事务与有界重试

- 状态：已完成，验证见文末记录。
- 文件：context/manager.py。
- 依赖：T10、T13、T15、T16。
- 步骤：共享最多三次额度；超限才移动切点；最终重建五部分、裁减附件、同字符口径比较进展并原子提交。
- 验证：`uv run pytest tests/test_context_manager.py`；期望：混合错误不重置次数，最终预算/取消失败不替换历史。

## T18：实现统一预检

- 状态：已完成，验证见文末记录。
- 文件：context/manager.py。
- 依赖：T17。
- 步骤：普通请求先轻量后触发；公开入口各加一次锁、内部摘要不重入；放锁后发最终结果。
- 验证：`uv run pytest tests/test_context_manager.py`；期望：阈值/输出余量两种触发均覆盖，无前缀时按实际预算跳过或阻止。

## T19：接入Agent记账与紧急恢复

- 状态：已完成，验证见文末记录。
- 文件：agent/__init__.py。
- 依赖：T4、T8、T18。
- 步骤：每iteration预检；每次请求记账；超限先摘要再最多重发一次，延后错误尾部收尾，避免工具重复执行。
- 验证：`uv run pytest tests/test_agent.py tests/test_context_agent.py`；期望：普通重发最多一次，工具副作用不重放，失败usage在err前发送。

## T20：接入观察与手动操作

- 状态：已完成，验证见文末记录。
- 文件：agent/__init__.py。
- 依赖：T13、T19。
- 步骤：成功文件结果送入观察；手动compact入口不启动普通循环，取消能打断静默网络。
- 验证：`uv run pytest tests/test_context_agent.py`；期望：手动操作不追加用户/助手占位，取消无悬挂流。

## T21：接入TUI压缩状态

- 状态：已完成，验证见文末记录。
- 文件：tui/app.py、tui/stream.py、tui/view.py。
- 依赖：T20。
- 步骤：App持有会话管理器；/compact先于add_user分派；增加COMPACTING和专用收尾，展示usage与取消。
- 验证：`uv run pytest tests/test_tui.py tests/test_context_tui.py`；期望：手动完成无空助手块，忙碌保护、模式不变、可继续输入。

## T22：两协议集成与端到端证据

- 状态：已完成，验证见文末记录。
- 文件：docs/ch08/evidence/context_e2e.py、上下文测试。
- 依赖：T21。
- 步骤：实际文件+Engine+Textual run_test驱动自动/紧急/手动/取消/熔断与缺窗口场景；SDK请求捕获验证两协议。
- 验证：`uv run python docs/ch08/evidence/context_e2e.py`；期望：端到端实际通过并输出可保存证据，无需真实密钥。

## T23：全量验证与文档回填

- 状态：已执行并回填；全量 pytest 保留 2 项既有 MCP 失败，详情见验收报告。
- 文件：README、docs/ch08/task.md、checklist.md。
- 依赖：T22。
- 步骤：执行Ruff format/check、pytest、mypy；修复本章问题；记录基线问题、实际结果及使用说明。
- 验证：`uv run ruff format --check .；uv run ruff check .；uv run pytest；uv run mypy src/potatocode`；期望：检查有实际证据，全部AC有结论。

## 执行顺序

T1→T2 后，协议适配 T3/T4、历史与类型 T5–T9、文件观察 T12 可分工。
存盘与附件 T10–T13、摘要 T14/T15 在共享类型确定后并行；管理器 T16–T18 随后集成。
T19–T21 接入真实用户流程，T22/T23 汇总验证。共享文件由单一负责人修改，先确认接口再接线。

## 验证记录

- 基线首次执行：sandbox 中 uv 无法查询 Python 解释器（Windows 拒绝访问），尚未获得代码检查结果；需在获准的运行环境重试。

### 2026-09-19 实际执行

- T1–T21 对应配置、协议、历史、台账、估算、存盘、附件、摘要、管理器、Agent、TUI 测试均通过全量运行验证；T7 公共类型在实际调用和 mypy 中完成导入验证。
- T22：两种真实 SDK 经内存 HTTP 的完整流程通过；独立端到端脚本 12/12 通过，证据见 [context-e2e-results.md](evidence/context-e2e-results.md)。
- T23：Ruff 格式与静态检查通过，mypy 49 个源文件通过；pytest 为 333 passed、2 failed、3 skipped。失败为基线已存在的 MCP 测试构造器签名和缺失示例文件，本章未改动这些测试。
- 初始基线为 181 passed、8 failed、2 skipped；TUI manager 参数兼容调整解决了其中 6 项启动失败。最终新增 1 项 Windows 无权限建立符号链接的跳过。
- 验证命令、测试覆盖和未通过项详见 [verification.md](evidence/verification.md)。
