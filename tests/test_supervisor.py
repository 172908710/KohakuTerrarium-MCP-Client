"""Supervisor health-state tests."""

from dataclasses import dataclass
from pathlib import Path
import pytest
import sys

from kt_mcp_client.config import DeviceProfile
from kt_mcp_client.kt_runtime import RuntimeStatus
from kt_mcp_client.process import ProcessStatus
from kt_mcp_client.ssh_tunnel import SSHTunnelError, TunnelStatus
from kt_mcp_client.supervisor import Supervisor, SupervisorError


@dataclass
class FakeProcess:
    value: ProcessStatus

    def status(self) -> ProcessStatus:
        return self.value


class FakeRuntime:
    def __init__(self, status: RuntimeStatus) -> None:
        self.status_value = status
        self.process = FakeProcess(status.process)
        self.started = 0

    def status(self) -> RuntimeStatus:
        return self.status_value

    def start(self) -> RuntimeStatus:
        self.started += 1
        return self.status_value

    def stop(self) -> RuntimeStatus:
        return self.status_value


class FakeTunnel:
    def __init__(self, status: TunnelStatus, *, start_error: Exception | None = None) -> None:
        self.status_value = status
        self.process = FakeProcess(status.process)
        self.start_error = start_error
        self.started = 0

    def status(self) -> TunnelStatus:
        return self.status_value

    def start(self) -> TunnelStatus:
        self.started += 1
        if self.start_error is not None:
            raise self.start_error
        return self.status_value

    def stop(self) -> TunnelStatus:
        return self.status_value


def profile(tmp_path: Path) -> DeviceProfile:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return DeviceProfile(
        display_name="test",
        workspace_path=str(workspace),
        kt_python=sys.executable,
        kt_home=str(tmp_path / "kt-home"),
        public_origin="https://device.example.invalid",
    )


def runtime_status(
    tmp_path: Path,
    cli_status: str,
    *,
    local_ready: bool | None = None,
    public_ready: bool | None = None,
) -> RuntimeStatus:
    if local_ready is None:
        local_ready = cli_status == "ready"
    if public_ready is None:
        public_ready = cli_status == "ready"
    return RuntimeStatus(
        ProcessStatus("kt-runtime", True, 100, True, None),
        cli_status,
        "state='starting'" if cli_status != "ready" else "ready",
        cli_status == "ready",
        local_ready,
        public_ready,
    )


def healthy_tunnel() -> TunnelStatus:
    return TunnelStatus(ProcessStatus("ssh-tunnel", True, 200, True, None), True, "healthy")


def test_status_rechecks_stale_running_runtime_health(tmp_path):
    runtime = FakeRuntime(runtime_status(tmp_path, "degraded"))
    tunnel = FakeTunnel(healthy_tunnel())
    supervisor = Supervisor(
        profile(tmp_path),
        tmp_path / "config" / "runtime",
        runtime=runtime,
        tunnel=tunnel,
        reconnect=False,
    )
    supervisor._state = "running"
    supervisor._detail = "stale running state"

    result = supervisor.status()

    assert result.state == "degraded"
    assert "KT CLI 未 ready" in result.detail
    assert result.runtime["cli_status"] == "degraded"


def test_status_rechecks_dead_or_unidentified_tunnel(tmp_path):
    runtime = FakeRuntime(runtime_status(tmp_path, "ready"))
    tunnel = FakeTunnel(
        TunnelStatus(ProcessStatus("ssh-tunnel", False, 200, False, 1), True, "SSH stopped")
    )
    supervisor = Supervisor(
        profile(tmp_path),
        tmp_path / "config" / "runtime",
        runtime=runtime,
        tunnel=tunnel,
        reconnect=False,
    )
    supervisor._state = "running"

    result = supervisor.status()

    assert result.state == "degraded"
    assert "SSH 隧道进程未运行" in result.detail


def test_reconnect_does_not_start_tunnel_when_runtime_not_ready(tmp_path):
    runtime = FakeRuntime(runtime_status(tmp_path, "degraded"))
    tunnel = FakeTunnel(healthy_tunnel())
    supervisor = Supervisor(
        profile(tmp_path), tmp_path / "config" / "runtime",
        runtime=runtime, tunnel=tunnel, reconnect=False,
    )
    supervisor._state = "degraded"
    supervisor._reconnect_stop.set()
    supervisor._reconnect_loop()
    assert tunnel.started == 0


def test_local_ready_degraded_runtime_bootstraps_ssh_tunnel(tmp_path):
    runtime = FakeRuntime(
        runtime_status(tmp_path, "degraded", local_ready=True, public_ready=False)
    )
    tunnel = FakeTunnel(healthy_tunnel())
    supervisor = Supervisor(
        profile(tmp_path),
        tmp_path / "config" / "runtime",
        runtime=runtime,
        tunnel=tunnel,
        reconnect=False,
    )

    with pytest.raises(SupervisorError, match="state='starting'"):
        supervisor.start()
    assert tunnel.started == 1
    assert supervisor.state == "degraded"
