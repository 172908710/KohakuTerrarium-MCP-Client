"""Supervisor health-state tests."""

from dataclasses import dataclass
import json
from pathlib import Path
import pytest
import sys
import threading
import time

from kt_mcp_client.config import DeviceProfile
from kt_mcp_client.kt_runtime import KTRuntimeError, RuntimeStatus
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
        self.stopped = 0
        self.start_wait_for_public: list[bool] = []

    def status(self) -> RuntimeStatus:
        return self.status_value

    def start(self, *, wait_for_public: bool = True) -> RuntimeStatus:
        self.started += 1
        self.start_wait_for_public.append(wait_for_public)
        return self.status_value

    def stop(self) -> RuntimeStatus:
        self.stopped += 1
        return self.status_value


class FakeTunnel:
    def __init__(self, status: TunnelStatus, *, start_error: Exception | None = None) -> None:
        self.status_value = status
        self.process = FakeProcess(status.process)
        self.start_error = start_error
        self.started = 0
        self.stopped = 0

    def status(self) -> TunnelStatus:
        return self.status_value

    def start(self) -> TunnelStatus:
        self.started += 1
        if self.start_error is not None:
            raise self.start_error
        return self.status_value

    def stop(self) -> TunnelStatus:
        self.stopped += 1
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


def test_stop_reuses_post_stop_snapshots_without_extra_probes(tmp_path, monkeypatch):
    runtime = FakeRuntime(runtime_status(tmp_path, "ready"))
    tunnel = FakeTunnel(healthy_tunnel())
    root = tmp_path / "config" / "runtime"
    supervisor = Supervisor(
        profile(tmp_path), root, runtime=runtime, tunnel=tunnel, reconnect=False
    )
    stopped_runtime = RuntimeStatus(
        ProcessStatus("kt-runtime", False, None, False, 0), "stopped", "", False
    )
    stopped_tunnel = TunnelStatus(
        ProcessStatus("ssh-tunnel", False, None, False, 0), True
    )
    calls = []

    def stop_tunnel():
        calls.append("tunnel.stop")
        assert supervisor._reconnect_stop.is_set()
        return stopped_tunnel

    def stop_runtime():
        calls.append("runtime.stop")
        return stopped_runtime

    monkeypatch.setattr(tunnel, "stop", stop_tunnel)
    monkeypatch.setattr(runtime, "stop", stop_runtime)
    monkeypatch.setattr(tunnel, "status", lambda: pytest.fail("Extra tunnel health probe"))
    monkeypatch.setattr(runtime, "status", lambda: pytest.fail("Extra KT status subprocess"))

    result = supervisor.stop()

    assert calls == ["tunnel.stop", "runtime.stop"]
    assert result.state == "stopped"
    assert result.runtime["cli_status"] == "stopped"
    assert result.tunnel["process"]["running"] is False
    assert json.loads((root / "state" / "status.json").read_text(encoding="utf-8")) == result.public_dict()


def test_tunnel_stop_error_still_attempts_runtime_stop(tmp_path, monkeypatch):
    runtime = FakeRuntime(runtime_status(tmp_path, "ready"))
    tunnel = FakeTunnel(healthy_tunnel())
    supervisor = Supervisor(
        profile(tmp_path), tmp_path / "config" / "runtime",
        runtime=runtime, tunnel=tunnel, reconnect=False,
    )
    calls = []

    def stop_tunnel():
        calls.append("tunnel.stop")
        raise SSHTunnelError("identity check refused")

    def stop_runtime():
        calls.append("runtime.stop")
        return runtime.status_value

    monkeypatch.setattr(tunnel, "stop", stop_tunnel)
    monkeypatch.setattr(runtime, "stop", stop_runtime)

    with pytest.raises(SSHTunnelError, match="identity check refused"):
        supervisor.stop()

    assert calls == ["tunnel.stop", "runtime.stop"]
    assert supervisor._reconnect_stop.is_set()


