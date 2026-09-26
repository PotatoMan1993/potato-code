# 工具遍历范围修订 Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|---|---|---|
| 新建 | `src/potatocode/tool/exclude.py` | `EXCLUDED_DIR_NAMES`、`_is_excluded_dir`、`is_excluded` —— 排除判定的唯一定义处 |
| 新建 | `tests/test_tool_exclude.py` | `is_excluded` 判定的单元测试（含逃生口与边界） |
| 修改 | `src/potatocode/tool/glob_tool.py` | 遍历循环中插入排除判定（`is_file()` 之后、计数之前） |
| 修改 | `src/potatocode/tool/grep_tool.py` | ① 遍历循环中插入排除判定（`open()` 之前）；② 重写 `_iter_lines` 切分循环 |
| 修改 | `tests/test_tool.py` | 补 glob / grep 的排除行为集成测试；补 `_iter_lines` 超长行回归与吞吐对照测试 |

不改动：`read_file.py` / `write_file.py` / `edit_file.py` / `bash.py` / `tool/__init__.py`（结果上限常量保持 100）。

## T1: 新增排除判定模块

**文件：** `src/potatocode/tool/exclude.py`（新建）
**依赖：** 无

**步骤：**

1. 写模块 docstring：`"""遍历排除规则：两个遍历类工具（glob / grep）共用的默认排除判定（ch09 F1/F2）。"""`
2. 定义固定噪声目录名常量，用 `frozenset`（热路径成员检查 + 冻结不可改）：
   ```python
   EXCLUDED_DIR_NAMES: frozenset[str] = frozenset({"__pycache__", "node_modules"})
   ```
3. 定义单目录名判定 `_is_excluded_dir(name: str) -> bool`：`name.startswith(".") or name in EXCLUDED_DIR_NAMES`。注释写明隐藏目录由"`.` 开头"统一覆盖、不逐个列举。
4. 定义对外接口 `is_excluded(path: Path, root: Path) -> bool`：
   - `rel = path.relative_to(root)`；捕获 `ValueError` 时 `return False`；
   - `return any(_is_excluded_dir(part) for part in rel.parts[:-1])`
5. docstring 必须写清三件事（复查时容易被当成 bug 的两处都在这里）：
   - 只看**目录分量**（`parts[:-1]`），故根下的 `.foo.py` 不被排除；
   - 判定基于**相对 root** 的路径，故 root 自身是被排除目录时规则失效——这就是逃生口（F2）；
   - 不在 root 之下时返回 `False`（失败放开），理由：这是**可见性**过滤，静默隐藏比多显示更糟；且两个遍历 API 产出必在 root 下，该分支仅防御。

**验证：**
```
ruff check src/potatocode/tool/exclude.py
.venv/Scripts/python.exe -c "from potatocode.tool.exclude import is_excluded; from pathlib import Path; r=Path('/p'); print(is_excluded(r/'.venv'/'x.py', r), is_excluded(r/'src'/'a.py', r))"
```
期望：`ruff` 无告警；输出 `True False`。

## T2: 排除判定的单元测试

**文件：** `tests/test_tool_exclude.py`（新建）
**依赖：** T1

**步骤：** 写 8 个测试函数（模块 docstring 为 `"""tool.exclude 排除判定单测（ch09 F1/F2）。"""`）：

1. `test_excludes_hidden_dirs` —— `.venv/Lib/x.py`、`.git/config`、`.mypy_cache/3.12/a.json` 均判排除（F1）
2. `test_excludes_noise_dir_names` —— `__pycache__/x.pyc`、`node_modules/lodash/index.js` 均判排除（F1）
3. `test_excludes_nested_at_any_depth` —— `src/a/node_modules/b/c.js`、`src/.cache/d/e.py` 均判排除（F1 的"任意深度"）
4. `test_keeps_normal_dirs` —— `src/a.py`、`docs/x.md`、`b.py` 均不排除（F1）
5. `test_root_itself_may_be_excluded_dir` —— **逃生口 + 边界**：
   ```python
   root = Path("/proj/.venv")
   assert not is_excluded(root / "Lib" / "x.py", root)               # 根自身不再被判定
   assert is_excluded(root / "Lib" / "__pycache__" / "y.pyc", root)  # 根之下照常生效
   ```
   注意第二行断言是 `is_excluded`（逃生口只解除根自身，不解除根之下的子目录）
6. `test_dotfile_is_not_excluded` —— `.foo.py`、`src/.env` 不排除（F2 只看目录分量）
7. `test_path_outside_root_not_excluded` —— `is_excluded(Path("/elsewhere/a.py"), Path("/proj"))` 为 `False`（防御分支）
8. 复用 T1 的常量做一次自证：断言 `EXCLUDED_DIR_NAMES == frozenset({"__pycache__", "node_modules"})`，并断言 `".venv" not in EXCLUDED_DIR_NAMES`（隐藏目录靠句点规则覆盖，不靠名单）