def test_start_without_ssh_stays_degraded_after_local_kt_ready(tmp_path):
    runtime = FakeRuntime(runtime_status(tmp_path, "ready"))
    tunnel = FakeTunnel(
        TunnelStatus(ProcessStatus("ssh-tunnel", False, None, False, None), False),
        start_error=SSHTunnelError("ssh_host 未配置"),
    )
    supervisor = Supervisor(
        profile(tmp_path),
        tmp_path / "config" / "runtime",
        runtime=runtime,
        tunnel=tunnel,
        reconnect=False,
    )

    with pytest.raises(SupervisorError, match="公网不可用"):
        supervisor.start()
    assert runtime.started == 1
    assert tunnel.started == 1
    assert supervisor.state == "degraded"


def test_fresh_status_derives_live_health_without_creating_stop_marker(tmp_path):
    runtime = FakeRuntime(runtime_status(tmp_path, "ready"))
    tunnel = FakeTunnel(healthy_tunnel())
    supervisor = Supervisor(
        profile(tmp_path),
        tmp_path / "config" / "runtime",
        runtime=runtime,
        tunnel=tunnel,
        reconnect=False,
    )

    assert supervisor.status().state == "running"
    assert not (tmp_path / "config" / "runtime" / "state" / "status.json").exists()


def test_explicit_stop_state_is_sticky_for_new_status_process(tmp_path):
    runtime = FakeRuntime(runtime_status(tmp_path, "ready"))
    tunnel = FakeTunnel(healthy_tunnel())
    root = tmp_path / "config" / "runtime"
    supervisor = Supervisor(
        profile(tmp_path), root, runtime=runtime, tunnel=tunnel, reconnect=False
    )

    stopped = supervisor.stop()
    assert stopped.state == "stopped"
    assert (root / "state" / "status.json").is_file()

    # A new CLI invocation must preserve the explicit stop decision even when
    # stale process probes still look healthy.
    fresh_runtime = FakeRuntime(runtime_status(tmp_path, "ready"))
    fresh_tunnel = FakeTunnel(healthy_tunnel())
    reopened = Supervisor(
        supervisor.profile, root, runtime=fresh_runtime, tunnel=fresh_tunnel, reconnect=False
    )
    assert reopened.status().state == "stopped"


def test_persisted_running_state_is_rechecked_by_new_status_process(tmp_path):
    root = tmp_path / "config" / "runtime"
    supervisor = Supervisor(
        profile(tmp_path),
        root,
        runtime=FakeRuntime(runtime_status(tmp_path, "ready")),
        tunnel=FakeTunnel(healthy_tunnel()),
        reconnect=False,
    )
    assert supervisor.start().state == "running"

    reopened = Supervisor(
        supervisor.profile,
        root,
        runtime=FakeRuntime(runtime_status(tmp_path, "ready")),
        tunnel=FakeTunnel(healthy_tunnel()),
        reconnect=False,
    )
    assert reopened.status().state == "running"


def test_persisted_error_state_is_sticky_for_new_status_process(tmp_path):
    root = tmp_path / "config" / "runtime"
    state_dir = root / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "status.json").write_text(
        '{"state":"error","detail":"startup failed"}\n', encoding="utf-8"
    )
    reopened = Supervisor(
        profile(tmp_path),
        root,
        runtime=FakeRuntime(runtime_status(tmp_path, "ready")),
        tunnel=FakeTunnel(healthy_tunnel()),
        reconnect=False,
    )

    result = reopened.status()
    assert result.state == "error"
    assert result.detail == "startup failed"


def test_status_running_to_degraded_starts_auto_reconnect(tmp_path, monkeypatch):
    runtime = FakeRuntime(runtime_status(tmp_path, "ready"))
    tunnel = FakeTunnel(healthy_tunnel())
    supervisor = Supervisor(
        profile(tmp_path),
        tmp_path / "config" / "runtime",
        runtime=runtime,
        tunnel=tunnel,
        reconnect=True,
    )
    supervisor._fresh_state = False
    supervisor._state = "running"
    calls: list[str] = []
    monkeypatch.setattr(
        supervisor, "_start_reconnect_thread", lambda: calls.append("reconnect")
    )

    runtime.status_value = runtime_status(
        tmp_path, "degraded", local_ready=True, public_ready=False
    )
    tunnel.status_value = TunnelStatus(
        ProcessStatus("ssh-tunnel", False, None, False, None), True, "SSH stopped"
    )
    result = supervisor.status()

    assert result.state == "degraded"
    assert calls == ["reconnect"]