def test_runtime_stop_error_probes_once_and_preserves_live_status(tmp_path, monkeypatch):
    runtime = FakeRuntime(runtime_status(tmp_path, "ready"))
    tunnel = FakeTunnel(TunnelStatus(ProcessStatus("ssh-tunnel", False, None, False, 0), True))
    supervisor = Supervisor(
        profile(tmp_path), tmp_path / "config" / "runtime",
        runtime=runtime, tunnel=tunnel, reconnect=False,
    )
    calls = []

    def stop_runtime():
        calls.append("runtime.stop")
        raise KTRuntimeError("stop timed out")

    def runtime_probe():
        calls.append("runtime.status")
        return runtime.status_value

    def tunnel_probe():
        calls.append("tunnel.status")
        return tunnel.status_value

    monkeypatch.setattr(runtime, "stop", stop_runtime)
    monkeypatch.setattr(runtime, "status", runtime_probe)
    monkeypatch.setattr(tunnel, "status", tunnel_probe)

    result = supervisor.stop()

    assert calls == ["runtime.stop", "runtime.status", "tunnel.status"]
    assert result.state == "stopped"
    assert result.runtime["cli_status"] == "ready"
    assert result.runtime["local_ready"] is True
    assert result.detail == "stop timed out"


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


def test_status_running_to_degraded_does_not_acquire_monitor_ownership(tmp_path, monkeypatch):
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
    assert calls == []
    assert supervisor._reconnect_thread is None


def dead_tunnel() -> TunnelStatus:
    return TunnelStatus(
        ProcessStatus("ssh-tunnel", False, None, False, 1), True, "SSH stopped"
    )


class RecoveringTunnel(FakeTunnel):
    def __init__(self) -> None:
        super().__init__(healthy_tunnel())
        self.reconnected = threading.Event()

    def start(self) -> TunnelStatus:
        self.started += 1
        self.status_value = healthy_tunnel()
        self.reconnected.set()
        return self.status_value


def wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    pause = threading.Event()
    while not predicate():
        remaining = deadline - time.monotonic()
        assert remaining > 0, "Timed out waiting for supervisor monitor"
        pause.wait(min(0.01, remaining))


@pytest.fixture
def make_supervisor(tmp_path, monkeypatch):
    selected_profile = profile(tmp_path)
    supervisors: list[Supervisor] = []

    def make(runtime=None, tunnel=None, *, reconnect=None, interval=0.01):
        supervisor = Supervisor(
            selected_profile,
            tmp_path / "config" / "runtime",
            runtime=runtime or FakeRuntime(runtime_status(tmp_path, "ready")),
            tunnel=tunnel or FakeTunnel(healthy_tunnel()),
            reconnect=reconnect,
        )
        supervisor._reconnect_interval = interval
        monkeypatch.setattr(supervisor, "_check_profile", lambda: [])
        monkeypatch.setattr(supervisor, "_check_port_available", lambda: None)
        supervisors.append(supervisor)
        return supervisor

    yield make
    for supervisor in reversed(supervisors):
        supervisor.stop_monitoring()


def test_healthy_start_arms_continuous_monitor(make_supervisor):
    supervisor = make_supervisor(interval=60.0)

    result = supervisor.start()
    worker = supervisor._reconnect_thread

    assert result.state == "running"
    assert worker is not None and worker.is_alive()
    assert supervisor.runtime.started == 1
    assert supervisor.runtime.start_wait_for_public == [True]
    assert supervisor.tunnel.started == 1
    assert supervisor.start().state == "running"
    assert supervisor._reconnect_thread is not None
    assert supervisor._reconnect_thread.is_alive()
    assert not worker.is_alive()
    assert supervisor.runtime.started == supervisor.tunnel.started == 1


