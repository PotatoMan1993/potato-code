"""黑名单单测：高危命令命中、安全命令不误伤（AC1/F1/N1）。"""

from __future__ import annotations

import pytest

from potatocode.permission.blacklist import hits_blacklist


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf /",
        "rm -fr ~",
        "rm -rf $HOME",
        "rm -rf /*",
        "rm -rf --no-preserve-root /",
        ":(){ :|:& };:",
        "dd if=/dev/zero of=/dev/sda",
        "mkfs.ext4 /dev/sda",
        "echo x > /dev/sda",
        "chmod -R 777 /",
    ],
)
def test_hits_dangerous(cmd: str):
    """高危命令命中黑名单（AC1/F1）。"""
    assert hits_blacklist(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "rm -rf ./build",
        "rm ./tmp.txt",
        "git status",
        "ls -la",
        "pytest -q",
        "echo hello",
    ],
)
def test_misses_safe(cmd: str):
    """安全命令不误伤（AC1）。"""
    assert not hits_blacklist(cmd)
