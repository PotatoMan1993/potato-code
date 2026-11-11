# 子 Agent 用户审批升级到主 TUI 的链路

> 范围：ch15 F12-③ / F13 / G6 / AC8 —— 子 Agent 工具调用走到权限第五层（Ask）时，如何把审批请求上报到主 TUI 弹窗、又把用户裁决回传子 Agent。
> 行号基于当前工作区版本，会随迭代漂移；**定位优先按 `文件:符号名`**。

---

## 一、一句话数据流

```
Agent(子) —(await 回调)→ AgentTool._make_approval_upgrader —→ Manager.upgrade_approval
        —(Queue)→ TasksMixin._consume_subagent_approvals —(APPROVING 菜单)→ _submit_approval
        —(Future.set_result)→ 回到子 Agent 的 await respond
```

**核心机制**：子 Agent 创建 `asyncio.Future(respond)`；`ApprovalRequest` 经 `Manager` 的队列交给主 TUI 的常驻消费协程；TUI 弹菜单、用户按键后 `set_result`，子 Agent 从挂起点恢复继续跑动。

---

## 二、完整流程图

```
┌──────────────────────── 子 Agent 侧（后台协程 / 前台工具内） ────────────────────────┐
│                                                                                      │
│  Agent._run_iterations → _execute_batched           agent/__init__.py:957~1018      │
│    decision = engine.check(...)                                                      │
│      ├─ ALLOW → 直接执行                                                             │
│      ├─ DENY  → 直接回灌错误结果                                                     │
│      └─ ASK ──┬─ self.dont_ask=True → 直接放行（F12-②）                              │
│               │                                        agent/__init__.py:974         │
│               └─ 建 respond Future（共享载体）          agent/__init__.py:978         │
│                    │                                                                 │
│                    ├─ self.approval_upgrader 存在（F12-③）  :982                     │
│                    │     await upgrader(ApprovalRequest(name,args,reason,respond))    │
│                    │        │                                                        │
│                    │        ▼                                                        │
│                    │   AgentTool._make_approval_upgrader(label)                      │
│                    │     agent/agent_tool.py:298~315                                 │
│                    │       · 复制出新 ApprovalRequest                                │
│                    │         reason = "[来自 SubAgent {label}] " + 原 reason          │
│                    │         respond = 原 Future（关键：同一个 Future 对象）           │
│                    │       · return await task_mgr.upgrade_approval(forwarded)        │
│                    │            │                                                    │
│                    │            ▼                                                    │
│                    │   Manager.upgrade_approval()        task/manager.py:228~242     │
│                    │       · _approval_q 未创建/已满 → (DENY_ONCE, False) 回退        │
│                    │       · 否则 _approval_q.put_nowait(req)                        │
│                    │       · outcome = await req.respond   ← ★ 子 Agent 在此暂停     │
│                    │       · return (outcome, True)                                │
│                    │                                                                 │
│                    ├─ handled=False（升级失败）→ 回退默认 emit 路径 :995~1005         │
│                    │     yield Event(approval=原始 ApprovalRequest)                  │
│                    │     outcome = await respond                                     │
│                    │     └ 但 run_to_completion 立刻自动 DENY_ONCE                    │
│                    │        agent/__init__.py:434~467（无人值守安全默认，绝不悬死）    │
│                    │                                                                 │
│                    └─ 拿到 outcome 后统一收尾 :1006~1018                             │
│                         ALLOW_FOREVER → persist_local_allow + 执行                    │
│                         ALLOW_ONCE    → 执行                                          │
│                         DENY_ONCE     → 错误结果回灌（模型自行调整）                   │
└──────────────────────────────────▲───────────────────────────────────────────────────┘
                                   │ Outcome（asyncio.Future.set_result）
                                   │
┌──────────────────────────────────┴──────────── 主 TUI 侧（主事件循环） ──────────────┐
│                                                                                      │
│  cli.py 组装根                                                                        │
│    task_mgr = task.Manager()                        cli.py:110                       │
│    AgentTool(subagent_catalog, task_mgr, ...)       cli.py:115~118                   │
│    PotatoCodeApp(..., task_mgr=task_mgr, ...)       cli.py:154                       │
│                                                                                      │
│  PotatoCodeApp.on_mount()                            tui/app.py:344~369               │
│    asyncio.create_task(_consume_task_done())                                         │
│    asyncio.create_task(_consume_subagent_approvals())   ← 独立消费协程（常驻）         │
│         │                                                                            │
│         ▼                                                                            │
│  TasksMixin._consume_subagent_approvals()             tui/tasks.py:73~101            │
│    approval_queue = task_mgr.open_approval_queue()    task/manager.py:119~124        │
│    while True:                                                                        │
│      req = await approval_queue.get()               ← 阻塞等子 Agent 的 Ask           │
│      if req.respond.done(): continue                ← 过期请求跳过                    │
│      async with self.approval_menu_lock:            ← ★ 与主 Agent 弹窗互斥           │
│        self._approval_prev_state = state_before     ← 通常是 IDLE（后台子 Agent）      │
│        self.pending = req                                                            │
│        self.approve_cursor = 0                                                       │
│        self.state = SessionState.APPROVING                                           │
│        self._refresh_streaming_view()  ─────────────────┐                            │
│        try:    await req.respond            ← 暂停等用户 │                            │
│        finally: self.pending = None                     │                            │
│                 self.state = self._approval_prev_state  │                            │
│         │                                               │                            │
│         │                                     tui/stream.py:165~172                  │
│         │                                     view.approval_block(req, cursor)        │
│         │                                     tui/view.py:82~104                     │
│         │                                       ● bash(rm -rf x)                     │
│         │                                         [来自 SubAgent X] 原因…             │
│         │                                         1. 允许本次                       │
│         │                                         2. 永久允许                       │
│         │                                         3. 拒绝本次                       │
│         │                                               │                            │
│         │   用户按键                                     │                            │
│         ▼                                               ▼                            │
│  ChatTextArea._on_key 前置拦截                 tui/app.py:88~96                       │
│    if app.state is APPROVING: app.update_approving(event.key); stop()                │
│         │                                                                            │
│         ▼                                                                            │
│  StreamMixin.update_approving(key)                    tui/stream.py:198~215           │
│    ↑↓/j/k 移光标 · 回车/1,2,3/y/n/d 提交                                              │
│         │                                                                            │
│         ▼                                                                            │
│  StreamMixin._submit_approval(index)                  tui/stream.py:217~226           │
│    self.state = self._approval_prev_state                                            │
│    req.respond.set_result(outcome_for_index(index))  ← ★ 裁决回传子 Agent            │
│                                                                                      │
│  取消路径（同样回传 DENY_ONCE）                                                        │
│    Esc → action_cancel_turn / Ctrl+C → action_cancel_or_quit                         │
│         tui/app.py:630~654 → _cancel_approval():                                    │
│           pending.respond.set_result(DENY_ONCE); state 复原; turn_cancel.set()        │
└──────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 三、关键设计点（读代码时容易漏的三处）

1. **Future 是「共享载体」，请求对象是「拷贝」**
   `agent/__init__.py:978` 创建 `respond` Future → `_make_approval_upgrader` 复制 `ApprovalRequest` 时**只改 reason、不换 Future**（`agent/agent_tool.py:307~312`）。所以升级成功时 Future 由 TUI 填；升级失败回退时用的是**原始请求对象**（reason 不带前缀）。两条路径共用一个 Future，不会重复 `set_result`。

2. **`open_approval_queue` 的懒创建是「安全开关」**
   `task/manager.py:103` 里 `_approval_q` 初始为 `None`；只有 TUI 的 `_consume_subagent_approvals` 调用 `open_approval_queue()`（`tui/tasks.py:80`）后才存在。未创建 → `upgrade_approval` 直接返回 `(DENY_ONCE, False)`（`task/manager.py:234~235`）→ 子 Agent 走回退 emit → `run_to_completion` 立即 `set_result(DENY_ONCE)`（`agent/__init__.py:463~467`）。**任何一条路径都不会悬死**。

3. **一把锁串行化两个来源的弹窗**
   `approval_menu_lock`（`tui/app.py:312`）同时被主 Agent 路径（`tui/stream.py:95`）和子 Agent 路径（`tui/tasks.py:86`）持有，同一时刻最多一个菜单。`_approval_prev_state` 记录进入前状态：主 Agent 恢复为 `STREAMING`（主循环本就暂停在工具执行中），后台子 Agent 恢复为 `IDLE`；`_submit_approval` 与 `_cancel_approval` 都负责还原它。

---

## 四、代码位置速查表

| 环节 | 位置 |
|---|---|
| 组装：Manager + AgentTool 注入 task_mgr | `cli.py:110~118`、`cli.py:154` |
| 子 Agent 构造时注入 `approval_upgrader` | `agent/agent_tool.py:281~296` |
| 升级回调包装（加「来自 SubAgent」标签） | `agent/agent_tool.py:298~315` |
| 升级回调类型定义 `ApprovalUpgrader` | `agent/permission_upgrade.py` |
| 能力面窄协议 `TaskManagerLike.upgrade_approval` | `agent/agent_tool.py:55~70` |
| Agent 构造参数 `approval_upgrader` / `dont_ask` | `agent/__init__.py:290`、`304` |
| Ask 三层分支（dontAsk / upgrader / 默认 emit） | `agent/__init__.py:957~1018`（关键 982~1005） |
| `run_to_completion` 审批自动拒绝 | `agent/__init__.py:434~467` |
| 审批队列懒创建 / 消费队列获取 | `task/manager.py:103`、`119~124` |
| 升级入口 `Manager.upgrade_approval` | `task/manager.py:228~242` |
| TUI 常驻消费协程启动 | `tui/app.py:366~369` |
| 审批菜单锁定义 | `tui/app.py:310~313` |
| 子 Agent 审批消费 + 弹窗串行 | `tui/tasks.py:73~101` |
| 主 Agent 审批消费（共用锁的对照路径） | `tui/stream.py:91~107` |
| 菜单按键分派 / 提交裁决 | `tui/stream.py:198~226` |
| 菜单渲染 `approval_block` | `tui/view.py:82~104` |
| 按键前置拦截（APPROVING 态） | `tui/app.py:88~96` |
| 取消路径（Esc / Ctrl+C → DENY_ONCE） | `tui/app.py:630~654` |
| 会话状态枚举 `APPROVING` | `tui/view.py:30` |

---

## 五、三条路径对照

| 场景 | 走向 | 结果 |
|---|---|---|
| 后台子 Agent / 前台子 Agent（TUI 已启动消费协程） | upgrader → Manager 队列 → TUI 菜单 | 用户裁决回传，子 Agent 继续 |
| 子 Agent 角色 `permissionMode: dontAsk` | 不进 Ask 弹窗 | 自动放行（F12-②） |
| 无 TUI 消费者（测试 / `run_to_completion` 被直接调用 / 队列满） | `(DENY_ONCE, False)` 回退默认路径 | 自动拒绝，绝不悬死（N3） |

---

## 六、相关文档

- `docs/architecture.md` §4.6（子 Agent 扩展）、§5.1（五层权限 + 三层升级链）、§11（TUI 层）、§15.5（权限与审批升级）
- `docs/ch15/spec.md`（F12 / F13 / G6 / AC8）