@pytest.mark.parametrize("marker", [None, "stopped", "error"])
def test_monitor_adopts_live_health_over_stale_marker_without_starting(
    tmp_path, make_supervisor, marker
):
    if marker is not None:
        state_dir = tmp_path / "config" / "runtime" / "state"
        state_dir.mkdir(parents=True)
        (state_dir / "status.json").write_text(
            json.dumps({"state": marker, "detail": "old lifecycle decision"}),
            encoding="utf-8",
        )
    supervisor = make_supervisor(interval=60.0)

    result = supervisor.monitor()
    worker = supervisor._reconnect_thread

    assert result.state == "running"
    assert result.detail == "healthy"
    assert supervisor.runtime.started == supervisor.tunnel.started == 0
    assert worker is not None and worker.is_alive()
    assert supervisor.monitor().state == "running"
    assert supervisor._reconnect_thread is worker
    assert json.loads((supervisor.state_dir / "status.json").read_text(encoding="utf-8")) == result.public_dict()


def test_adopted_pair_transfers_to_one_monitor_owner(tmp_path, make_supervisor):
    runtime = FakeRuntime(runtime_status(tmp_path, "ready"))
    tunnel = RecoveringTunnel()
    old_owner = make_supervisor(runtime, tunnel)
    old_owner.monitor()
    old_worker = old_owner._reconnect_thread

    old_owner.stop_monitoring()
    assert old_worker is not None and not old_worker.is_alive()
    assert old_owner._reconnect_thread is None
    assert old_owner._reconnect_stop.is_set()
    assert runtime.stopped == tunnel.stopped == 0
    assert runtime.started == tunnel.started == 0

    new_owner = make_supervisor(runtime, tunnel)
    new_owner.monitor()
    assert new_owner.runtime is old_owner.runtime is runtime
    assert new_owner.tunnel is old_owner.tunnel is tunnel
    assert new_owner._reconnect_thread is not None
    assert new_owner._reconnect_thread.is_alive()
    tunnel.status_value = dead_tunnel()

    assert tunnel.reconnected.wait(2.0)
    wait_until(lambda: new_owner.state == "running")
    assert tunnel.started == 1
    # A read-only probe by the retired owner cannot reclaim monitoring.
    old_owner.status()
    assert old_owner._reconnect_thread is None
    assert runtime.started == 0


@pytest.mark.parametrize("entrypoint", ["start", "monitor"])
def test_auto_reconnect_false_never_starts_monitor(
    tmp_path, make_supervisor, entrypoint
):
    supervisor = make_supervisor()
    supervisor.profile.auto_reconnect = False
    supervisor.reconnect = supervisor.profile.auto_reconnect

    assert getattr(supervisor, entrypoint)().state == "running"
    assert supervisor._reconnect_thread is None
    starts = supervisor.tunnel.started
    supervisor.tunnel.status_value = dead_tunnel()
    assert supervisor.status().state == "degraded"
    supervisor._start_reconnect_thread()
    assert supervisor._reconnect_thread is None
    assert supervisor.tunnel.started == starts


@pytest.mark.parametrize("entrypoint", ["start", "monitor"])
def test_monitor_detects_repeated_drops_and_reconnects_without_status_calls(
    tmp_path, make_supervisor, entrypoint
):
    runtime = FakeRuntime(runtime_status(tmp_path, "ready"))
    tunnel = RecoveringTunnel()
    supervisor = make_supervisor(runtime, tunnel)
    getattr(supervisor, entrypoint)()
    worker = supervisor._reconnect_thread
    initial_starts = tunnel.started

    for drop in range(1, 3):
        tunnel.reconnected.clear()
        with supervisor._lifecycle_lock:
            runtime.status_value = runtime_status(
                tmp_path, "degraded", local_ready=True, public_ready=False
            )
            tunnel.status_value = dead_tunnel()
        assert tunnel.reconnected.wait(2.0)
        wait_until(lambda: supervisor.state == "degraded")
        with supervisor._lifecycle_lock:
            assert tunnel.started == initial_starts + drop
            runtime.status_value = runtime_status(tmp_path, "ready")
        wait_until(lambda: supervisor.state == "running")
        assert supervisor._reconnect_thread is worker
        assert worker is not None and worker.is_alive()
    assert runtime.started == (1 if entrypoint == "start" else 0)