**验证：**
```
.venv/Scripts/python.exe -m pytest tests/test_tool_exclude.py -v
```
期望：8 个测试全通过。

## T3: glob 接入排除判定

**文件：** `src/potatocode/tool/glob_tool.py`
**依赖：** T1

**步骤：**

1. 加导入：`from .exclude import is_excluded`
2. 在 `execute` 的遍历循环中，`if not p.is_file(): continue` **之后**、`rel = str(p.relative_to(root))` **之前**插入：
   ```python
   if is_excluded(p, root):
       continue  # 跳过噪声目录，100 条配额留给项目自身（F1/F2）
   ```
3. **位置不可挪到计数之后**：被排除的命中若先计入 `matches`，仍会占满上限、挤掉项目文件（AC6）。

**验证：**
```
.venv/Scripts/python.exe -m pytest tests/test_tool.py -k glob -v
.venv/Scripts/python.exe -c "
import asyncio,sys; sys.path.insert(0,'src')
from potatocode.tool.glob_tool import GlobTool
r=asyncio.run(GlobTool().execute('{\"pattern\": \"**/*.py\"}'))
ls=r.content.splitlines(); print('返回',len(ls),'条, .venv 占', sum(1 for l in ls if l.startswith('.venv')))
"
```
期望：既有 glob 测试仍通过；输出 `.venv 占 0`（改动前为 14）。

## T4: grep 接入排除判定

**文件：** `src/potatocode/tool/grep_tool.py`
**依赖：** T1

**步骤：**

1. 加导入：`from .exclude import is_excluded`
2. 在遍历循环中，`if not p.is_file(): continue` **之后**、`try: with p.open(...)` **之前**插入：
   ```python
   if is_excluded(p, root):
       continue  # 不读噪声目录，省下扫描开销（F1/F2）
   ```
3. 必须放在 `open()` **之前**——放在之后只能过滤输出、省不下开销（F1 的主要收益来自不读文件）。

**验证：**
```
.venv/Scripts/python.exe -m pytest tests/test_tool.py -k grep -v
.venv/Scripts/python.exe -c "
import asyncio,sys,time; sys.path.insert(0,'src')
from potatocode.tool.grep_tool import GrepTool
t=time.time(); r=asyncio.run(GrepTool().execute('{\"pattern\": \"import asyncio\", \"glob\": \"*.py\"}'))
ls=[l for l in r.content.splitlines() if l!='[truncated]']
print(f'{time.time()-t:.2f}s  返回{len(ls)}条, .venv 占', sum(1 for l in ls if l.startswith('.venv')))
"
```
期望：既有 grep 测试仍通过；输出 `.venv 占 0`（改动前为 73）、耗时明显低于改动前。

## T5: 重写 _iter_lines 的切分循环

**文件：** `src/potatocode/tool/grep_tool.py`
**依赖：** T4（同一文件，避免并发编辑）

**步骤：**

1. 只改 `_iter_lines` 函数体中的切分循环。**下面两处分支逐字保留不动**（它们就是超长行语义的实现）：
   - 超长行守卫：`if "\n" not in buf and len(buf) > max_line_chars:` 那一支
   - 文件末尾收尾：`if not chunk:` 里的那一支（含它产出的 `too_long` 用法）
2. 把
   ```python
   while "\n" in buf:
       line, buf = buf.split("\n", 1)
       lineno += 1
       too_long = len(line) > max_line_chars
       yield lineno, line[:max_line_chars] + ("…" if too_long else ""), too_long
   ```
   换成
   ```python
   parts = buf.split("\n")   # 每块只切一次（N: 消除逐行重扫）
   buf = parts.pop()         # 尾段是未完成的行，留给下一块拼接
   for line in parts:
       lineno += 1
       too_long = len(line) > max_line_chars
       yield lineno, line[:max_line_chars] + ("…" if too_long else ""), too_long
   ```
3. 更新 docstring：补一句"每块只做一次切分，未完成的行尾留到下一块拼接（ch09 F4）"。
4. 函数签名与产出契约**不变**：`(行号, 行文本, 是否超长未完整)`。

**验证：** 用新实现与改动前的旧实现跑同一份真实语料，断言行号序列与命中集合逐条一致：
```
.venv/Scripts/python.exe -c "
import re,time,sys; sys.path.insert(0,'src')
from pathlib import Path
from potatocode.tool.grep_tool import _iter_lines, _MAX_LINE_CHARS
rx=re.compile('import asyncio'); files=[p for p in Path('.').rglob('*.py') if p.is_file()]
t=time.time(); n=0; h=0
for p in files:
    try:
        with p.open('r',encoding='utf-8',errors='replace') as f:
            for _, line, _ in _iter_lines(f,_MAX_LINE_CHARS):
                n+=1
                if rx.search(line): h+=1
    except OSError: pass
print(f'改后: {time.time()-t:.2f}s  {n} 行  {h} 命中')
print('基准(旧实现): 2.01~2.21s  1148661 行  95 命中')
"
```
期望：行数 `1148661`、命中 `95`（与旧实现完全相同）；耗时约 0.85s（旧实现约 2.1s，比值 ≈ 0.4）。

