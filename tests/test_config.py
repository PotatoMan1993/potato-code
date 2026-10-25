"""config 模块单测：加载、校验与错误路径。"""

from __future__ import annotations

from pathlib import Path

import pytest

from potatocode.config import ConfigError, ProviderConfig, effective_context_window, load

# 合法配置样例（两条 provider，覆盖各字段）
VALID_YAML = """
providers:
  - name: Claude
    protocol: anthropic
    api_key: sk-ant-test
    model: claude-sonnet-4-5
    thinking: true
  - name: GPT
    protocol: openai
    api_key: sk-test
    model: gpt-4o
    base_url: https://example.com/v1
"""


def write_config(tmp_path, content: str) -> str:
    """把配置内容写入临时文件，返回路径。"""
    p = tmp_path / "config.yaml"
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_load_valid_config(tmp_path):
    """合法配置应解析出正确条数与字段。"""
    cfg = load(write_config(tmp_path, VALID_YAML))
    assert len(cfg.providers) == 2

    a = cfg.providers[0]
    assert a.name == "Claude"
    assert a.protocol == "anthropic"
    assert a.api_key == "sk-ant-test"
    assert a.model == "claude-sonnet-4-5"
    assert a.base_url is None  # 未配置时为 None
    assert a.thinking is True

    b = cfg.providers[1]
    assert b.protocol == "openai"
    assert b.base_url == "https://example.com/v1"
    assert b.thinking is False  # 默认 False


def test_load_missing_file(tmp_path):
    """文件不存在应抛 ConfigError 且信息含路径。"""
    with pytest.raises(ConfigError, match="配置文件不存在"):
        load(str(tmp_path / "nope.yaml"))


def test_empty_providers(tmp_path):
    """providers 为空列表应报错。"""
    with pytest.raises(ConfigError, match="providers 不能为空"):
        load(write_config(tmp_path, "providers: []\n"))


def test_missing_providers_key(tmp_path):
    """缺少 providers 键应报错。"""
    with pytest.raises(ConfigError, match="providers 不能为空"):
        load(write_config(tmp_path, "foo: bar\n"))


@pytest.mark.parametrize("field", ["name", "protocol", "api_key", "model"])
def test_missing_required_field(tmp_path, field):
    """缺少任一必填字段应报错，且信息指明位置与字段名。"""
    entry = {
        "name": "X",
        "protocol": "anthropic",
        "api_key": "k",
        "model": "m",
    }
    del entry[field]
    body = "\n    ".join(f"{k}: {v}" for k, v in entry.items())
    yaml_text = f"providers:\n  - {body}\n"
    with pytest.raises(ConfigError, match=rf"providers\[0\]\.{field} 不能为空"):
        load(write_config(tmp_path, yaml_text))


def test_invalid_protocol(tmp_path):
    """非法 protocol 应报错并列出合法取值。"""
    yaml_text = """
providers:
  - name: X
    protocol: gemini
    api_key: k
    model: m
"""
    with pytest.raises(ConfigError, match="protocol 非法"):
        load(write_config(tmp_path, yaml_text))


def test_invalid_yaml_syntax(tmp_path):
    """YAML 语法错误应转为可读的 ConfigError。"""
    with pytest.raises(ConfigError, match="YAML 格式错误"):
        load(write_config(tmp_path, "providers: [unclosed\n"))


def test_invalid_thinking_type(tmp_path):
    """thinking 非布尔值应报错。"""
    yaml_text = """
providers:
  - name: X
    protocol: anthropic
    api_key: k
    model: m
    thinking: "yes"
"""
    with pytest.raises(ConfigError, match="thinking 应为布尔值"):
        load(write_config(tmp_path, yaml_text))


def test_second_provider_error_index(tmp_path):
    """错误信息应指明出错的是第几个 provider。"""
    yaml_text = """
providers:
  - name: A
    protocol: anthropic
    api_key: k
    model: m
  - name: B
    protocol: openai
    model: m
"""
    with pytest.raises(ConfigError, match=r"providers\[1\]\.api_key 不能为空"):
        load(write_config(tmp_path, yaml_text))


