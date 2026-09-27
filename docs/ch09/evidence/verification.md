# ch09 工具遍历范围修订 · 实际执行结果

执行环境：Windows 11 + Python 3.14.6（`.venv`），命令统一用 venv 解释器运行。
本节记录的是**实际跑出来的结果**，不是设计预期。

## 一、测试与静态检查

| 检查 | 改动前基线 | 改动后 | 结论 |
|---|---|---|---|
| `pytest`（全量） | `1 failed, 320 passed, 2 skipped` | `1 failed, 339 passed, 2 skipped` | passed **+19**（= 本次新增测试数），failed 仍是同一条既有遗留 |
| `ruff check`（本次改动的 5 个文件） | — | All checks passed | 干净 |
| `ruff format --check`（本次改动的 5 个文件） | — | 5 files already formatted | 干净 |

**既有遗留（不在本章范围）**：`tests/test_mcp_config.py::test_documented_example_parses` 因
`docs/mcp/mcp-servers.example.yaml` 已不在仓库而失败（ch07 之后）；`ruff` 对
`src/potatocode/tool/read_file.py` 报 2 条 E501 且该文件未格式化（ch07 遗留，ch08 的验证记录中已记）。
spec §不做的事 明确 read_file 一律不动，故未处理。

新增测试 19 条：`tests/test_tool_exclude.py` 8 条 + `tests/test_tool.py` 11 条
（glob 排除 2、grep 排除 4、逐行扫描 5）。

## 二、按验收标准逐条实测

### AC1 / AC2 / AC6 —— 默认排除与配额保真

在本仓库实跑，统计结果中来自被排除目录的条数：

| 调用 | 改动前（噪声条数） | 改动后 | 返回总数 |
|---|---|---|---|
| glob `**/*.py` | 14 | **0** | 88 |
| glob `**/*.txt` | 64（占 98%） | **0** | 1 |
| glob `**/*.json` | 19（占 90%） | **0** | 1 |
| glob `**/*.md` | 19（占 34%） | **0** | 37 |
| grep `import asyncio`（`*.py`） | 73（占 76%） | **0** | 22 |
| grep `def`（`*.py`） | — | **0** | 100 |

噪声判定覆盖 `.venv` / `__pycache__` / `node_modules` / `.git` / `.mypy_cache` /
`.pytest_cache` / `.ruff_cache` / `.potatocode` 八类，**全部为 0**。
`**/*.py` 返回 88 条 = 86 个原有项目文件 + 本次新增的 `exclude.py` 与 `test_tool_exclude.py`（对得上）。
`**/*.txt` 与 `**/*.json` 由"21 条里 19 条是噪声"变为"1 条且是项目自身文件"——配额不再被挤占。

### AC4 —— 逃生口（正反两向证据）

| 场景 | 结果 |
|---|---|
| `path=".venv/Lib/site-packages"`，pattern `def ` | **100 条命中，全部来自 `.venv`** |
| `path=".venv"`，pattern `import asyncio` | **73 条命中**，路径形如 `.venv\Lib\site-packages\typing_extensions.py` |
| `path="."`（项目根） | 22 条命中，**不含**任何 `.venv` 路径 |
| 传项目**绝对路径**作为根 | 22 条，与传 `.` **完全一致** |

最后一行是需要说清的一点：**豁免只针对搜索根自身，不针对根的子目录**。因此把项目根（无论相对还是
绝对）作为搜索根，`.venv` 仍被排除——"整个项目含 `.venv` 一起搜"无法用根参数表达，
要真搜依赖目录必须把根**指进**该目录内。这是 F2 的既定语义，非缺陷。

### AC7 / AC8 —— 效率与扫描等价性

搜代码内容（pattern `import asyncio`，glob `*.py`）：

| 范围 | 耗时 | 命中 |
|---|---|---|
| 默认范围（排除后 12,670 行） | **0.28s** | 22 |
| 逃生口全量（`path=".venv"`，1,148,768 行） | **1.25s** | 73 |

阈值：默认 < 1s ✓、全量 < 5s ✓。

逐行扫描吞吐（同一进程内交替跑、取多轮）：

| 轮次 | 改后实现 | 朴素按行迭代 | 比值 |
|---|---|---|---|
| 1 | 0.86s | 0.75s | 1.15 |
| 2 | 0.85s | 0.74s | 1.15 |
| 3 | 0.87s | 0.73s | 1.19 |

阈值 ≤ 1.5 倍 ✓。比值在轮次间稳定而绝对耗时漂移——这正是 AC8 用比值而非绝对耗时定判据的原因。

**产出等价性**：同语料下新旧实现对照，行数 `1,148,768` 两侧相同、命中 `95` 两侧相同，
且 `(行号, 超长标记)` 序列与命中集合**逐条相等**（脚本断言 `True`）。

### AC9 —— 超长行语义（改动前零测试覆盖，本次新增）

