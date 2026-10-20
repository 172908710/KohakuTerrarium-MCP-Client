"""Runtime, connection validation, and readiness tests."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kt_mcp_client.config import ConfigError, DeviceProfile
from kt_mcp_client.kt_runtime import (
    KTRuntime,
    KTRuntimeError,
    ensure_tools_config,
    render_tools_yaml,
)
from kt_mcp_client.ssh_tunnel import SSHTunnelError, build_reverse_command

TOKEN = "S" * 43
ACTUAL_TOKEN = "T" * 43


def make_profile(tmp_path: Path, **overrides) -> DeviceProfile:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "kt-home"
    home.mkdir()
    identity = tmp_path / "id_ed25519"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("test key placeholder", encoding="utf-8")
    known_hosts.write_text("example.invalid ssh-ed25519 AAAA", encoding="utf-8")
    values = dict(
        display_name="staging",
        workspace_path=str(workspace),
        kt_python=sys.executable,
        kt_home=str(home),
        public_origin="https://staging.example.invalid",
        ssh_host="vps.example.invalid",
        ssh_port=2222,
        ssh_user="kt-tunnel",
        ssh_identity_file=str(identity),
        ssh_known_hosts=str(known_hosts),
        ssh_extra_options=["Compression=no"],
        secret=TOKEN,
    )
    values.update(overrides)
    return DeviceProfile(**values)


def test_ssh_reverse_command_has_required_security_options(tmp_path):
    value = make_profile(tmp_path)
    command = build_reverse_command(value)
    assert command[0] == "ssh"
    assert command[1:4] == ["-N", "-T", "-R"]
    assert "127.0.0.1:18766:127.0.0.1:8865" in command
    assert "-p" in command and "2222" in command
    assert "ExitOnForwardFailure=yes" in command
    assert "StrictHostKeyChecking=yes" in command
    assert "Compression=no" in command
    assert value.secret not in "\x00".join(command)
    assert command[-1] == "kt-tunnel@vps.example.invalid"


def test_low_level_ssh_rejects_production_ports(tmp_path):
    value = make_profile(tmp_path, local_port=8765)
    with pytest.raises(SSHTunnelError, match="生产端口"):
        build_reverse_command(value)
    other = tmp_path / "other"
    other.mkdir()
    value = make_profile(other, remote_forward_port=18765)
    with pytest.raises(SSHTunnelError, match="生产端口"):
        build_reverse_command(value)


def test_ssh_requires_existing_identity_and_known_hosts(tmp_path):
    value = make_profile(tmp_path, ssh_identity_file=str(tmp_path / "missing"))
    with pytest.raises(SSHTunnelError):
        build_reverse_command(value)


def test_ssh_extra_options_cannot_override_security(tmp_path):
    value = make_profile(tmp_path, ssh_extra_options=["ProxyCommand=bad"])
    with pytest.raises(SSHTunnelError):
        build_reverse_command(value)


def test_default_kt_home_is_canonicalized_inside_config_root(tmp_path):
    value = make_profile(tmp_path, kt_home="")
    config = tmp_path / "config"
    runtime = KTRuntime(value, config / "runtime", config_root=config)
    assert runtime.kt_home == (config / "kt-home").resolve()
    assert Path(value.kt_home) == runtime.kt_home
    assert runtime.kt_home != config.resolve()
    assert runtime.runtime_dir == (config / "runtime").resolve()


def test_default_kt_home_never_uses_legacy_bridge_path(tmp_path):
    value = make_profile(tmp_path, kt_home="")
    with pytest.raises(ConfigError):
        KTRuntime(
            value,
            "C:/Users/Administrator/AppData/Local/KT-MCP-Bridge/env/runtime",
            config_root="C:/Users/Administrator/AppData/Local/KT-MCP-Bridge",
        )


def test_real_kt_cli_commands_are_independent_and_no_secret(tmp_path):
    value = make_profile(tmp_path)
    runtime = KTRuntime(value, tmp_path / "config" / "runtime", config_root=tmp_path / "config")
    setup = runtime.setup_command()
    start = runtime.start_command()
    stop = runtime.stop_command()
    status = runtime.status_command()
    assert setup[setup.index("setup") + 1 : setup.index("--home-dir")] == [
        "--non-interactive", "--mode", "external", "--origin", "https://staging.example.invalid",
        "--port", "8865", "--json", "--config", str(tmp_path / "config" / "tools.yaml"),
    ]
    assert start[start.index("start") + 1 : start.index("--home-dir")] == ["--wait", "30", "--json"]
    for command in (setup, start, stop, status):
        assert value.secret not in "\x00".join(command)
        assert str(tmp_path / "kt-home") in command


def test_tools_yaml_is_independent_and_workspace_specific(tmp_path):
    value = make_profile(tmp_path)
    target = ensure_tools_config(tmp_path / "config", value.workspace_path, profile=value)
    assert target == (tmp_path / "config" / "tools.yaml").resolve()
    rendered = target.read_text(encoding="utf-8")
    assert rendered == render_tools_yaml(value.workspace_path)
    assert "workspace:" not in rendered
    assert "name: read" in rendered and "type: builtin" in rendered and "config: {}" in rendered
    assert all(f"name: {name}" in rendered for name in (
        "read", "write", "edit", "multi_edit", "glob", "grep", "tree", "bash", "python",
    ))
    assert "pwd_guard: block" in rendered
    assert "name: default" not in rendered


def test_workspace_registry_registers_all_configured_entries(tmp_path, monkeypatch):
    first = tmp_path / "workspace"
    second = tmp_path / "second-workspace"
    second.mkdir()
    value = make_profile(
        tmp_path,
        workspaces=[
            {"workspace_id": "localpc", "path": str(first)},
            {"workspace_id": "second", "path": str(second)},
        ],
    )
    config = tmp_path / "config"
    config.mkdir()
    ensure_tools_config(config, value.workspace_path, profile=value)
    _connection(Path(value.kt_home), config, first)
    runtime = KTRuntime(value, config / "runtime", config_root=config)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        action = command[command.index("mcp-serve") + 1]
        if action == "workspace" and command[command.index("workspace") + 1] == "list":
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"workspaces": {"localpc": str(first)}}), ""
            )
        if action == "workspace" and command[command.index("workspace") + 1] == "add":
            return subprocess.CompletedProcess(command, 0, "{}", "")
        raise AssertionError(command)

    monkeypatch.setattr(runtime, "_run_command", fake_run)
    assert runtime.ensure_workspace_registered()
    add_commands = [
        command for command in calls
        if command[command.index("mcp-serve") + 1 : command.index("--home-dir")]
        and command[command.index("mcp-serve") + 2] == "add"
    ]
    assert len(add_commands) == 1
    assert add_commands[0][add_commands[0].index("add") + 1 : add_commands[0].index("--home-dir")] == [
        "second", str(second.resolve()),
    ]


def test_ensure_tools_config_cleans_temporary_file_on_replace_failure(tmp_path, monkeypatch):
    value = make_profile(tmp_path)
    config = tmp_path / "config"
    original_replace = __import__("os").replace

    def fail_replace(source, target):
        assert Path(source).parent == config
        raise OSError("simulated replace failure")

    monkeypatch.setattr("kt_mcp_client.kt_runtime.os.replace", fail_replace)
    with pytest.raises(OSError, match="replace failure"):
        ensure_tools_config(config, value.workspace_path, profile=value)
    assert not list(config.glob(".tools.yaml.*.tmp"))
    assert not (config / "tools.yaml").exists()
    assert original_replace is not None


def _connection(home: Path, config_root: Path, workspace: Path, **updates: object) -> Path:
    endpoint = home / "mcp-serve" / "endpoint"
    endpoint.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "home_dir": str(home),
        "public_origin": "https://staging.example.invalid",
        "port": 8865,
        "tunnel": "external",
        "tools_config": str(config_root / "tools.yaml"),
        "endpoint_url": f"https://staging.example.invalid/mcp/{'S' * 43}",
    }
    updates = dict(updates)
    if "endpoint_url" not in updates:
        payload.pop("endpoint_url", None)
    elif updates.get("endpoint_url") is None:
        payload.pop("endpoint_url", None)
    if updates.get("tools_config", object()) is None:
        updates.pop("tools_config")
    payload.update(updates)
    (endpoint / "connection.json").write_text(json.dumps(payload), encoding="utf-8")
    return endpoint / "connection.json"


def _configured_runtime(tmp_path: Path, **updates: object) -> KTRuntime:
    value = make_profile(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    ensure_tools_config(config, value.workspace_path, profile=value)
    _connection(Path(value.kt_home), config, Path(value.workspace_path), **updates)
    return KTRuntime(value, config / "runtime", config_root=config)


def test_connection_secret_must_match_complete_url(tmp_path):
    value = make_profile(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    ensure_tools_config(config, value.workspace_path, profile=value)
    _connection(Path(value.kt_home), config, Path(value.workspace_path), secret=ACTUAL_TOKEN, url=f"https://staging.example.invalid/mcp/{TOKEN}")
    runtime = KTRuntime(value, config / "runtime", config_root=config)
    with pytest.raises(KTRuntimeError, match="完整 URL 不一致"):
        runtime._validate_connection()


def test_connection_secret_only_record_is_accepted(tmp_path):
    runtime = _configured_runtime(tmp_path, secret=TOKEN)
    payload = json.loads(runtime.connection_path.read_text(encoding="utf-8"))
    assert payload["secret"] == TOKEN
    assert "endpoint_url" not in payload
    assert runtime._validate_connection() == payload
    # A persisted secret is not proof that this process fetched the current
    # endpoint URL; url() performs that authenticated verification separately.
    assert not runtime.profile.endpoint_verified


def test_connection_origin_host_case_is_compatible(tmp_path):
    value = make_profile(tmp_path, public_origin="https://STAGING.EXAMPLE.INVALID")
    config = tmp_path / "config"
    config.mkdir()
    ensure_tools_config(config, value.workspace_path, profile=value)
    _connection(Path(value.kt_home), config, Path(value.workspace_path), secret=TOKEN)
    runtime = KTRuntime(value, config / "runtime", config_root=config)
    assert runtime._validate_connection()["public_origin"] == "https://staging.example.invalid"


def test_connection_secret_only_must_match_profile(tmp_path):
    value = make_profile(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    ensure_tools_config(config, value.workspace_path, profile=value)
    _connection(
        Path(value.kt_home), config, Path(value.workspace_path),
        secret=ACTUAL_TOKEN,
    )
    runtime = KTRuntime(value, config / "runtime", config_root=config)
    with pytest.raises(KTRuntimeError, match="profile 不匹配"):
        runtime._validate_connection()

def test_existing_connection_identity_mismatch_is_error(tmp_path):
    value = make_profile(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    ensure_tools_config(config, value.workspace_path, profile=value)
    _connection(Path(value.kt_home), config, Path(value.workspace_path), port=9999)
    runtime = KTRuntime(value, config / "runtime", config_root=config)
    with pytest.raises(KTRuntimeError, match="port"):
        runtime.setup()


def test_connection_config_object_is_not_treated_as_tools_path(tmp_path):
    runtime = _configured_runtime(tmp_path, tools_config=None, config={"name": "production-like"})
    assert runtime._validate_connection()["config"] == {"name": "production-like"}


def test_connection_without_tools_marker_uses_valid_independent_file(tmp_path):
    runtime = _configured_runtime(tmp_path, tools_config=None)
    payload = json.loads(runtime.connection_path.read_text(encoding="utf-8"))
    payload.pop("tools_config")
    runtime.connection_path.write_text(json.dumps(payload), encoding="utf-8")
    assert runtime._validate_connection()["version"] == 1


def test_connection_tools_marker_must_match_independent_file(tmp_path):
    runtime = _configured_runtime(tmp_path, tools_config=str(tmp_path / "other-tools.yaml"))
    with pytest.raises(KTRuntimeError, match="tools_config"):
        runtime._validate_connection()


def test_url_reads_actual_kt_url_and_replaces_legacy_secret(tmp_path, monkeypatch):
    value = make_profile(tmp_path, secret=TOKEN)
    config = tmp_path / "config"
    config.mkdir()
    ensure_tools_config(config, value.workspace_path, profile=value)
    _connection(Path(value.kt_home), config, Path(value.workspace_path))
    runtime = KTRuntime(value, config / "runtime", config_root=config)

    def fake_run(command, **kwargs):
        assert command[command.index("mcp-serve") + 1] == "url"
        return subprocess.CompletedProcess(command, 0, f"https://staging.example.invalid/mcp/{ACTUAL_TOKEN}\n", "")

    monkeypatch.setattr("kt_mcp_client.kt_runtime.subprocess.run", fake_run)
    assert runtime.url() == f"https://staging.example.invalid/mcp/{ACTUAL_TOKEN}"
    assert value.secret == ACTUAL_TOKEN
    assert value.endpoint_verified


def test_url_rejects_invalid_actual_endpoint(tmp_path, monkeypatch):
    value = make_profile(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    runtime = KTRuntime(value, config / "runtime", config_root=config)

    monkeypatch.setattr(
        "kt_mcp_client.kt_runtime.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "http://bad/mcp/short\n", ""),
    )
    with pytest.raises(KTRuntimeError, match="endpoint"):
        runtime.url()


def test_kt_python_is_required(tmp_path):
    value = make_profile(tmp_path, kt_python="")
    runtime = KTRuntime(value, tmp_path / "config" / "runtime", config_root=tmp_path / "config")
    with pytest.raises(KTRuntimeError, match="kt_python"):
        runtime.start_command()


def test_start_requires_ready_json_flags_not_returncode(tmp_path, monkeypatch):
    runtime = _configured_runtime(tmp_path)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        action = command[command.index("mcp-serve") + 1]
        if action == "workspace":
            return subprocess.CompletedProcess(command, 0, json.dumps({"workspaces": {"default": str(runtime.profile.workspace_path)}}), "")
        if action == "start":
            return subprocess.CompletedProcess(command, 0, json.dumps({"state": "starting", "public_ready": False, "local_ready": True}), "")
        if action == "url":
            return subprocess.CompletedProcess(command, 0, f"https://staging.example.invalid/mcp/{ACTUAL_TOKEN}\n", "")
        raise AssertionError(action)

    monkeypatch.setattr("kt_mcp_client.kt_runtime.subprocess.run", fake_run)
    result = runtime.start()
    assert result.cli_status == "degraded"
    assert not result.cli_running
    assert result.local_ready
    assert not result.public_ready
    assert "state='starting'" in result.detail
    assert any(command[command.index("mcp-serve") + 1] == "start" for command in calls)
    assert not any(command[command.index("mcp-serve") + 1] == "url" for command in calls)
    assert not runtime.profile.endpoint_verified




def test_status_unknown_state_is_not_local_ready(tmp_path, monkeypatch):
    runtime = _configured_runtime(tmp_path)

    def fake_run(command, **kwargs):
        action = command[command.index("mcp-serve") + 1]
        if action == "status":
            return subprocess.CompletedProcess(
                command, 0,
                json.dumps({"state": "mystery", "public_ready": False, "local_ready": True}), "",
            )
        raise AssertionError(action)

    monkeypatch.setattr("kt_mcp_client.kt_runtime.subprocess.run", fake_run)
    result = runtime.status()
    assert result.cli_status == "unknown"
    assert not result.local_ready


def test_start_rejects_terminal_state_even_with_zero_exit_and_local_ready(tmp_path, monkeypatch):
    runtime = _configured_runtime(tmp_path)

    def fake_run(command, **kwargs):
        action = command[command.index("mcp-serve") + 1]
        if action == "workspace":
            return subprocess.CompletedProcess(
                command, 0,
                json.dumps({"workspaces": {"default": str(runtime.profile.workspace_path)}}), "",
            )
        if action == "start":
            return subprocess.CompletedProcess(
                command, 0,
                json.dumps({"state": "failed", "public_ready": False, "local_ready": True}), "",
            )
        raise AssertionError(action)

    monkeypatch.setattr("kt_mcp_client.kt_runtime.subprocess.run", fake_run)
    with pytest.raises(KTRuntimeError, match="不可用状态"):
        runtime.start()


def test_start_nonzero_degraded_json_is_returned_when_local_ready(tmp_path, monkeypatch):
    runtime = _configured_runtime(tmp_path, secret=TOKEN)
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        action = command[command.index("mcp-serve") + 1]
        if action == "workspace":
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"workspaces": {"default": str(runtime.profile.workspace_path)}}),
                "",
            )
        if action == "start":
            return subprocess.CompletedProcess(
                command,
                1,
                json.dumps(
                    {
                        "state": "degraded",
                        "public_ready": False,
                        "local_ready": True,
                        "error": "public probe failed",
                    }
                ),
                "",
            )
        raise AssertionError(action)

    monkeypatch.setattr("kt_mcp_client.kt_runtime.subprocess.run", fake_run)
    result = runtime.start()
    assert result.cli_status == "degraded"
    assert not result.cli_running
    assert result.local_ready
    assert not result.public_ready
    assert "state='degraded'" in result.detail
    assert any(command[command.index("mcp-serve") + 1] == "start" for command in calls)


def test_start_marks_ready_only_when_all_flags_are_true(tmp_path, monkeypatch):
    runtime = _configured_runtime(tmp_path)

    def fake_run(command, **kwargs):
        action = command[command.index("mcp-serve") + 1]
        if action == "workspace":
            return subprocess.CompletedProcess(command, 0, json.dumps({"workspaces": {"default": str(runtime.profile.workspace_path)}}), "")
        if action == "start":
            return subprocess.CompletedProcess(command, 0, json.dumps({"state": "ready", "public_ready": True, "local_ready": True}), "")
        if action == "url":
            return subprocess.CompletedProcess(command, 0, f"https://staging.example.invalid/mcp/{ACTUAL_TOKEN}\n", "")
        raise AssertionError(action)

    monkeypatch.setattr("kt_mcp_client.kt_runtime.subprocess.run", fake_run)
    result = runtime.start()
    assert result.cli_status == "ready"
    assert result.cli_running