# ────────── context_window 与协议默认值 ──────────

CONTEXT_YAML = """
providers:
  - name: X
    protocol: anthropic
    api_key: k
    model: m
    context_window: 80000
"""


def test_effective_context_window_unconfigured():
    """anthropic 未配置 → 200000。"""
    p = ProviderConfig(name="x", protocol="anthropic", api_key="k", model="m")
    assert p.context_window == 0
    assert effective_context_window(p) == 200000


def test_effective_context_window_zero():
    """openai 显式配置 0 → 视为未配置，取 128000。"""
    p = ProviderConfig(name="x", protocol="openai", api_key="k", model="m", context_window=0)
    assert effective_context_window(p) == 128000


def test_effective_context_window_positive():
    """配置为正数时优先于协议默认值。"""
    p = ProviderConfig(name="x", protocol="anthropic", api_key="k", model="m", context_window=80000)
    assert effective_context_window(p) == 80000


def test_effective_context_window_unknown_protocol():
    """未知协议按保守默认 200000（协议字段已校验，此处用构造绕过）。"""
    p = ProviderConfig.__new__(ProviderConfig)
    p.context_window = 0
    p.protocol = "gemini"  # type: ignore[assignment]
    assert effective_context_window(p) == 200000


def test_load_context_window_from_yaml(tmp_path):
    """YAML 中的 context_window 被正确解码。"""
    cfg = load(write_config(tmp_path, CONTEXT_YAML))
    assert cfg.providers[0].context_window == 80000
    assert effective_context_window(cfg.providers[0]) == 80000


def test_load_invalid_context_window(tmp_path):
    """context_window 非整数 / 负数时报可读错误。"""
    with pytest.raises(ConfigError, match=r"providers\[0\]\.context_window 应为整数"):
        load(write_config(tmp_path, CONTEXT_YAML.replace("80000", '"80000"')))

    with pytest.raises(ConfigError, match=r"providers\[0\]\.context_window 不能为负数"):
        load(write_config(tmp_path, CONTEXT_YAML.replace("80000", "-1")))


def test_example_config_is_parseable():
    """配置示例文件可被解析，且展示的 context_window 字段生效。"""
    import yaml

    from potatocode.config.config import Config, _from_dict

    example = Path(".potatocode/config.yaml.example")
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    cfg = _from_dict(raw, str(example))

    assert isinstance(cfg, Config)
    first = cfg.providers[0]
    assert first.context_window == 200000
    assert effective_context_window(first) == 200000


def test_enable_subagent_background_default(tmp_path):
    """未配置 enable_subagent_background：字段为 None，effective 视为 True（ch15 N6）。"""
    cfg = load(write_config(tmp_path, VALID_YAML))
    assert cfg.enable_subagent_background is None
    assert cfg.effective_enable_subagent_background() is True


def test_enable_subagent_background_false(tmp_path):
    """显式 false：effective 返回 False，后台/Fork 全部强制前台（ch15 N6/AC18）。"""
    cfg = load(
        write_config(
            tmp_path,
            VALID_YAML + "\nenable_subagent_background: false\n",
        )
    )
    assert cfg.enable_subagent_background is False
    assert cfg.effective_enable_subagent_background() is False


def test_enable_subagent_background_true(tmp_path):
    cfg = load(
        write_config(tmp_path, VALID_YAML + "\nenable_subagent_background: true\n")
    )
    assert cfg.effective_enable_subagent_background() is True


def test_enable_subagent_background_invalid_type(tmp_path):
    """非布尔值应报 ConfigError。"""
    with pytest.raises(ConfigError, match="enable_subagent_background"):
        load(write_config(tmp_path, VALID_YAML + "\nenable_subagent_background: yes-or-no\n"))