@pytest.mark.parametrize("entrypoint", ["start", "monitor"])
def test_public_readiness_can_settle_without_duplicate_process_starts(
    tmp_path, make_supervisor, entrypoint
):
    runtime = FakeRuntime(
        runtime_status(tmp_path, "degraded", local_ready=True, public_ready=False)
    )
    supervisor = make_supervisor(runtime)
    if entrypoint == "start":
        result = supervisor.start(wait_for_public=False)
        assert runtime.start_wait_for_public == [False]
    else:
        result = supervisor.monitor()
    assert result.state == "degraded"
    assert result.runtime["public_ready"] is False
    worker = supervisor._reconnect_thread
    assert worker is not None and worker.is_alive()
    starts = supervisor.tunnel.started

    with supervisor._lifecycle_lock:
        runtime.status_value = runtime_status(tmp_path, "ready")
    wait_until(lambda: supervisor.state == "running")

    assert supervisor.tunnel.started == starts
    assert runtime.started == (1 if entrypoint == "start" else 0)
    assert supervisor._reconnect_thread is worker


@pytest.mark.parametrize("wait_for_public", [False, True])
def test_start_resamples_runtime_after_tunnel_bootstrap(
    tmp_path, make_supervisor, monkeypatch, wait_for_public
):
    runtime = FakeRuntime(
        runtime_status(tmp_path, "degraded", local_ready=True, public_ready=False)
    )
    tunnel = FakeTunnel(dead_tunnel())
    supervisor = make_supervisor(runtime, tunnel, reconnect=False)
    calls = []

    def start_tunnel():
        calls.append("tunnel.start")
        tunnel.started += 1
        runtime.status_value = runtime_status(tmp_path, "ready")
        tunnel.status_value = healthy_tunnel()
        return tunnel.status_value

    def runtime_probe():
        calls.append("runtime.status")
        return runtime.status_value

    monkeypatch.setattr(tunnel, "start", start_tunnel)
    monkeypatch.setattr(runtime, "status", runtime_probe)

    result = supervisor.start(wait_for_public=wait_for_public)

    assert result.state == "running"
    assert calls.index("tunnel.start") < len(calls) - 1
    assert calls[-1] == "runtime.status"
    assert runtime.start_wait_for_public == [wait_for_public]
    assert runtime.started == tunnel.started == 1


@pytest.mark.parametrize(
    "broken_tunnel",
    [
        dead_tunnel(),
        TunnelStatus(ProcessStatus("ssh-tunnel", True, 200, False, None), True),
    ],
)
def test_start_does_not_report_running_from_stale_tunnel_start_result(
    tmp_path, make_supervisor, monkeypatch, broken_tunnel
):
    runtime = FakeRuntime(runtime_status(tmp_path, "ready"))
    tunnel = FakeTunnel(broken_tunnel)
    supervisor = make_supervisor(runtime, tunnel, reconnect=False)
    monkeypatch.setattr(tunnel, "start", healthy_tunnel)

    with pytest.raises(SupervisorError):
        supervisor.start(wait_for_public=False)

    assert supervisor.state == "degraded"
    assert supervisor.status().state == "degraded"


@pytest.mark.parametrize("entrypoint", ["start", "monitor"])
def test_healthy_ssh_cannot_make_unready_runtime_running(
    tmp_path, make_supervisor, entrypoint
):
    runtime = FakeRuntime(
        runtime_status(tmp_path, "degraded", local_ready=True, public_ready=False)
    )
    supervisor = make_supervisor(runtime, reconnect=False)
    if entrypoint == "start":
        result = supervisor.start(wait_for_public=False)
    else:
        result = supervisor.monitor()
    assert result.state == "degraded"
    assert supervisor.status().state == "degraded"


@pytest.mark.parametrize("failure", ["local", "ssh", "configuration"])
def test_bootstrap_start_still_raises_for_genuine_failures(
    tmp_path, make_supervisor, monkeypatch, failure
):
    runtime = FakeRuntime(runtime_status(tmp_path, "ready"))
    tunnel = FakeTunnel(healthy_tunnel())
    supervisor = make_supervisor(runtime, tunnel, reconnect=False)
    if failure == "local":
        runtime.status_value = runtime_status(tmp_path, "degraded", local_ready=False)
    elif failure == "ssh":
        tunnel.status_value = dead_tunnel()
        tunnel.start_error = SSHTunnelError("SSH connection refused")
    else:
        monkeypatch.setattr(supervisor, "_check_profile", lambda: ["invalid profile"])

    with pytest.raises(SupervisorError):
        supervisor.start(wait_for_public=False)

    assert supervisor.state != "running"
    if failure in {"local", "configuration"}:
        assert tunnel.started == 0


