# ch08 验收记录

执行日期：2026-09-19。Windows，现有项目虚拟环境（Python 3.14）；项目目标保持 Python >=3.12，未增加依赖。

## 命令与实际结果

Windows sandbox 首次无法查询解释器；以下 uv 命令经获准的执行环境运行，统一使用已有依赖：

```powershell
uv --cache-dir "$env:TEMP/potatocode-uv-cache" run --no-sync --offline ruff format --check . docs/ch08/evidence/context_e2e.py
uv --cache-dir "$env:TEMP/potatocode-uv-cache" run --no-sync --offline ruff check . docs/ch08/evidence/context_e2e.py
uv --cache-dir "$env:TEMP/potatocode-uv-cache" run --no-sync --offline pytest -q -rs
uv --cache-dir "$env:TEMP/potatocode-uv-cache" run --no-sync --offline mypy src/potatocode
uv --cache-dir "$env:TEMP/potatocode-uv-cache" run --no-sync --offline python docs/ch08/evidence/context_e2e.py
```

| 检查 | 实际结果 |
| --- | --- |
| Ruff format | 86 files already formatted |
| Ruff check | All checks passed |
| pytest | 333 passed、2 failed、3 skipped，26.48 秒 |
| mypy | Success: no issues found in 49 source files |
| 独立端到端脚本 | 12/12 PASS |

Ruff 的项目配置默认排除 docs，因此单独把本章脚本加入检查。
端到端结果文件由脚本运行生成：[context-e2e-results.md](context-e2e-results.md)。

## 覆盖映射

| 验收项 | 实际测试证据 |
| --- | --- |
| AC1–AC5 | test_context_artifacts、test_context_manager、test_context_agent：单条/聚合阈值、稳定排序、原文读回、幂等、磁盘故障与阻止请求 |
| AC6–AC9 | test_config、test_context_providers、test_context_ledger、test_context_estimate、test_context_manager：usage 归一/缺失/失败、输入锚点、epoch、精确阈值和输出预算 |
| AC10–AC11 | test_context_manager、test_context_agent：最大合法前缀、严格近期下限、完整工具配对、无可摘要前缀和实际超限 |
| AC12–AC14 | test_context_summary、test_conversation、test_context_manager、test_context_protocol_e2e：禁止工具、八字段、草稿丢弃、用户引文、原文和 ID 保留、二次重建、真实协议消息顺序 |
| AC15–AC17 | test_context_attachments、test_tool、test_context_manager：真实文件与 Engine、成功观察、最近去重、正文上限、Skill 实际片段、先裁旧附件及失败原子性 |
| AC18–AC21 | test_context_manager、test_context_summary、test_context_agent、test_context_providers：三次共享额度、10% 缩减、普通最多重发一次、工具不重复执行、分类不误判 |
| AC22–AC23 | test_context_tui、test_context_summary、test_context_agent：实际 Enter/Esc、忙碌提交保护、模式不变、静默流关闭、取消后重新输入、无空助手块 |
| AC24 | test_context_protocol_e2e：Anthropic/OpenAI 真实 SDK + httpx.MockTransport，分别完整运行多轮工具自动摘要及 Textual 手动取消/失败后继续；独立脚本亦覆盖 |
| AC25 | 上述实际执行命令；全量 pytest 的既有失败未标记通过 |

测试中的 API 密钥是固定占位值；没有访问真实 API，也没有读取本地密钥配置。协议流的格式来自项目安装的真实 SDK，网络由内存传输替代。

## 基线差异和限制

初次基线：181 passed、8 failed、2 skipped。App 的可选 MCP manager 兼容调整解决 6 项启动失败。
下列 2 项在修改前已失败，最终仍失败；本章保留无关代码：

- `tests/test_mcp_cli.py::test_amain_registers_mcp_tools_and_closes_manager`：测试 FakeApp 构造器只接收四个参数，CLI 传入五个。
- `tests/test_mcp_config.py::test_documented_example_parses`：仓库没有其引用的 `docs/mcp/mcp-servers.example.yaml`。

三项跳过均因 Windows 环境无法创建符号链接：1 项新存盘路径测试、2 项既有权限沙箱测试。
因此不能宣称全量测试全部通过或该环境已动态验证符号链接路径。

当前 token 估算仍为 usage 校准的字符近似；没有精确 tokenizer 或跨进程历史恢复。
没有实际 API 服务的在线联调结论。使用前需填写实际端点的 `context_window`。
