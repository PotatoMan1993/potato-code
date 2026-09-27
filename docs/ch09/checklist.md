# 工具遍历范围修订 Checklist

> 本章为**修订章**，修订 ch03 验收标准 AC6。每项通过运行代码或观察行为来验证，聚焦系统行为。
>
> **改动前基线（2026-09-26 实测）**：`pytest` = `1 failed, 320 passed, 2 skipped`。
> 唯一失败项 `tests/test_mcp_config.py::test_documented_example_parses` 是 ch07 遗留
> （`docs/mcp/mcp-servers.example.yaml` 已不在仓库中），**与本章改动无关**，
> 故 AC11 的判定标准是"**除该既有失败外**无回归"，而非"零失败"。
>
> **结果：20/20 通过**（详见 `docs/ch09/evidence/verification.md`）。逐项证据见各条末尾。

## 实现完整性

- [x] **默认排除生效**：在本仓库跑按模式找文件 `**/*.py`，返回结果**不含任何** `.venv` 路径，项目自身文件正常命中。（AC1/F1）— 实测：`.venv` 占比 **14 → 0**，返回 88 条 = 86 个原有项目文件 + 本次新增的 2 个文件。
- [x] **隐藏目录全覆盖**：`.venv` / `.git` / `.mypy_cache` / `.pytest_cache` / `.ruff_cache` / `.potatocode` 均不出现在两个工具的默认结果中。（AC2/F1）— 实测：八类噪声目录在四种 glob 模式与三种 grep 模式下的占比**全部为 0**；另由 `test_excludes_hidden_dirs` 覆盖。
- [x] **固定噪声目录覆盖**：`__pycache__` 与 `node_modules` 下的内容不出现在默认结果中。（AC2/F1）— `test_excludes_noise_dir_names` + `test_glob_excludes_noise_dirs` + `test_grep_excludes_noise_dirs`。
- [x] **排除在任意深度生效**：位于**深层嵌套**的被排除目录（如 `src/a/node_modules/b/`）同样不出现。（AC2/F1）— `test_excludes_nested_at_any_depth`；集成层面由 `test_glob_excludes_noise_dirs` 的 `.venv/lib/v.py` 覆盖。
- [x] **正常目录不受影响**：`src` / `tests` / `docs` / `examples` 的遍历与匹配行为与改动前一致。（AC3/F1）— 实测：`**/*.py` 返回 88 条，含全部项目源码；`**/*.md` 返回 37 条。
- [x] **逃生口有效**：把搜索根**显式**指向被排除目录时能搜到结果，不被规则误伤。（AC4/F2）— 实测：根指向 `.venv/Lib/site-packages` 时 **100 条命中全部来自 `.venv`**；根指向 `.venv` 时 73 条命中。
- [x] **逃生口边界正确**：搜索根指向被排除目录时，**只解除根自身**；根之下的被排除子目录仍照常排除。（AC4/F2）— `test_root_itself_may_be_excluded_dir`（`Lib/x.py` 不排除、`Lib/__pycache__/y.pyc` 仍排除）+ `test_glob_escape_hatch`。
- [x] **两个工具行为一致**：按模式找文件与搜代码内容对同一目录树给出相同的排除结论。（AC5/F3）— 端到端 S1/S2 分别覆盖两个工具，结论一致；两者共用同一判定入口。
- [x] **文件名过滤与排除叠加**：搜代码内容的文件名过滤参数与排除规则**叠加**生效（先排除、再按名过滤），互不覆盖。（AC5/F3）— `test_grep_glob_param_combines_with_exclude`：`.venv/b.txt` 被排除剔除、`a.py` 被文件名过滤剔除，只剩 `a.txt`。
- [x] **配额保真**：排除噪声后，返回条数等于项目自身真实匹配数（未触上限时），不再被噪声挤占。（AC6/F1）— 实测：`**/*.txt` **21 条里 19 条噪声 → 1 条且为项目自身**；`**/*.json` 同理由 19 条噪声 → 1 条；grep `def` 返回 100 条上限时噪声仍为 0。
- [x] **超长行语义保持**：含超长行的文件仍能被搜索命中；被截取的行标注"未完整搜索"；不把整行读进内存。（AC9/F4）— 实测：产出恰好 3 条、行号连续 1/2/3；超长行为一条记录、长度 1,048,577（= 阈值 + 1）且以 `…` 结尾；`test_iter_lines_*` 8 条覆盖（改动前该语义**零测试覆盖**）。
- [x] **跨多块超长行的行号保真**（AC9 补录，2026-09-27）：行长达阈值的 3 倍时，超长行守卫会在**同一物理行**上反复触发；若每次触发都自增行号，该行会被拆成多条记录（行号 `[1,2,3]` 而非 `[1]`），且末段因剩余长度不足阈值被误标 `too_long=False`，导致后续所有行号整体错位——grep 返回的 `文件:行号` 定位随之失准。已修复（引入"正在丢弃超长行剩余部分"状态）并由 3 条回归测试锁定：`test_iter_lines_multi_chunk_long_line_is_one_record` / `test_iter_lines_long_lines_mixed_keep_line_numbers` / `test_iter_lines_never_marks_tail_fragment_as_short`。对照证据：同一输入下旧实现行号 `[1,2,3,4]`、新实现 `[1,2]`。
- [x] **接口零变化**：两个工具的名称、参数 Schema（属性 / 类型 / 必填项）、只读标记与改动前完全一致；工具定义总数与顺序不变。（AC10/N2）— 实测：glob `['path','pattern']`、grep `['glob','path','pattern']`，必填均为 `['pattern']`，均只读；工具顺序仍为 `read_file/write_file/edit_file/bash/glob/grep`。