def test_repeated_strict_start_cannot_return_public_not_ready_success(
    tmp_path, make_supervisor
):
    runtime = FakeRuntime(
        runtime_status(tmp_path, "degraded", local_ready=True, public_ready=False)
    )
    supervisor = make_supervisor(runtime, reconnect=False)
    assert supervisor.start(wait_for_public=False).state == "degraded"

    with pytest.raises(SupervisorError, match="KT CLI 未 ready"):
        supervisor.start()

    assert runtime.started == supervisor.tunnel.started == 1


def test_start_after_adoption_does_not_succeed_with_missing_tunnel(make_supervisor):
    supervisor = make_supervisor(tunnel=FakeTunnel(dead_tunnel()), reconnect=False)
    assert supervisor.monitor().state == "degraded"

    with pytest.raises(SupervisorError):
        supervisor.start(wait_for_public=False)

    assert supervisor.runtime.started == 0
    assert supervisor.tunnel.started == 1


@pytest.mark.parametrize("cli_status", ["ready", "degraded"])
def test_persisted_running_start_bootstraps_dead_ssh_before_returning(
    tmp_path, make_supervisor, cli_status
):
    state_dir = tmp_path / "config" / "runtime" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "status.json").write_text(
        '{"state":"running","detail":"before reboot"}', encoding="utf-8"
    )
    runtime = FakeRuntime(
        runtime_status(
            tmp_path, cli_status, local_ready=True, public_ready=cli_status == "ready"
        )
    )
    tunnel = RecoveringTunnel()
    tunnel.status_value = dead_tunnel()
    supervisor = make_supervisor(runtime, tunnel, interval=60.0)

    result = supervisor.start(wait_for_public=False)

    assert result.state == ("running" if cli_status == "ready" else "degraded")
    assert result.tunnel["process"]["running"] is True
    assert result.tunnel["process"]["identity_ok"] is True
    assert tunnel.started == 1
    assert runtime.started == 0
    assert supervisor._reconnect_thread is not None
    assert supervisor._reconnect_thread.is_alive()
    assert json.loads((state_dir / "status.json").read_text(encoding="utf-8")) == result.public_dict()


def test_persisted_running_start_rechecks_public_readiness_after_ssh(
    tmp_path, make_supervisor, monkeypatch
):
    state_dir = tmp_path / "config" / "runtime" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "status.json").write_text('{"state":"running"}', encoding="utf-8")
    runtime = FakeRuntime(
        runtime_status(tmp_path, "degraded", local_ready=True, public_ready=False)
    )
    tunnel = RecoveringTunnel()
    tunnel.status_value = dead_tunnel()
    supervisor = make_supervisor(runtime, tunnel, interval=60.0)
    original_start = tunnel.start

    def start_tunnel():
        candidate = original_start()
        runtime.status_value = runtime_status(tmp_path, "ready")
        return candidate

    monkeypatch.setattr(tunnel, "start", start_tunnel)

    assert supervisor.start().state == "running"
    assert tunnel.started == 1
    assert runtime.started == 0


@pytest.mark.parametrize("wait_for_public", [False, True])
def test_persisted_active_start_still_raises_if_ssh_start_actually_fails(
    tmp_path, make_supervisor, wait_for_public
):
    state_dir = tmp_path / "config" / "runtime" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "status.json").write_text('{"state":"running"}', encoding="utf-8")
    tunnel = FakeTunnel(
        dead_tunnel(), start_error=SSHTunnelError("SSH connection refused")
    )
    supervisor = make_supervisor(tunnel=tunnel, interval=60.0)

    with pytest.raises(SupervisorError, match="SSH connection refused"):
        supervisor.start(wait_for_public=wait_for_public)

    assert tunnel.started == 1
    assert supervisor.runtime.started == 0
    assert supervisor.state == "degraded"
    assert supervisor._reconnect_thread is not None
    assert supervisor._reconnect_thread.is_alive()


