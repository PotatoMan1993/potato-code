"""命令注册中心：注册期冲突检测、按名/别名查找、可见清单与前缀匹配（ch12 F1/F2/F28）。"""

from __future__ import annotations

from .command import Command


class Registry:
    """内置命令注册中心。

    `_by_name` 把主名与别名都映射到同一 Command（dispatcher 的查找表）；
    `_visible` 按 name 字典序维护非 hidden 命令（/help 与补全菜单的数据源）。
    """

    def __init__(self) -> None:
        self._by_name: dict[str, Command] = {}
        self._visible: list[Command] = []

    def register(self, cmd: Command) -> None:
        """登记一条命令；名字/别名冲突立即抛 RuntimeError（F2，启动期报错不进运行时）。

        非法形态（空名、含大写）同样拒绝注册，避免静默注册一条永远命不中的命令。
        """
        if not cmd.name or cmd.name != cmd.name.lower():
            raise ValueError(f"命令名必须非空且全小写: {cmd.name!r}")
        for alias in cmd.aliases:
            if not alias or alias != alias.lower():
                raise ValueError(f"命令别名必须非空且全小写: {alias!r}")
        for key in (cmd.name, *cmd.aliases):
            if key in self._by_name:
                raise RuntimeError(f"command conflict: {key}")
        for key in (cmd.name, *cmd.aliases):
            self._by_name[key] = cmd
        if not cmd.hidden:
            self._visible.append(cmd)
            self._visible.sort(key=lambda c: c.name)

    def lookup(self, name: str) -> Command | None:
        """按名字查找命令（主名与别名同表），大小写不敏感；未命中返回 None（F4）。"""
        return self._by_name.get(name.strip().lower())

    def visible(self) -> list[Command]:
        """按 name 字典序返回可见命令的副本（hidden 不含，F18/F28）。"""
        return list(self._visible)

    def prefix_match(self, prefix: str) -> list[Command]:
        """按前缀过滤可见命令（补全菜单数据源，F24/F25）。

        只匹配主名，不匹配别名与描述；`prefix` 为空（仅 "/"）时返回全部可见命令。
        """
        p = prefix.strip().lstrip("/").lower()
        return [cmd for cmd in self._visible if cmd.name.startswith(p)]
