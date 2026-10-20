import json
import subprocess
import sys

import pytest

from kt_mcp_client import __main__ as cli
from kt_mcp_client import provisioning
from kt_mcp_client.config import DeviceProfile
from kt_mcp_client.kt_runtime import KTRuntime, KTRuntimeError


def result(state="offline", local=True, public=False, code=1):
    return subprocess.CompletedProcess([], code, json.dumps({
        "state": state, "local_ready": local, "public_ready": public,
    }), "")


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    profile = DeviceProfile(kt_python=sys.executable, kt_home=str(tmp_path / "kt-home"))
    runtime = KTRuntime(profile, tmp_path / "runtime", config_root=tmp_path)
    runtime._setup_done = True
    monkeypatch.setattr(runtime, "_configuration_exists", lambda: True)
    monkeypatch.setattr(runtime, "_check_start_profile", lambda: None)
    monkeypatch.setattr(runtime, "_validate_connection", lambda: None)
    monkeypatch.setattr(runtime, "ensure_workspace_registered", lambda: False)
    return runtime


def test_local_bootstrap_uses_only_supported_short_wait(runtime, monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return result()
    monkeypatch.setattr(runtime, "_run_cli", run)
    status = runtime.start(wait_for_public=False)
    assert calls[0][0] == ["start", "--wait", "5", "--json"]
    assert len(calls) == 1
    assert status.local_ready is True
    assert status.public_ready is False
    assert status.cli_status == "degraded"


def test_default_public_start_keeps_existing_behavior(runtime, monkeypatch):
    calls = []
    monkeypatch.setattr(runtime, "_run_cli", lambda args, **kw: (calls.append(args) or result()))
    runtime.start()
    assert calls == [["start", "--wait", "30", "--json"]]


def test_local_start_polls_native_status_instead_of_starting_again(runtime, monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return result("starting", local=False) if len(calls) == 1 else result(code=0)
    monkeypatch.setattr(runtime, "_run_cli", run)
    status = runtime.start(wait_for_public=False)
    assert status.local_ready
    assert [c[0] for c in calls] == ["start", "status"]


@pytest.mark.parametrize("state", ["failed", "stopped", "unresponsive", "unknown"])
def test_fast_path_does_not_accept_terminal_state_with_stale_ready(runtime, monkeypatch, state):
    monkeypatch.setattr(runtime, "_run_cli", lambda *a, **k: result(state, code=0))
    with pytest.raises(KTRuntimeError):
        runtime.start(wait_for_public=False)


def test_fast_path_rejects_contradictory_ready_flags(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "_run_cli", lambda *a, **k: result("ready", code=0))
    with pytest.raises(KTRuntimeError, match="相互矛盾"):
        runtime.start(wait_for_public=False)


def test_poll_has_a_deadline_and_never_fabricates_readiness(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "_run_cli", lambda *a, **k: pytest.fail("Deadline already expired"))
    with pytest.raises(KTRuntimeError, match="仍未就绪"):
        runtime._wait_for_local_start(result("starting", local=False), timeout=0)


def _safe_override(monkeypatch):
    monkeypatch.setenv("KT_MCP_PROVISION_URL", "https://provision.example.invalid/api")
    monkeypatch.setenv(
        "KT_MCP_PROVISION_ORIGIN_MAP",
        '{"https://example.invalid": ["provision.example.invalid"]}',
    )


def test_preflight_returns_no_credentials_and_performs_no_network(monkeypatch):
    monkeypatch.setenv("KT_MCP_PROVISION_TOKEN", "synthetic-token")
    _safe_override(monkeypatch)
    monkeypatch.setattr(provisioning, "_no_redirect_opener", lambda *a, **k: pytest.fail("Preflight is local only"))
    assert provisioning.check_provisioning_configuration("https://example.invalid") is None


@pytest.mark.parametrize("token", ["bad\nheader", "bad\x00header", "x" * 4097])
def test_preflight_rejects_malformed_credentials_without_echo(monkeypatch, token):
    _safe_override(monkeypatch)
    if "\x00" in token:
        monkeypatch.setattr(
            provisioning.os,
            "environ",
            {
                "KT_MCP_PROVISION_TOKEN": token,
                "KT_MCP_PROVISION_URL": "https://provision.example.invalid/api",
                "KT_MCP_PROVISION_ORIGIN_MAP": '{"https://example.invalid": ["provision.example.invalid"]}',
            },
        )
    else:
        monkeypatch.setenv("KT_MCP_PROVISION_TOKEN", token)
    with pytest.raises(provisioning.ProvisioningError, match="凭据格式无效") as error:
        provisioning.check_provisioning_configuration("https://example.invalid")
    assert token not in str(error.value)


def test_cli_missing_token_never_starts_runtime(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("KT_MCP_PROVISION_TOKEN", raising=False)
    _safe_override(monkeypatch)
    profile = DeviceProfile(public_origin="https://example.invalid")
    monkeypatch.setattr(cli, "load", lambda path: profile)
    monkeypatch.setattr(cli, "config_file", lambda path: tmp_path / "profile.json")
    monkeypatch.setattr(cli, "KTRuntime", lambda *a, **k: pytest.fail("Must fail before runtime construction"))
    assert cli.main(["--provision"]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert "KT_MCP_PROVISION_TOKEN" in output.err


def test_fast_path_rejects_abnormal_exit_with_ready_flag(runtime, monkeypatch):
    monkeypatch.setattr(runtime, "_run_cli", lambda *a, **k: result(code=2))
    with pytest.raises(KTRuntimeError, match="异常退出"):
        runtime.start(wait_for_public=False)