def test_persisted_active_start_recovers_stopped_runtime_before_ssh(
    tmp_path, make_supervisor, monkeypatch
):
    state_dir = tmp_path / "config" / "runtime" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "status.json").write_text('{"state":"running"}', encoding="utf-8")
    runtime = FakeRuntime(
        RuntimeStatus(ProcessStatus("kt-runtime", False, None, False, 0), "stopped")
    )
    tunnel = RecoveringTunnel()
    tunnel.status_value = dead_tunnel()
    supervisor = make_supervisor(runtime, tunnel, interval=60.0)
    original_start = runtime.start

    def start_runtime(*, wait_for_public=True):
        runtime.status_value = runtime_status(
            tmp_path, "degraded", local_ready=True, public_ready=False
        )
        return original_start(wait_for_public=wait_for_public)

    monkeypatch.setattr(runtime, "start", start_runtime)

    result = supervisor.start(wait_for_public=False)

    assert result.state == "degraded"
    assert runtime.started == tunnel.started == 1
    assert runtime.start_wait_for_public == [False]
    assert result.tunnel["process"]["running"] is True
    assert supervisor._reconnect_thread is not None
    assert supervisor._reconnect_thread.is_alive()


@pytest.mark.parametrize("command", ["stop_monitoring", "stop"])
def test_cancellation_during_health_probe_prevents_late_restart(
    tmp_path, make_supervisor, monkeypatch, command
):
    runtime = FakeRuntime(runtime_status(tmp_path, "ready"))
    tunnel = RecoveringTunnel()
    supervisor = make_supervisor(runtime, tunnel)
    supervisor.monitor()
    worker = supervisor._reconnect_thread
    probe_entered = threading.Event()
    release_probe = threading.Event()
    completed = threading.Event()
    errors = []

    def blocked_probe():
        probe_entered.set()
        if not release_probe.wait(2.0):
            raise RuntimeError("Test did not release health probe")
        return runtime.status_value

    def cancel():
        try:
            getattr(supervisor, command)()
        except Exception as exc:
            errors.append(exc)
        finally:
            completed.set()

    with supervisor._lifecycle_lock:
        tunnel.status_value = dead_tunnel()
        monkeypatch.setattr(runtime, "status", blocked_probe)
    assert probe_entered.wait(2.0)
    stopper = threading.Thread(target=cancel, daemon=True)
    stopper.start()
    try:
        assert supervisor._reconnect_stop.wait(2.0)
        assert not completed.is_set()
    finally:
        release_probe.set()
    assert completed.wait(2.0)
    stopper.join(2.0)
    assert errors == []
    assert worker is not None and not worker.is_alive()
    assert supervisor._reconnect_thread is None
    assert tunnel.started == 0
    assert runtime.stopped == tunnel.stopped == (1 if command == "stop" else 0)
    supervisor.status()
    assert supervisor._reconnect_thread is None
    assert tunnel.started == 0