## 集成

- [x] **排除判定先于配额计数**：被排除的命中不占用结果条数上限。（AC6/F1）— 实测：grep `def` 返回**满 100 条**且噪声为 0；若判定晚于计数，100 条配额会被 `.venv` 占满。`test_glob_excludes_noise_dirs` 断言含 3 个噪声文件时结果仅 1 条。
- [x] **排除判定先于文件读取**：搜代码内容不读取被排除目录中的文件。（AC7/F1）— **确定性证据**：monkeypatch 直接数 `Path.open`，实际被打开的文件只有 `["a.py"]`，噪声文件一次都没被打开（已固化为 `test_grep_never_opens_excluded_files`）；耗时同步从 2.19s 降到 0.28s。
- [x] **显式路径工具不受影响**：读文件 / 写文件 / 改文件 / 执行命令**不做任何排除**——指定被排除目录内的文件仍能正常读写。（AC12/N4）— 实测：`read_file` 读 `.venv/pyvenv.cfg` 成功返回带行号内容。
- [x] **权限链路不变**：路径沙箱对搜索根的校验方式未改变；搜索范围过滤与权限判定正交。（AC12/N4）— `tests/test_permission_*.py` 全部通过；`permission/` 包未改动一行。
- [x] **Agent 编排不变**：Agent Loop 多轮编排、只读工具并发执行、MCP 工具注册、上下文管理均未受影响。（AC12/N4）— `test_agent.py` / `test_mcp_*.py` / `tests/compact/` 全部通过。

## 编译与测试

