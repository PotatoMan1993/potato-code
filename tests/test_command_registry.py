"""命令注册中心单测：注册、冲突检测、字典序可见清单、前缀匹配与 hidden 机制（ch12）。"""

from __future__ import annotations

import pytest

from potatocode.command import Command, Kind, Registry


def _cmd(name: str, kind: Kind = Kind.LOCAL, **kwargs) -> Command:
    """构造一条测试命令（handler 用 no-op，注册中心不调用它）。"""

    async def _noop(ui) -> None:  # noqa: ARG001
        pass

    return Command(name=name, description=f"{name} 的描述", kind=kind, handler=_noop, **kwargs)


def test_register_ok():
    """注册后主名与别名都能命中同一条命令。"""
    reg = Registry()
    cmd = _cmd("status", aliases=["st"])
    reg.register(cmd)
    assert reg.lookup("status") is cmd
    assert reg.lookup("st") is cmd
    assert reg.lookup("STATUS") is cmd  # 大小写不敏感（F4）


def test_register_duplicate_name_raises():
    """同名注册立即抛 RuntimeError，消息含具体冲突名（F2/N4）。"""
    reg = Registry()
    reg.register(_cmd("help"))
    with pytest.raises(RuntimeError) as exc_info:
        reg.register(_cmd("help"))
    assert "help" in str(exc_info.value)


def test_register_duplicate_alias_raises():
    """别名与已有主名/别名冲突立即抛 RuntimeError，消息含具体冲突别名（F2/N4）。"""
    reg = Registry()
    reg.register(_cmd("help", aliases=["h"]))
    with pytest.raises(RuntimeError) as exc_info:
        reg.register(_cmd("do", aliases=["h"]))
    assert "h" in str(exc_info.value)


def test_register_rejects_uppercase_name():
    """非全小写的名字/别名在注册期拒绝，避免静默注册命不中的命令。"""
    reg = Registry()
    with pytest.raises(ValueError):
        reg.register(_cmd("Help"))
    with pytest.raises(ValueError):
        reg.register(_cmd("ok", aliases=["OK"]))


def test_visible_sorted():
    """visible() 按 name 字典序返回，且返回的是副本（F18/N7）。"""
    reg = Registry()
    for name in ("status", "clear", "help"):
        reg.register(_cmd(name))
    visible = reg.visible()
    assert [c.name for c in visible] == ["clear", "help", "status"]
    visible.append(_cmd("hack"))
    assert [c.name for c in reg.visible()] == ["clear", "help", "status"]


def test_hidden_excluded_from_visible():
    """hidden=True 的命令不进 visible()，但 dispatcher 仍可命中（F28）。"""
    reg = Registry()
    hidden = _cmd("secret", hidden=True)
    reg.register(hidden)
    reg.register(_cmd("help"))
    assert [c.name for c in reg.visible()] == ["help"]
    assert reg.lookup("secret") is hidden


def test_prefix_match():
    """prefix_match 只按主名前缀过滤，不匹配别名与描述（F24/F25）。"""
    reg = Registry()
    reg.register(_cmd("status"))
    reg.register(_cmd("session"))
    reg.register(_cmd("clear", aliases=["cl"]))  # 别名不参与前缀匹配
    reg.register(_cmd("help"))  # 描述里含 "s" 也不该命中

    assert [c.name for c in reg.prefix_match("/s")] == ["session", "status"]
    assert [c.name for c in reg.prefix_match("S")] == ["session", "status"]  # 大小写不敏感
    assert [c.name for c in reg.prefix_match("/x")] == []
    assert len(reg.prefix_match("/")) == 4  # 仅 "/" 返回全部可见命令
    assert len(reg.prefix_match("")) == 4
