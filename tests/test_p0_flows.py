from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kt_mcp_client import __main__ as cli
from tests.helpers import set_test_endpoint
from kt_mcp_client.config import ConfigError, DeviceProfile, save
from kt_mcp_client.kt_runtime import KTRuntime, KTRuntimeError
from kt_mcp_client.provisioning import ProvisioningError, request_independent_endpoint

@pytest.fixture(autouse=True)
def registration_environment(monkeypatch):
    monkeypatch.setenv("KT_MCP_PROVISION_TOKEN", "synthetic-test-token")
    monkeypatch.delenv("KT_MCP_PROVISION_URL", raising=False)
    monkeypatch.setenv("KT_MCP_PROVISION_ORIGIN_MAP", '{"https://device.example.invalid": ["provision.example.invalid"]}')


TOKEN = "Z" * 43
ENDPOINT = f"https://device.example.invalid/mcp/{TOKEN}"
OTHER_ENDPOINT = f"https://other.example.invalid/mcp/{TOKEN}"


class Response:
    status = 200

    def __init__(self, payload: object) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def close(self) -> None:
        pass


def test_kt_url_accepts_endpoint_url_json_and_rejects_origin_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = DeviceProfile(
        kt_python=sys.executable,
        kt_home=str(tmp_path / "kt-home"),
        public_origin="https://device.example.invalid",
    )
    runtime = KTRuntime(
        profile,
        tmp_path / "config" / "runtime",
        config_root=tmp_path / "config",
    )

    result = subprocess.CompletedProcess(
        [], 0, json.dumps({"endpoint_url": OTHER_ENDPOINT}), ""
    )
    monkeypatch.setattr(runtime, "_run_cli", lambda *args, **kwargs: result)

    with pytest.raises(KTRuntimeError, match="origin"):
        runtime.url()
    assert not profile.endpoint_verified
    assert not (tmp_path / "config" / "profile.json").exists()

    assert runtime._url_output(
        subprocess.CompletedProcess([], 0, json.dumps({"endpoint_url": ENDPOINT}), "")
    ) == ENDPOINT


def test_nested_conflicting_endpoint_values_fail_closed(tmp_path: Path) -> None:
    profile = DeviceProfile(public_origin="https://device.example.invalid")
    runtime = KTRuntime(profile, tmp_path / "runtime", config_root=tmp_path)
    result = subprocess.CompletedProcess(
        [], 0,
        json.dumps({"url": ENDPOINT, "nested": {"endpoint_url": OTHER_ENDPOINT}}), ""
    )
    with pytest.raises(KTRuntimeError, match="冲突"):
        runtime._url_output(result)

def test_provisioning_accepts_endpoint_url_response_key_and_rejects_mismatched_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KT_MCP_PROVISION_URL", "https://provision.example.invalid/api")
    monkeypatch.setenv("KT_MCP_PROVISION_ORIGIN_MAP", '{"https://device.example.invalid": ["provision.example.invalid"]}')
    monkeypatch.setenv("KT_MCP_PROVISION_TOKEN", "enrollment-token")
    verified_config = tmp_path / "independent"
    profile = DeviceProfile(
        kt_home=str(verified_config / "kt-home"),
        public_origin="https://device.example.invalid",
        secret="legacy-" + TOKEN,
    )
    set_test_endpoint(profile, ENDPOINT)
    save(profile, verified_config)

    assert request_independent_endpoint(
        "https://device.example.invalid",
        ENDPOINT,
        profile=profile,
        config_path=verified_config,
        opener=lambda request, timeout: Response({"endpoint_url": ENDPOINT, "remote_port": 18766}),
    ) == ENDPOINT

    profile = profile
    bad_url = "https://device.example.invalid/mcp/" + ("R" * 43)
    with pytest.raises(ProvisioningError, match="不一致"):
        request_independent_endpoint(
            "https://device.example.invalid",
            bad_url,
            profile=profile,
            config_path=verified_config,
            opener=lambda request, timeout: pytest.fail("network must not be called"),
        )


