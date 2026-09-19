# 上下文管理 Checklist

> 状态：2026-09-19 用户已授权后续文件全部直接批准，本清单自动通过。
> 2026-09-19 已执行；本章功能检查通过，全量 pytest 尚有 2 项已存在的 MCP 失败。

## 功能与集成

- [x] AC1：用户首轮和工具续调均先轻量预防再检查窗口，摘要不递归。（验证：manager/agent 请求捕获测试。）
- [x] AC2：50,000/50,001 字符边界正确，存盘原文与换行可回读，预览1,500字符且关联/错误状态不变。（验证：artifacts 文件测试。）
- [x] AC3：聚合超200,000按长度降序、同长原顺序存盘，直至满足限制。（验证：artifacts 聚合测试。）
- [x] AC4：等于阈值不触发，不跨消息聚合，重复检查无嵌套预览和重复文件。（验证：artifacts 幂等测试。）
- [x] AC5：存盘失败、权限拒绝、无可缩减结果时不发送请求，原文仍可恢复。（验证：文件故障注入及manager测试。）
- [x] AC6：API usage可按请求边界查询，当前占用不累加历次输入，新增内容按增量估算。（验证：ledger/estimate测试。）
- [x] AC7：缓存归一、缺失/失败usage、模型/epoch变化、摘要锚点隔离正确。（验证：两协议、ledger/estimate测试。）
- [x] AC8：C=128,000时94,999不触发、95,000触发；system/tools改变计入预算。（验证：manager阈值测试。）
- [x] AC9：缺C、非法预算、输出余量不足正确阻止或先压缩，不发送已知非法请求。（验证：config/provider/manager测试。）
- [x] AC10：最大合法M、N>=5且后缀>10,000，不从tool开始、不拆配对、M+N=L；长工具循环可切分。（验证：manager切分测试。）
- [x] AC11：严格下限无可用前缀时跳过；实际超限仍判定，无可压缩内容明确报告。（验证：manager/agent边界测试。）
- [x] AC12：摘要请求无工具且预算合规，草稿不显示或入历史，正式摘要八项完整。（验证：summary/provider请求捕获。）
- [x] AC13：用户引文逐字可追溯，工具/助手不冒充原话，近期文本原样保留。（验证：summary/conversation测试。）
- [x] AC14：重建五部分和角色顺序正确；二次摘要不叠加旧声明或伪造用户来源。（验证：summary/manager二次摘要测试。）
- [x] AC15：最近文件成功次序、路径去重、最多5个各10,000字符，删除/拒绝如实说明。（验证：attachments真实文件与Engine测试。）
- [x] AC16：Skill压缩后仍有实际版本/片段，局部读取注明范围，总正文<=50,000，名称提及不算加载。（验证：attachments测试。）
- [x] AC17：重建超预算先裁旧文件再裁旧Skill，仍失败不提交，不改近期原文或无限摘要。（验证：manager预算与事务测试。）
- [x] AC18：摘要输入超限最多三次，每次早期估算至少减10%，移出的消息归入近期。（验证：manager错误序列测试。）
- [x] AC19：混合失败共享三次额度，非超限保持切点，无合法缩减提前停止。（验证：summary/manager故障注入。）
- [x] AC20：普通请求实际超限后摘要并仅重发一次，不增加用户消息或重复工具副作用。（验证：agent恢复测试。）
- [x] AC21：重发仍超限即停；摘要内部失败不递归，鉴权/网络/输出截断不误判。（验证：provider分类和agent测试。）
- [x] AC22：/compact不入历史、无需达到T、不自动续跑、模式保持、无前缀提示跳过。（验证：Textual run_test。）
- [x] AC23：压缩忙碌/取消/重试/用量展示正确，静默流可取消，无残留锁或任务。（验证：异步取消和Textual测试。）
- [x] AC24：两协议适配器请求捕获和完整“工具→压缩→继续”“手动→取消/失败→再输入”流程通过。（验证：SDK mock transport + Textual端到端脚本。）

## 代码检查与端到端

- [x] AC25：`uv run ruff format --check .` 通过。
- [x] AC25：`uv run ruff check .` 通过。
- [ ] AC25：`uv run pytest` 通过。
- [x] AC25：`uv run mypy src/potatocode` 通过或逐项如实记录既有基线问题。
- [x] `uv run python docs/ch08/evidence/context_e2e.py` 输出实际成功证据，包含自动/紧急/手动/取消/熔断/缺窗口。
- [x] 配置示例、README与实际C/O/A、/compact和存盘路径行为一致，不包含真实凭据。

## 实际执行记录

- 基线首次尝试：sandbox 下 uv 查询 Python 解释器被 Windows 拒绝访问，尚无代码检查结果。

### 最终结果（2026-09-19）

- AC1–AC24：对应行为测试已通过；两协议完整流程使用真实 SDK 和内存 HTTP，避免仅凭 fake provider 宣称跨协议通过。
- Ruff：项目格式通过（显式包含本章脚本共 86 个文件），静态规则全部通过。mypy：49 个源文件无问题。
- 全量 pytest：333 passed、2 failed、3 skipped（26.48 秒）。不将整个测试套件标记通过。
- 既有失败：`test_mcp_cli.py::test_amain_registers_mcp_tools_and_closes_manager` 的 FakeApp 仍是旧四参数构造器；`test_mcp_config.py::test_documented_example_parses` 引用的 `docs/mcp/mcp-servers.example.yaml` 不存在。两者在本章修改前已失败，未修改无关测试或示例。
- 跳过：1 项存盘目录符号链接、2 项既有权限符号链接测试，均因 Windows 环境不支持创建链接。
- 端到端：12/12 通过；自动、紧急、手动、取消、熔断、缺窗口及两协议 SDK 流程均有实际证据。
- 命令与覆盖映射见 [verification.md](evidence/verification.md)，端到端输出见 [context-e2e-results.md](evidence/context-e2e-results.md)。