## T6: glob / grep 排除行为的集成测试

**文件：** `tests/test_tool.py`
**依赖：** T3、T4

**步骤：** 在既有 `# ────────── glob ──────────` 与 `# ────────── grep ──────────` 分区内各追加测试：

1. `test_glob_excludes_noise_dirs` —— 临时目录内建 `src/a.py`、`.venv/lib/x.py`、`__pycache__/y.pyc`、`node_modules/z.js`，跑 `**/*.py`：断言 `src/a.py` 在结果中，且结果**不含**任何 `.venv` / `__pycache__` / `node_modules` 路径
2. `test_glob_escape_hatch` —— 同一临时目录，`path` 指向 `.venv`，跑 `**/*.py`：断言能搜到 `.venv` 内的文件（F2 逃生口）
3. `test_grep_excludes_noise_dirs` —— 临时目录内建 `a.py`（含关键字）、`.venv/b.py`（含同关键字）、`node_modules/c.py`（含同关键字），跑该关键字：断言只命中 `a.py`
4. `test_grep_escape_hatch` —— 同上，`path` 指向 `.venv`：断言能命中 `.venv/b.py`
5. `test_grep_glob_param_combines_with_exclude` —— 临时目录内建 `a.py` / `a.txt`（均含关键字）与 `.venv/b.txt`（含关键字），传 `glob="*.txt"`：断言只命中 `a.txt`（排除与文件名过滤**叠加**生效，F3）

**验证：**
```
.venv/Scripts/python.exe -m pytest tests/test_tool.py -v
```
期望：新增 5 个测试全通过，既有测试无回归。

## T7: _iter_lines 回归测试

**文件：** `tests/test_tool.py`
**依赖：** T5

**步骤：** 新增 import 与测试。**当前 `_iter_lines` 的超长行语义完全没有测试覆盖**，本次改写正好动这段代码，这是新增覆盖。

1. 导入：`from potatocode.tool.grep_tool import GrepTool, _MAX_LINE_CHARS, _iter_lines`
2. `test_iter_lines_ultra_long_line` —— 构造"短行 + 超长行（长度 > `_MAX_LINE_CHARS`）+ 短行"的文件，逐行收集 `_iter_lines` 产出：断言三行的行号连续为 1/2/3；超长行的 `too_long` 为 `True` 且文本以 `…` 结尾、长度不超过 `_MAX_LINE_CHARS + 1`；两条短行内容完整、`too_long` 为 `False`
3. `test_iter_lines_long_line_without_newline_at_eof` —— 文件以无换行的超长行结尾：断言守卫分支生效（`too_long=True`、文本被截断标注），不抛异常
4. `test_iter_lines_matches_plain_iteration` —— 用 `tmp_path` 造一个含普通多行内容的文件，断言 `_iter_lines` 产出的**行号与文本**与"按行读取 + 去掉换行符"的结果逐条一致（把 T5 的等价性固化为回归）
5. `test_iter_lines_memory_bounded` —— 断言对超长行只产出截断文本（不返回整行），即同时验证 AC9 的"不把整行读入内存"

**验证：**
```
.venv/Scripts/python.exe -m pytest tests/test_tool.py -k iter_lines -v
```
期望：5 个测试全通过。

## T8: 全量回归与验收数据采集

**文件：** 无（只跑验证）
**依赖：** T1~T7 全部完成

**步骤：**

1. 全量测试：`.venv/Scripts/python.exe -m pytest` —— 期望全绿，无回归（AC11 / AC12）
2. 格式化与静态检查：`ruff format --check .`、`ruff check .` —— 期望通过、无告警（AC13）
3. 采集 AC7 / AC6 的实测数据：默认范围全量搜代码内容耗时（期望 < 1s）、显式搜全量语料耗时（期望 < 5s）、`**/*.py` 与 `**/*.txt` 的 `.venv` 占比（期望均为 0）
4. 采集 AC1~AC5 的端到端证据：在本仓库实跑两个工具，记录默认排除、逃生口、配额保真的实际输出，落盘到 `docs/ch09/evidence/`
5. 把以上实测值回填进 `checklist.md` 的对应条目

**验证：** 每条命令的实际输出与期望一致；不一致的记录差异并回到对应任务修复。

## 执行顺序

```
T1 ─┬─→ T2
    ├─→ T3 ─┐
    └─→ T4 ─┴─→ T6 ─┐
         │          │
         └─→ T5 ─→ T7 ─┴─→ T8
```

- T3 与 T4 在 T1 之后可并行（不同文件）。
- T4 → T5 必须顺序执行：同一个文件 `grep_tool.py`，避免并发编辑冲突。
- T6 依赖 T3 与 T4 都完成；T7 依赖 T5。
- T8 收口，依赖全部前置任务。