def test_stop_monitoring_joins_without_lifecycle_lock_and_before_event_reuse(
    make_supervisor, monkeypatch
):
    supervisor = make_supervisor(interval=60.0)
    retiring = threading.Event()
    release_retiring_worker = threading.Event()
    lifecycle_lock_available = threading.Event()
    stop_completed = threading.Event()
    monitor_completed = threading.Event()
    failures = []

    def worker_body():
        supervisor._reconnect_stop.wait()
        retiring.set()
        if not release_retiring_worker.wait(2.0):
            failures.append("Test did not release retiring worker")
            return
        if supervisor._lifecycle_lock.acquire(timeout=0.5):
            lifecycle_lock_available.set()
            supervisor._lifecycle_lock.release()
        else:
            failures.append("stop_monitoring joined while holding lifecycle lock")

    original_loop = supervisor._reconnect_loop
    monkeypatch.setattr(supervisor, "_reconnect_loop", worker_body)
    supervisor.monitor()
    old_worker = supervisor._reconnect_thread

    def cancel():
        supervisor.stop_monitoring()
        stop_completed.set()

    def readopt():
        supervisor.monitor()
        monitor_completed.set()

    stopper = threading.Thread(target=cancel, daemon=True)
    starter = threading.Thread(target=readopt, daemon=True)
    stopper.start()
    try:
        assert retiring.wait(2.0)
        monkeypatch.setattr(supervisor, "_reconnect_loop", original_loop)
        starter.start()
        assert supervisor._reconnect_stop.is_set()
        assert not stop_completed.is_set()
        assert not monitor_completed.is_set()
    finally:
        release_retiring_worker.set()
    assert stop_completed.wait(2.0)
    assert monitor_completed.wait(2.0)
    stopper.join(2.0)
    starter.join(2.0)
    assert failures == []
    assert lifecycle_lock_available.is_set()
    assert old_worker is not None and not old_worker.is_alive()
    assert supervisor._reconnect_thread is not old_worker
    assert supervisor._reconnect_thread is not None
    assert supervisor._reconnect_thread.is_alive()
    assert not supervisor._reconnect_stop.is_set()
    assert supervisor.runtime.started == supervisor.tunnel.started == 0
    assert supervisor.runtime.stopped == supervisor.tunnel.stopped == 0


@pytest.mark.parametrize("failed_probe", ["runtime", "tunnel"])
def test_transient_health_and_process_probe_exceptions_do_not_kill_monitor(
    tmp_path, make_supervisor, monkeypatch, failed_probe
):
    runtime = FakeRuntime(runtime_status(tmp_path, "ready"))
    tunnel = RecoveringTunnel()
    supervisor = make_supervisor(runtime, tunnel)
    supervisor.monitor()
    worker = supervisor._reconnect_thread
    failed = threading.Event()
    permit_recovery = threading.Event()
    target = runtime if failed_probe == "runtime" else tunnel

    def transient_probe():
        if not permit_recovery.is_set():
            failed.set()
            raise OSError("temporary status failure")
        return target.status_value

    def failed_process_probe():
        raise OSError("temporary process identity failure")

    with supervisor._lifecycle_lock:
        monkeypatch.setattr(target, "status", transient_probe)
        monkeypatch.setattr(target.process, "status", failed_process_probe)
        tunnel.status_value = dead_tunnel()
    assert failed.wait(2.0)
    wait_until(lambda: supervisor.state != "running")
    with supervisor._lifecycle_lock:
        assert tunnel.started == 0
        assert worker is not None and worker.is_alive()
        permit_recovery.set()

    assert tunnel.reconnected.wait(2.0)
    wait_until(lambda: supervisor.state == "running")
    assert supervisor._reconnect_thread is worker
    assert tunnel.started == 1


def test_transient_reconnect_error_is_retried_on_later_monitor_tick(
    tmp_path, make_supervisor, monkeypatch
):
    runtime = FakeRuntime(runtime_status(tmp_path, "ready"))
    tunnel = RecoveringTunnel()
    supervisor = make_supervisor(runtime, tunnel)
    supervisor.monitor()
    worker = supervisor._reconnect_thread
    failed_attempt = threading.Event()
    permit_recovery = threading.Event()
    original_start = tunnel.start

    def transient_start():
        if not permit_recovery.is_set():
            failed_attempt.set()
            raise OSError("temporary SSH launch failure")
        return original_start()

    with supervisor._lifecycle_lock:
        monkeypatch.setattr(tunnel, "start", transient_start)
        tunnel.status_value = dead_tunnel()
    assert failed_attempt.wait(2.0)
    wait_until(lambda: supervisor.state == "degraded")
    assert worker is not None and worker.is_alive()
    permit_recovery.set()

    assert tunnel.reconnected.wait(2.0)
    wait_until(lambda: supervisor.state == "running")
    assert supervisor._reconnect_thread is worker
    assert tunnel.started == 1
