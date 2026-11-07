"""MCP 两层配置、校验与环境变量展开测试。"""

from __future__ import annotations

from pathlib import Path

from potatocode.mcp import load_config


def _use_home(monkeypatch, home: Path) -> None:
    monkeypatch.setattr(Path, "home", lambda: home)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_missing_files_are_empty(monkeypatch, tmp_path, pkg_logs):
    home = tmp_path / "home"
    root = tmp_path / "root"
    _use_home(monkeypatch, home)

    assert load_config(str(root)).servers == {}
    assert pkg_logs.records == []


def test_two_layers_merge_and_project_fully_overrides(monkeypatch, tmp_path):
    home = tmp_path / "home"
    root = tmp_path / "root"
    _use_home(monkeypatch, home)
    _write(
        home / ".potatocode" / "mcp_servers.yaml",
        """
mcp_servers:
  user-only:
    type: stdio
    command: user-command
  shared:
    type: stdio
    command: must-not-survive
    args: [old]
""",
    )
    _write(
        root / ".potatocode" / "mcp_servers.yaml",
        """
mcp_servers:
  project-only:
    type: http
    url: https://project.example/mcp
  shared:
    type: http
    url: https://override.example/mcp
""",
    )

    servers = load_config(str(root)).servers
    assert set(servers) == {"user-only", "project-only", "shared"}
    assert servers["shared"].type == "http"
    assert servers["shared"].url == "https://override.example/mcp"
    assert servers["shared"].command == ""
    assert servers["shared"].args == []


def test_invalid_layer_warns_without_hiding_other_layer(monkeypatch, tmp_path, pkg_logs):
    home = tmp_path / "home"
    root = tmp_path / "root"
    _use_home(monkeypatch, home)
    _write(home / ".potatocode" / "mcp_servers.yaml", "mcp_servers: [broken\n")
    _write(
        root / ".potatocode" / "mcp_servers.yaml",
        "mcp_servers:\n  good:\n    type: stdio\n    command: python\n",
    )

    config = load_config(str(root))

    assert set(config.servers) == {"good"}
    warned = pkg_logs.text()
    assert "load" in warned and "failed" in warned


def test_environment_expands_only_env_and_headers(monkeypatch, tmp_path, pkg_logs):
    home = tmp_path / "home"
    root = tmp_path / "root"
    _use_home(monkeypatch, home)
    monkeypatch.setenv("KNOWN_TOKEN", "secret-from-host")
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    _write(
        root / ".potatocode" / "mcp_servers.yaml",
        """
mcp_servers:
  ${SERVER_NAME}:
    type: stdio
    command: ${KNOWN_TOKEN}
    args: ["${KNOWN_TOKEN}"]
    env:
      TOKEN: "prefix-${KNOWN_TOKEN}-${MISSING_TOKEN}-${MISSING_TOKEN}"
  remote:
    type: http
    url: "https://example.test/${KNOWN_TOKEN}"
    headers:
      Authorization: "Bearer ${KNOWN_TOKEN}"
""",
    )

    servers = load_config(str(root)).servers

    literal = servers["${SERVER_NAME}"]
    assert literal.command == "${KNOWN_TOKEN}"
    assert literal.args == ["${KNOWN_TOKEN}"]
    assert literal.env == {"TOKEN": "prefix-secret-from-host--"}
    assert servers["remote"].url == "https://example.test/${KNOWN_TOKEN}"
    assert servers["remote"].headers == {"Authorization": "Bearer secret-from-host"}
    assert pkg_logs.text().count("${MISSING_TOKEN}") == 1


def test_invalid_servers_are_skipped_independently(monkeypatch, tmp_path, pkg_logs):
    home = tmp_path / "home"
    root = tmp_path / "root"
    _use_home(monkeypatch, home)
    _write(
        root / ".potatocode" / "mcp_servers.yaml",
        """
mcp_servers:
  missing-type: {command: python}
  bad-type: {type: websocket, url: ws://example}
  missing-command: {type: stdio}
  missing-url: {type: http}
  bad-args: {type: stdio, command: python, args: nope}
  bad-env: {type: stdio, command: python, env: {PORT: 1234}}
  bad-headers: {type: http, url: https://example.test, headers: [nope]}
  good: {type: stdio, command: python, args: []}
  extra-fields-ignored: {type: http, url: https://example.test, args: nope, env: nope}
""",
    )

    config = load_config(str(root))

    assert set(config.servers) == {"good", "extra-fields-ignored"}
    assert pkg_logs.text().count("skip server") == 7


def test_invalid_document_shapes_degrade(monkeypatch, tmp_path, pkg_logs):
    home = tmp_path / "home"
    root = tmp_path / "root"
    _use_home(monkeypatch, home)
    _write(home / ".potatocode" / "mcp_servers.yaml", "- not\n- a\n- mapping\n")
    _write(root / ".potatocode" / "mcp_servers.yaml", "mcp_servers: nope\n")

    assert load_config(str(root)).servers == {}
    assert pkg_logs.text().count("must be a mapping") == 2


def test_documented_example_parses(monkeypatch, tmp_path):
    home = tmp_path / "home"
    root = tmp_path / "root"
    _use_home(monkeypatch, home)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("EXAMPLE_TOKEN", "test-token")
    example = Path(__file__).parents[1] / "docs/mcp/mcp-servers.example.yaml"
    _write(root / ".potatocode" / "mcp_servers.yaml", example.read_text(encoding="utf-8"))

    assert set(load_config(str(root)).servers) == {"github", "local-sqlite", "example-http"}
