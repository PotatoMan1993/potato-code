"""`/hooks` 命令 handler：输出已加载 hook 列表与加载来源（ch14 F34/F35）。

命令逻辑只依赖 UI Protocol 的 hook 查询方法，不持有 Textual 类型（ch12 G3）。
"""

from __future__ import annotations

from ..command.ui import UI


def _format_flags(rule) -> str:
    """拼接执行控制标志：[once] / [async]（ch14 F34）。"""
    flags: list[str] = []
    if rule.only_once:
        flags.append("[once]")
    if rule.asyncio_mode:
        flags.append("[async]")
    return " ".join(flags)


def _rule_line(rule) -> str:
    """单条 hook 的展示行：`  <name>  <event>  <action.type>  <flags>`（ch14 F34）。"""
    parts = [f"  {rule.name}  {rule.event.value}  {rule.action.type.value}"]
    flags = _format_flags(rule)
    if flags:
        parts.append(flags)
    return "  ".join(parts)


async def handle_hooks(ui: UI) -> None:
    """/hooks：按 event 分组列出全部已加载 hook，末尾显示加载来源（ch14 F34/F35）。"""
    rules = ui.hook_rules()
    if not rules:
        ui.println("No hooks loaded.")
        return
    # 按 event 分组、组内保持 yaml 声明顺序（F34/N：加载层不引入显式优先级）
    grouped: dict[str, list] = {}
    for rule in rules:
        grouped.setdefault(rule.event.value, []).append(rule)
    lines: list[str] = []
    for event_name in grouped:  # dict 保持首次出现顺序
        for rule in grouped[event_name]:
            lines.append(_rule_line(rule))
    sources = ui.hook_sources()
    lines.append(f"Loaded from: {', '.join(sources) if sources else '(none)'}")
    ui.println("\n".join(lines))


__all__ = ["handle_hooks"]
