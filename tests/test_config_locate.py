"""配置文件三级查找单测：环境变量 → 项目级 → 用户级。

实现语义是「候选逐个尝试、跳过不可用的那份」：顺序决定优先级，但任何一份候选
（不存在或内容非法）都不会中断查找，只有全部候选都不可用才报 ConfigError。
下面的用例逐条钉住这层语义。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from potatocode.config import (
    CONFIG_PATH_ENV_VARIABLE,
    ConfigError,
    config_candidate_paths,
    load_for_workspace,
)


def write_provider_config(config_path: Path, provider_name: str) -> Path:
    """在指定路径写入一份最小合法配置，返回该路径。"""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "providers:\n"
        f"  - name: {provider_name}\n"
        "    protocol: openai\n"
        "    api_key: sk-test\n"
        "    model: test-model\n",
        encoding="utf-8",
    )
    return config_path


def write_invalid_config(config_path: Path) -> Path:
    """在指定路径写入一份「能被 YAML 解析、但校验不通过」的配置（providers 为空）。"""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("providers: []\n", encoding="utf-8")
    return config_path


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """隔离用户主目录，并清掉可能干扰的环境变量。"""
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.delenv(CONFIG_PATH_ENV_VARIABLE, raising=False)
    return fake_home


# ────────── 优先级与回退 ──────────


def test_project_config_takes_priority(tmp_path, isolated_home):
    """项目级与用户级同时存在时，优先用项目级。"""
    workspace = tmp_path / "workspace"
    project_config = write_provider_config(workspace / ".potatocode" / "config.yaml", "项目级")
    write_provider_config(isolated_home / ".potatocode" / "config.yaml", "用户级")

    cfg = load_for_workspace(workspace)

    assert cfg.providers[0].name == "项目级"
    assert cfg.source_path == project_config


def test_fallback_to_user_config(tmp_path, isolated_home):
    """项目内没有配置时，回退到 ~/.potatocode/config.yaml。"""
    workspace = tmp_path / "workspace-no-config"
    workspace.mkdir()
    user_config = write_provider_config(isolated_home / ".potatocode" / "config.yaml", "用户级")

    cfg = load_for_workspace(workspace)

    assert cfg.providers[0].name == "用户级"
    assert cfg.source_path == user_config


def test_env_override_wins(tmp_path, isolated_home, monkeypatch):
    """环境变量指定的配置优先级最高。"""
    workspace = tmp_path / "workspace"
    write_provider_config(workspace / ".potatocode" / "config.yaml", "项目级")
    override_config = write_provider_config(tmp_path / "elsewhere" / "custom.yaml", "环境变量")
    monkeypatch.setenv(CONFIG_PATH_ENV_VARIABLE, str(override_config))

    cfg = load_for_workspace(workspace)

    assert cfg.providers[0].name == "环境变量"
    assert cfg.source_path == override_config


# ────────── 不可用候选被跳过（本实现的语义） ──────────


def test_env_override_missing_file_falls_back_to_project(tmp_path, isolated_home, monkeypatch):
    """环境变量指向不存在的文件时跳过它，用项目级配置（不报错）。"""
    workspace = tmp_path / "workspace"
    project_config = write_provider_config(workspace / ".potatocode" / "config.yaml", "项目级")
    monkeypatch.setenv(CONFIG_PATH_ENV_VARIABLE, str(tmp_path / "nowhere.yaml"))

    cfg = load_for_workspace(workspace)

    assert cfg.providers[0].name == "项目级"
    assert cfg.source_path == project_config


def test_env_override_missing_file_falls_back_to_user(tmp_path, isolated_home, monkeypatch):
    """环境变量指向不存在的文件、项目内也没有配置时，回退到用户级。"""
    workspace = tmp_path / "workspace-no-config"
    workspace.mkdir()
    user_config = write_provider_config(isolated_home / ".potatocode" / "config.yaml", "用户级")
    monkeypatch.setenv(CONFIG_PATH_ENV_VARIABLE, str(tmp_path / "nowhere.yaml"))

    cfg = load_for_workspace(workspace)

    assert cfg.providers[0].name == "用户级"
    assert cfg.source_path == user_config


def test_invalid_project_config_falls_back_to_user_config(
    tmp_path, isolated_home, caplog
):
    """项目级配置校验不通过时跳过它并用用户级，同时留一条 warning 便于追溯。"""
    workspace = tmp_path / "workspace"
    write_invalid_config(workspace / ".potatocode" / "config.yaml")
    user_config = write_provider_config(isolated_home / ".potatocode" / "config.yaml", "用户级")

    with caplog.at_level(logging.WARNING, logger="potatocode.config.locate"):
        cfg = load_for_workspace(workspace)

    assert cfg.providers[0].name == "用户级"
    assert cfg.source_path == user_config
    assert any("error" in record.getMessage() for record in caplog.records)


def test_blank_env_override_is_ignored(tmp_path, isolated_home, monkeypatch):
    """环境变量为空白串时视同未设置，不参与候选。"""
    workspace = tmp_path / "workspace"
    project_config = write_provider_config(workspace / ".potatocode" / "config.yaml", "项目级")
    monkeypatch.setenv(CONFIG_PATH_ENV_VARIABLE, "   ")

    cfg = load_for_workspace(workspace)

    assert cfg.providers[0].name == "项目级"
    assert cfg.source_path == project_config


# ────────── 全部不可用时的错误信息 ──────────


def test_missing_config_lists_all_candidates(tmp_path, isolated_home):
    """全部候选都不存在时，错误信息列出查过的路径与补救方式。"""
    workspace = tmp_path / "empty-workspace"
    workspace.mkdir()

    with pytest.raises(ConfigError, match="未找到配置文件") as exc_info:
        load_for_workspace(workspace)

    message = str(exc_info.value)
    assert str(workspace / ".potatocode" / "config.yaml") in message
    assert str(isolated_home / ".potatocode" / "config.yaml") in message
    assert CONFIG_PATH_ENV_VARIABLE in message


def test_missing_config_mentions_override_when_all_absent(tmp_path, isolated_home, monkeypatch):
    """设了环境变量但所有候选都不可用时，错误信息点明环境变量也试过。"""
    workspace = tmp_path / "empty-workspace"
    workspace.mkdir()
    nowhere = tmp_path / "nowhere.yaml"
    monkeypatch.setenv(CONFIG_PATH_ENV_VARIABLE, str(nowhere))

    with pytest.raises(ConfigError, match="未找到配置文件") as exc_info:
        load_for_workspace(workspace)

    message = str(exc_info.value)
    assert str(nowhere) in message  # 环境变量指向的路径也列了出来
    assert "已继续查找其余候选" in message  # 明确说明发生过回退


# ────────── 候选顺序 ──────────


def test_candidate_paths_order_without_override(tmp_path, isolated_home):
    """未设环境变量时候选顺序为：项目级 → 用户级。"""
    workspace = tmp_path / "workspace"

    candidates = config_candidate_paths(workspace)

    assert candidates == [
        workspace / ".potatocode" / "config.yaml",
        isolated_home / ".potatocode" / "config.yaml",
    ]


def test_candidate_paths_put_override_first(tmp_path, isolated_home, monkeypatch):
    """设了环境变量时它在候选列表最前面，其后仍是项目级与用户级。"""
    workspace = tmp_path / "workspace"
    override_config = tmp_path / "elsewhere" / "custom.yaml"
    monkeypatch.setenv(CONFIG_PATH_ENV_VARIABLE, str(override_config))

    candidates = config_candidate_paths(workspace)

    assert candidates == [
        override_config,
        workspace / ".potatocode" / "config.yaml",
        isolated_home / ".potatocode" / "config.yaml",
    ]


def test_candidate_paths_deduplicate_same_config(tmp_path, isolated_home):
    """工作目录就是用户主目录时，项目级与用户级是同一路径，只保留一次。"""
    candidates = config_candidate_paths(isolated_home)

    assert candidates == [isolated_home / ".potatocode" / "config.yaml"]