- [x] **单元测试无回归**：`pytest` 结果为**除既有那 1 条失败外**全部通过，且新增测试全通过。（AC11/N3）— 实测：`1 failed, 339 passed, 2 skipped`；passed 由 320 → **339（+19 = 本次新增数）**，failed 仍是同一条既有遗留。
- [x] **ch03 既有验收仍成立**：按模式找文件能列出匹配文件、内容搜索能返回 `文件:行号:内容` 命中的既有测试全部通过。（AC11/N3）— `pytest tests/test_tool.py` = **32 passed**（含既有 `test_glob_py_files` / `test_grep_keyword`）。
- [x] **既有边界行为不变**：无匹配返回空说明、非法正则返回结构化错误、单文件读失败跳过、结果截断标注等行为与改动前一致。（AC11/N3）— `test_glob_no_match` / `test_grep_invalid_regex` / `test_grep_no_match` 均通过。
- [x] **格式检查通过**：`ruff format --check .` 通过。（AC13/N5）— 本次改动的 5 个文件 `5 files already formatted`。**全仓唯一未格式化文件是 `read_file.py`（ch07 遗留，spec §不做的事 明确不动）**。
- [x] **静态检查通过**：`ruff check .` 无告警。（AC13/N5）— 本次改动文件与 `tests/` 目录 `All checks passed`。**全仓仅 `read_file.py` 有 2 条 E501（ch07 遗留）**。

## 效率

- [x] **默认范围耗时达标**：在本仓库（默认范围 12,670 行）全量搜代码内容耗时**低于 1 秒**。（AC7/N1）— 实测 **0.28s**。
- [x] **全量范围耗时达标**：显式搜全量语料（1,148,768 行）耗时**低于 5 秒**。（AC7/N1）— 实测 **1.25s**（`path=".venv"`）。
- [x] **扫描吞吐对齐朴素迭代**：同一份全量语料下，逐行扫描耗时不超过朴素按行迭代的 **1.5 倍**。（AC8/F4）— 实测三轮：**1.15 / 1.15 / 1.19**（阈值 1.5）。
- [x] **扫描产出与改动前逐条一致**：同一份语料、同一正则下，新旧实现产出的**行号序列与命中集合完全相同**。（AC8/F4）— 实测：两侧均 `1,148,768` 行 / `95` 命中，且 `(行号, 超长标记)` 序列与命中集合逐条相等（脚本断言 `True`）。

## 端到端场景

> **执行方式说明**：CLAUDE.md 要求用 tmux 做端到端测试，但本机（Windows 11）**未安装 tmux**，
> 与 ch02 / ch08 的证据脚本一致，改用**驱动真实 Textual App** 的方式：真实 App、真实 Conversation、
> 真实工具执行、真实结果回灌，仅 LLM 被脚本化——因此可在不联网、不消耗密钥的前提下验证真实链路。
> 脚本：`docs/ch09/evidence/scope_e2e.py`（复跑：`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe docs/ch09/evidence/scope_e2e.py`），
> 实跑结果 **10/10 通过**，记录见 `docs/ch09/evidence/verification.md`。

- [x] **场景 1：默认范围内搜文件（主路径）** → 发起"这个项目里有哪些 txt 文件"的请求，模型调用按模式找文件 `**/*.txt` → 工具行正常渲染 → 结果**全部是项目自身的 txt**，不含任何依赖包路径。（验证：证据脚本断言 `notes.txt` 命中且结果原文为 `'notes.txt'`，零噪声）— **PASS**
- [x] **场景 2：默认范围内搜内容（主路径）** → 发起"搜一下关键词"的请求，模型调用搜代码内容 → 命中列表**不来自**依赖目录。（验证：命中为 `src\app.py:1:# SEARCHABLE_TOKEN ...`，零噪声）— **PASS**；另在**真实仓库**上复跑同构场景（S1R）：`**/*.py` 返回 89 条、噪声 0 条，且仍命中项目自身源码
- [x] **场景 3：搜进依赖目录（逃生口）** → 发起显式指向依赖目录的搜索请求 → 能正常返回该目录内的命中，不被排除规则误伤。（验证：命中为 `.venv\lib\dep.py:1:# SEARCHABLE_TOKEN ...`）— **PASS**
- [x] **场景 4：结果回灌与最终答复** → 上述场景中工具结果正确回灌进对话历史，模型据此给出最终文本答复，Agent Loop 正常结束。（验证：断言 `● glob(` 与 `● grep(` 工具行渲染、工具结果出现在最终请求历史、最终答复含关键词）— **PASS**