def test_cli_provision_runs_runtime_before_network_and_prints_only_local_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[object] = []
    identity = tmp_path / "id"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("key", encoding="utf-8")
    known_hosts.write_text("vps.example.invalid ssh-ed25519 AAAA", encoding="utf-8")
    profile = DeviceProfile(
        public_origin="https://device.example.invalid",
        ssh_host="vps.example.invalid", ssh_user="kt",
        ssh_identity_file=str(identity), ssh_known_hosts=str(known_hosts),
    )
    monkeypatch.setenv("KT_MCP_PROVISION_URL", "https://provision.example.invalid/api")
    monkeypatch.setenv("KT_MCP_PROVISION_ORIGIN_MAP", '{"https://device.example.invalid": ["provision.example.invalid"]}')

    class FakeRuntime:
        def __init__(self, *args, **kwargs) -> None:
            calls.append("construct")

        def setup(self) -> None:
            calls.append("setup")

        def start(self, *, wait_for_public=True):
            calls.append("start")
            return type("Status", (), {"cli_status": "ready", "local_ready": True, "detail": ""})()

        def url(self) -> str:
            calls.append("url")
            return ENDPOINT

    def provision(origin: str, endpoint: str, config: str | None, *, profile) -> str:
        calls.append((origin, endpoint, config, profile))
        return endpoint

    monkeypatch.setattr(cli, "load", lambda path: profile)
    monkeypatch.setattr(cli, "config_file", lambda path: tmp_path / "profile.json")
    monkeypatch.setattr(cli, "KTRuntime", FakeRuntime)
    monkeypatch.setattr(cli, "provision_and_save", provision)
    monkeypatch.setattr(cli, "verify_public_endpoint", lambda endpoint, profile: None)
    class FakeTunnelProcess:
        running = True
        identity_ok = True
    class FakeTunnelStatus:
        configured = True
        process = FakeTunnelProcess()
        detail = "ready"
    class FakeTunnel:
        def __init__(self, *args, **kwargs): pass
        def start(self): return FakeTunnelStatus()
    monkeypatch.setattr(cli, "SSHTunnel", FakeTunnel)

    assert cli.main(["--provision", "--config", str(tmp_path)]) == 0
    assert calls == [
        "construct",
        "setup",
        "start",
        "url",
        ("https://device.example.invalid", ENDPOINT, str(tmp_path), profile),
    ]
    assert capsys.readouterr().out.strip() == ENDPOINT


def test_cli_start_returns_nonzero_when_supervisor_is_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = DeviceProfile(public_origin="https://device.example.invalid")

    class FakeSupervisor:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self, *, wait_for_public=True):
            return type("Status", (), {"public_dict": lambda self: {"state": "degraded", "detail": "not ready"}})()

    monkeypatch.setattr(cli, "load", lambda path: profile)
    monkeypatch.setattr(cli, "config_file", lambda path: tmp_path / "profile.json")
    monkeypatch.setattr(cli, "Supervisor", FakeSupervisor)
    assert cli.main(["--start", "--config", str(tmp_path)]) == 2
    assert "not ready" in capsys.readouterr().err
def test_cli_provision_failure_does_not_print_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    profile = DeviceProfile(public_origin="https://device.example.invalid")
    monkeypatch.setenv("KT_MCP_PROVISION_URL", "https://provision.example.invalid/api")
    monkeypatch.setenv("KT_MCP_PROVISION_ORIGIN_MAP", '{"https://device.example.invalid": ["provision.example.invalid"]}')

    class FakeRuntime:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def setup(self) -> None:
            pass

        def start(self, *, wait_for_public=True):
            return type("Status", (), {"cli_status": "ready", "local_ready": True, "detail": ""})()

        def url(self) -> str:
            return ENDPOINT

    monkeypatch.setattr(cli, "load", lambda path: profile)
    monkeypatch.setattr(cli, "config_file", lambda path: tmp_path / "profile.json")
    monkeypatch.setattr(cli, "KTRuntime", FakeRuntime)
    monkeypatch.setattr(
        cli,
        "provision_and_save",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ProvisioningError("provisioning failed")
        ),
    )

    assert cli.main(["--provision", "--config", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert ENDPOINT not in captured.err


@pytest.mark.parametrize("cli_status", ["error", "stopped", "unknown"])
def test_cli_provision_rejects_terminal_start_status_even_when_local_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_status: str,
) -> None:
    """A stale local_ready flag must not bypass a failed start state."""

    profile = DeviceProfile(public_origin="https://device.example.invalid")
    monkeypatch.setenv("KT_MCP_PROVISION_URL", "https://provision.example.invalid/api")
    monkeypatch.setenv("KT_MCP_PROVISION_ORIGIN_MAP", '{"https://device.example.invalid": ["provision.example.invalid"]}')
    provisioned = False

    class FakeRuntime:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def setup(self) -> None:
            pass

        def start(self, *, wait_for_public=True):
            return type(
                "Status",
                (),
                {"cli_status": cli_status, "local_ready": True, "detail": "terminal"},
            )()

        def url(self) -> str:
            pytest.fail("KT url must not run after a terminal start status")

    def provision(*args, **kwargs):
        nonlocal provisioned
        provisioned = True

    monkeypatch.setattr(cli, "load", lambda path: profile)
    monkeypatch.setattr(cli, "config_file", lambda path: tmp_path / "profile.json")
    monkeypatch.setattr(cli, "KTRuntime", FakeRuntime)
    monkeypatch.setattr(cli, "provision_and_save", provision)

    assert cli.main(["--provision", "--config", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert not provisioned
    assert "terminal" in captured.err
    assert captured.out == ""


def test_set_actual_endpoint_does_not_adopt_kt_origin(tmp_path: Path) -> None:
    profile = DeviceProfile(
        kt_home=str(tmp_path / "kt-home"),
        public_origin="https://device.example.invalid",
    )
    from tests.helpers import set_test_endpoint

    with pytest.raises(ConfigError, match="origin"):
        set_test_endpoint(profile, OTHER_ENDPOINT)
    assert profile.public_origin == "https://device.example.invalid"
    assert not profile.endpoint_verified
