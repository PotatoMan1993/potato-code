"""config 模块单测：加载、校验与错误路径。"""

from __future__ import annotations

import pytest

from potatocode.config import ConfigError, load
from potatocode.llm import ContextLimits

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


def test_context_defaults_preserve_unknown_window(tmp_path):
    cfg = load(write_config(tmp_path, VALID_YAML)).providers[0]
    assert cfg.context_window is None
    assert cfg.max_output_tokens == 4096
    assert cfg.compact_max_output_tokens == 20000
    assert cfg.openai_output_limit_parameter == "max_completion_tokens"
    with pytest.raises(ValueError, match="context_window"):
        ContextLimits(cfg.context_window).validate()


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("context_window", "true", "context_window"),
        ("context_window", "33000", "context_window"),
        ("context_window", '"128000"', "context_window"),
        ("max_output_tokens", "false", "max_output_tokens"),
        ("max_output_tokens", "128000", "max_output_tokens"),
        ("max_output_tokens", "0", "max_output_tokens"),
        ("compact_max_output_tokens", "20001", "compact_max_output_tokens"),
        ("compact_max_output_tokens", "-1", "compact_max_output_tokens"),
    ],
)
def test_invalid_budget_is_reported_before_request(tmp_path, field, value, message):
    fields = {"context_window": "128000", field: value}
    yaml_text = "providers:\n  - name: X\n    protocol: openai\n    api_key: fake\n    model: m\n"
    yaml_text += "".join(f"    {key}: {item}\n" for key, item in fields.items())
    cfg = load(write_config(tmp_path, yaml_text)).providers[0]
    limits = ContextLimits(cfg.context_window, cfg.max_output_tokens, cfg.compact_max_output_tokens)
    with pytest.raises(ValueError, match=message):
        limits.validate()


def test_explicit_context_configuration(tmp_path):
    yaml_text = """providers:
  - name: X
    protocol: openai
    api_key: fake
    model: m
    context_window: 128000
    max_output_tokens: 1024
    compact_max_output_tokens: 6000
    openai_output_limit_parameter: max_tokens
"""
    cfg = load(write_config(tmp_path, yaml_text)).providers[0]
    ContextLimits(
        cfg.context_window, cfg.max_output_tokens, cfg.compact_max_output_tokens
    ).validate()
    assert cfg.openai_output_limit_parameter == "max_tokens"
    with pytest.raises(ConfigError, match="openai_output_limit_parameter"):
        load(
            write_config(tmp_path, yaml_text.replace("parameter: max_tokens", "parameter: invalid"))
        )