实测行为（阈值 `_MAX_LINE_CHARS` = 1,048,576）：对"短行 + 超长行 + 短行"的文件，产出恰好 3 条、
行号连续 1/2/3；超长行为**一条**记录，文本长度 `1,048,577`（= 阈值 + 1）且以 `…` 结尾、`too_long=True`。
恰好等于阈值的行**不**截断（守卫条件是 `>`）。无换行结尾的超长行同样被标记截断、不抛异常。

**补录（2026-09-27）：上述结论存在盲区，超长行跨多块时行号会错位。**

上一段的实测语料用的是"阈值 + 100"的超长行——只溢出不足一个读取块，守卫只触发一次，
恰好绕开了缺陷。缺陷在行长达**阈值 3 倍**时暴露：

| 输入 | 旧实现产出 | 新实现产出 |
|---|---|---|
| `'A' × (阈值×3) + "\nTAIL\n"` | 行号 `[1, 2, 3, 4]`，`TAIL` 报成第 4 行 | 行号 `[1, 2]`，`TAIL` 报成第 2 行 |

根因：守卫分支 `if "\n" not in buf and len(buf) > max_line_chars:` 每次都 `lineno += 1`，
却没有"这一行已经产出过"的状态，于是每攒满一个缓冲就把同一物理行当作新行再报一次。
连带两个后果：① 超长行的**末段**因剩余长度不足阈值而被标成 `too_long=False`，
正好抵消了这段代码想给的"未完整搜索"警告；② 其后所有行的行号被顶高，`文件:行号` 定位失准。

该缺陷**并非 ch09 引入**：ch03 的初版 `_iter_lines`（`dd93290`）已是同样写法，
ch09 的 F4 改写逐字保留了这一分支（plan.md / task.md 明确要求"守卫分支逐字保留"）。
本次修复因此**偏离** plan 的"原样保留"约定——这是必要的：该分支的语义本身有缺陷，
AC9 要求的是"超长行语义保持"，而错位行号与之直接冲突。

修复方式：引入 `dropping` 状态，超长行的剩余部分整段丢弃（不产出、不涨行号），
直到遇到换行才恢复，使行号与物理行严格一一对应。内存上界不变（丢弃期间缓冲每块清零）。

**回归验证**：把修复前的实现塞回测试模块，3 条新测试全部 FAIL、而原有的
`test_iter_lines_memory_bounded` 仍 PASS——后者正是当初放过该缺陷的原因（它只断言
`len(text) <= 阈值 + 1`，不检查产出条数与行号，而 3 倍阈值恰好产出 3 条且条条满足长度断言）。

**端到端**：含 3 倍阈值超长行的文件，grep 命中行号由修复前的 `:3:` / `:4:` 修正为 `:2:` / `:3:`。

### AC12 / N2 —— 显式路径工具不受影响、接口零变化

| 检查 | 结果 |
|---|---|
| `read_file` 读 `.venv/pyvenv.cfg` | 成功返回内容（`1	home = ...`），**未被排除规则影响** |
| glob 参数 Schema | `['path', 'pattern']`，必填 `['pattern']`，只读 `True` |
| grep 参数 Schema | `['glob', 'path', 'pattern']`，必填 `['pattern']`，只读 `True` |
| 工具总数与顺序 | `['read_file', 'write_file', 'edit_file', 'bash', 'glob', 'grep']`（与改动前一致） |

`read_file` / `write_file` / `edit_file` / `bash` 四类接收显式目标、无遍历行为的工具，代码未改动一行。

**排除先于文件读取的确定性证据**：用 monkeypatch 直接数 `Path.open` 调用（不靠耗时推断），
在含 `a.py` / `.venv/b.py` / `node_modules/c.py` 的目录树上跑一次 grep，
实际被打开的文件只有 `["a.py"]`——噪声目录中的文件**一次都没被打开**。
该断言已固化为 `tests/test_tool.py::test_grep_never_opens_excluded_files`。

## 三、端到端（`scope_e2e.py`，10/10 通过）

驱动**真实 Textual App + 真实 Conversation + 真实工具执行 + 真实结果回灌**，仅 LLM 脚本化。
本机未安装 tmux，沿用 ch02 / ch08 的证据脚本方式替代。

```
[PASS] S1  glob 默认范围命中项目自身的 notes.txt
[PASS] S1  glob 结果不含任何被排除目录            原文='notes.txt'
[PASS] S2  grep 默认范围命中项目自身源码           src\app.py:1:# SEARCHABLE_TOKEN ...
[PASS] S2  grep 命中不来自被排除目录
[PASS] S3  搜索根指向 .venv 时能搜进去             .venv\lib\dep.py:1:# SEARCHABLE_TOKEN ...
[PASS] S4  工具行在 TUI 正常渲染
[PASS] S4  工具结果回灌进最终请求的历史
[PASS] S4  模型给出最终文本答复
真实仓库: D:\AiLearning\code\potato-code
[PASS] S1R 真实仓库 glob **/*.py 结果不含被排除目录   89 条，噪声 0 条
[PASS] S1R 真实仓库仍能命中项目自身源码             样本=['docs\ch02\evidence\...', ...]
通过 10/10 项
```

复跑命令：`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe docs/ch09/evidence/scope_e2e.py`
