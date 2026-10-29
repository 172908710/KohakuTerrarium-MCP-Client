"""Coordinate independent KT and SSH lifecycles."""

from __future__ import annotations

import json
import os
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import DeviceProfile, canonicalize_runtime_root, public_dict, redact_sensitive, validate
from .kt_runtime import KTRuntime, KTRuntimeError, RuntimeStatus
from .process import ProcessError, ProcessStatus
from .ssh_tunnel import SSHTunnel, SSHTunnelError, TunnelStatus


class SupervisorError(RuntimeError):
    """Raised when the local client cannot safely transition state."""


STATES = {"stopped", "starting", "running", "degraded", "error"}


@dataclass(frozen=True)
class SupervisorStatus:
    state: str
    profile: dict[str, Any]
    runtime: dict[str, Any]
    tunnel: dict[str, Any]
    detail: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "profile": self.profile,
            "runtime": self.runtime,
            "tunnel": self.tunnel,
            "detail": self.detail,
        }


class Supervisor:
    """Coordinate KT -> SSH startup and SSH -> KT shutdown.

    Lifecycle commands are serialized separately from health probes so a
    cancelled monitor can be joined without holding the lifecycle lock.  Its
    stop event is checked again immediately before every reconnect attempt.
    Only explicit start()/monitor() calls acquire monitoring ownership.
    """

    def __init__(
        self,
        profile: DeviceProfile,
        runtime_root: str | os.PathLike[str],
        *,
        runtime: KTRuntime | None = None,
        tunnel: SSHTunnel | None = None,
        reconnect: bool | None = None,
    ) -> None:
        self.profile = profile
        self.runtime_root = canonicalize_runtime_root(runtime_root)
        # ``runtime_root`` is the independent config-root child shared with
        # the explicit provisioning flow. Keep lifecycle state, logs and PID
        # records in that same directory so a later CLI/GUI launch observes the
        # processes started by the previous one instead of a nested duplicate.
        self.state_dir = self.runtime_root / "state"
        self.log_dir = self.runtime_root / "logs"
        self.runtime_dir = self.runtime_root
        for directory in (self.state_dir, self.log_dir, self.runtime_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.runtime = runtime or KTRuntime(
            profile, self.runtime_dir, config_root=self.runtime_root.parent
        )
        self.tunnel = tunnel or SSHTunnel(profile, self.runtime_dir)
        self.reconnect = profile.auto_reconnect if reconnect is None else bool(reconnect)
        self._state = "stopped"
        self._detail = ""
        self._fresh_state = True
        self._active_start = False
        self._writing_state = False
        self._reconnect_stop = threading.Event()
        self._reconnect_stop.set()
        # The monitor never takes this command lock.  A retiring worker may
        # still need _lifecycle_lock, so joins must happen outside that lock.
        self._command_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        # Cancel and the final reconnect check/start share a small gate.  A
        # health probe does not hold it, so stop can cancel a slow probe before
        # it decides to start SSH.  An already-starting SSH is joined first.
        self._reconnect_gate = threading.Lock()
        self._reconnect_thread: threading.Thread | None = None
        self._reconnect_interval = 5.0
        self._load_persisted_state()
        # The initial snapshot is written after loading the previous lifecycle
        # marker.  This avoids clobbering a running/degraded/error state before
        # the first status probe in a new CLI process.
        self._initialized = True
        # A fresh instance must not create a sticky ``stopped`` marker merely
        # by answering ``--status``.  Its first health probe is derived from
        # the live processes; explicit start/stop writes the first marker.
        if not self._fresh_state:
            self._write_state()

    @property
    def state(self) -> str:
        return self._state

    def _load_persisted_state(self) -> None:
        """Restore only the lifecycle marker, never cached health details.

        A missing/invalid marker is a fresh supervisor: its live health is
        derived on each probe but not persisted until an explicit lifecycle
        command.  Once a marker exists, ``stopped`` and ``error`` remain
        sticky until an explicit lifecycle command; running and degraded
        states are re-probed below.
        """

        path = self.state_dir / "status.json"
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        state = payload.get("state")
        if state not in STATES:
            return
        self._state = state
        detail = payload.get("detail", "")
        self._detail = detail if isinstance(detail, str) else str(detail)
        self._fresh_state = False

    def _write_state(self, snapshot: SupervisorStatus | None = None) -> None:
        # A fresh health probe is intentionally ephemeral.  Only an explicit
        # lifecycle command establishes a persisted ownership marker.
        if self._fresh_state:
            return
        # status() persists transitions itself.  The guard prevents the
        # write-triggered status probe from recursively writing again.
        if self._writing_state:
            return
        self._writing_state = True
        try:
            payload = (snapshot if snapshot is not None else self.status()).public_dict()
            path = self.state_dir / "status.json"
            temporary = path.with_name("." + path.name + ".tmp")
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            except Exception:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                raise
        finally:
            self._writing_state = False

    @staticmethod
    def _runtime_dict(value: RuntimeStatus) -> dict[str, Any]:
        return {
            "process": {
                "name": value.process.name,
                "running": value.process.running,
                "pid": value.process.pid,
                "identity_ok": value.process.identity_ok,
                "returncode": value.process.returncode,
            },
            "cli_status": value.cli_status,
            "cli_running": value.cli_running,
            "local_ready": getattr(value, "local_ready", False),
            "public_ready": getattr(value, "public_ready", False),
            "detail": value.detail,
        }

    @staticmethod
    def _tunnel_dict(value: TunnelStatus) -> dict[str, Any]:
        return {
            "process": {
                "name": value.process.name,
                "running": value.process.running,
                "pid": value.process.pid,
                "identity_ok": value.process.identity_ok,
                "returncode": value.process.returncode,
            },
            "configured": value.configured,
            "detail": value.detail,
        }

    def _safe_detail(self, value: object) -> str:
        return redact_sensitive(
            str(value).replace(self.profile.secret, "<redacted>")
            if self.profile.secret
            else str(value)
        )

    @staticmethod
    def _runtime_ready(value: RuntimeStatus) -> bool:
        # RuntimeStatus.cli_status is deliberately stricter than a process bit:
        # KTRuntime only emits ``ready`` after the CLI JSON has confirmed state
        # ready plus both local_ready and public_ready.
        return value.cli_status == "ready"

    @staticmethod
    def _runtime_local_ready(value: RuntimeStatus) -> bool:
        """Whether the local KT listener can safely receive an SSH forward.

        A normal first start has ``local_ready=True`` and ``public_ready=False``
        because the reverse tunnel is what makes the public probe succeed.  Do
        not gate tunnel bootstrap on the stricter public ``ready`` state, but
        still refuse to forward to failed/stopped/unknown CLI states.
        """

        return (
            value.cli_status == "ready"
            or (
                value.cli_status == "degraded"
                and getattr(value, "local_ready", False)
            )
        )

    @staticmethod
    def _tunnel_ready(value: TunnelStatus) -> bool:
        return (
            value.configured
            and value.process.running
            and value.process.identity_ok
        )

    def _health_details(
        self, runtime_status: RuntimeStatus, tunnel_status: TunnelStatus
    ) -> list[str]:
        details: list[str] = []
        if not self._runtime_ready(runtime_status):
            if self._runtime_local_ready(runtime_status):
                message = "KT CLI 未 ready；本地 KT 已监听，公网 endpoint 尚未 ready"
            else:
                message = "KT CLI 未 ready；本地 KT 或公网 endpoint 不可视为运行"
            details.append(message + (f"：{runtime_status.detail}" if runtime_status.detail else ""))
        if not tunnel_status.configured:
            details.append("SSH 未配置；公网不可用")
        elif not tunnel_status.process.running:
            details.append("SSH 隧道进程未运行；公网不可用")
        elif not tunnel_status.process.identity_ok:
            details.append("SSH 隧道进程身份校验失败；公网不可用")
        return details

    def status(self) -> SupervisorStatus:
        # Keep health transitions and reconnect-thread decisions atomic with
        # start/stop.  RLock also permits _write_state() to probe recursively.
        with self._lifecycle_lock:
            return self._status_unlocked()

    def _probe_runtime(self) -> RuntimeStatus:
        try:
            return self.runtime.status()
        except Exception as exc:
            try:
                process_status = self.runtime.process.status()
            except Exception:
                process_status = ProcessStatus("kt-runtime", False, None, False, None)
            return RuntimeStatus(
                process=process_status,
                cli_status="error",
                detail=self._safe_detail(exc),
                cli_running=False,
            )

    def _probe_tunnel(self) -> TunnelStatus:
        try:
            return self.tunnel.status()
        except Exception as exc:
            try:
                process_status = self.tunnel.process.status()
            except Exception:
                process_status = ProcessStatus("ssh-tunnel", False, None, False, None)
            # A failed probe cannot prove configuration or process identity;
            # wait for a successful probe before attempting a reconnect.
            return TunnelStatus(process_status, False, self._safe_detail(exc))

    def _snapshot(
        self, runtime_status: RuntimeStatus, tunnel_status: TunnelStatus
    ) -> SupervisorStatus:
        return SupervisorStatus(
            self._state,
            public_dict(self.profile),
            self._runtime_dict(runtime_status),
            self._tunnel_dict(tunnel_status),
            self._safe_detail(self._detail),
        )

    def _status_unlocked(self) -> SupervisorStatus:
        return self._status_from_health(self._probe_runtime(), self._probe_tunnel())

    def _status_from_health(
        self,
        runtime_status: RuntimeStatus,
        tunnel_status: TunnelStatus,
        *,
        adopt: bool = False,
    ) -> SupervisorStatus:

        # Never trust a previously observed ``running`` bit.  A stopped
        # supervisor remains stopped by contract, an explicit startup phase
        # remains starting while healthy, and an error remains sticky until an
        # explicit start/restart; health probes cannot perform lifecycle
        # transitions on their own.
        previous_state = self._state
        previous_detail = self._detail
        state = previous_state
        health_details = self._health_details(runtime_status, tunnel_status)
        observed_runtime = runtime_status.cli_status not in {"stopped", "unknown", "error"}
        observed_tunnel = tunnel_status.process.running or tunnel_status.process.pid is not None

        if adopt or (self._fresh_state and not self._active_start):
            # Explicit adoption and fresh read-only status both derive health
            # from live probes.  Only adoption establishes persisted ownership.
            if health_details:
                state = "degraded" if (observed_runtime or observed_tunnel) else "stopped"
            elif observed_runtime and observed_tunnel:
                state = "running"
            elif observed_runtime or observed_tunnel:
                state = "degraded"
            else:
                state = "stopped"
        elif state in {"stopped", "error"}:
            # Explicit stop/error states are lifecycle decisions, not health
            # observations.  A later probe must not silently restart or clear
            # them; callers use start()/restart() to make that transition.
            pass
        elif state == "starting" and self._active_start:
            if health_details:
                state = "degraded"
        elif health_details:
            state = "degraded" if (observed_runtime or observed_tunnel) else "stopped"
        elif observed_runtime and observed_tunnel:
            state = "running"
        elif observed_runtime or observed_tunnel:
            state = "degraded"
        else:
            state = "stopped"

        self._state = state
        if state not in {"stopped", "error"} and health_details:
            self._detail = "；".join(self._safe_detail(item) for item in health_details)
        elif state == "running":
            self._detail = self._safe_detail(
                tunnel_status.detail or runtime_status.detail or self._detail
            )

        # A status-only observer never acquires lifecycle ownership or starts
        # a competing monitor.  Reuse this observation when persisting instead
        # of recursively launching another pair of health probes.
        snapshot = self._snapshot(runtime_status, tunnel_status)
        changed = state != previous_state or self._detail != previous_detail
        if (
            changed
            and self._initialized
            and not self._fresh_state
            and not self._writing_state
        ):
            self._write_state(snapshot)
        return snapshot

    def _check_port_available(self) -> None:
        port = int(self.profile.local_port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            # ``--provision`` starts the independent KT listener before the
            # follow-up SSH lifecycle command.  If this exact KT home reports
            # a local-ready endpoint, the occupied port is ours; otherwise a
            # foreign process must remain a hard failure.
            try:
                existing = self.runtime.status()
            except Exception:
                existing = None
            if existing is not None and self._runtime_local_ready(existing):
                return
            raise SupervisorError(
                f"本机端口 {port} 已被占用；未启动新客户端"
            ) from exc
        finally:
            sock.close()

    def _check_profile(self) -> list[str]:
        return validate(self.profile, self.runtime_root.parent)

    def monitor(self) -> SupervisorStatus:
        """Adopt the live runtime/tunnel without starting either process.

        This explicit lifecycle command replaces a persisted stop/error marker
        with current health and, when auto-reconnect is enabled, owns one
        continuous monitor even if the pair is already healthy.  Call
        stop_monitoring() on an old owner before adopting its pair elsewhere.
        """

        with self._command_lock:
            if self._reconnect_stop.is_set():
                # A cancelled worker must finish before its event is reused.
                self.stop_monitoring()
            with self._lifecycle_lock:
                self._fresh_state = False
                self._active_start = False
                self._detail = ""
                snapshot = self._status_from_health(
                    self._probe_runtime(), self._probe_tunnel(), adopt=True
                )
                self._write_state(snapshot)
                if self.reconnect:
                    self._reconnect_stop.clear()
                    self._start_reconnect_thread()
                return snapshot

    def stop_monitoring(self) -> None:
        """Cancel and join this owner's monitor, leaving both processes alone.

        Serialize event reuse with start()/monitor(), but never join while
        holding the lifecycle lock: the old worker may be waiting for it.
        """

        with self._command_lock:
            with self._reconnect_gate:
                self._reconnect_stop.set()
            with self._lifecycle_lock:
                thread = self._reconnect_thread
            if thread is threading.current_thread():
                return
            if thread is not None:
                thread.join()
            with self._lifecycle_lock:
                if self._reconnect_thread is thread:
                    self._reconnect_thread = None

    def start(self, *, wait_for_public: bool = True) -> SupervisorStatus:
        """Start KT, then SSH as soon as KT is locally ready.

        The default retains strict CLI readiness semantics.  With
        wait_for_public=False, a locally ready runtime and healthy SSH process
        may return degraded while public readiness settles; configuration,
        local-runtime and tunnel failures still raise SupervisorError.
        """

        with self._command_lock:
            self.stop_monitoring()
            with self._lifecycle_lock:
                runtime_status = None
                if (
                    not self._fresh_state
                    and self._state in {"starting", "running", "degraded"}
                ):
                    existing_runtime = self._probe_runtime()
                    existing_tunnel = self._probe_tunnel()
                    if self._runtime_local_ready(existing_runtime):
                        # A persisted active marker is not proof that SSH
                        # survived a reboot.  Reuse a healthy local listener,
                        # but synchronously bootstrap a missing tunnel below
                        # before returning to the GUI's public-readiness wait.
                        runtime_status = existing_runtime
                        if self._tunnel_ready(existing_tunnel):
                            snapshot = self._status_from_health(
                                existing_runtime, existing_tunnel
                            )
                            if self.reconnect:
                                self._reconnect_stop.clear()
                                self._start_reconnect_thread()
                            if wait_for_public and not self._runtime_ready(existing_runtime):
                                raise SupervisorError(self._detail or "KT 或 SSH 未 ready")
                            return snapshot
                    # An unready/stopped runtime needs the normal explicit
                    # startup too; the old lifecycle marker cannot skip it.
                self._fresh_state = False
                self._active_start = True
                self._state = "starting"
                self._detail = ""
                self._write_state()
                try:
                    errors = self._check_profile()
                    hard_errors = [
                        error
                        for error in errors
                        if not error.startswith("SSH 配置")
                        and not error.startswith("secret 必须")
                    ]
                    if hard_errors:
                        raise SupervisorError("配置校验失败：" + "；".join(hard_errors))
                    self._check_port_available()
                    if runtime_status is None:
                        runtime_status = self.runtime.start(wait_for_public=wait_for_public)
                    tunnel_message = ""
                    tunnel_started = False
                    if self._runtime_local_ready(runtime_status):
                        try:
                            candidate = self.tunnel.start()
                            tunnel_started = True
                            tunnel_message = candidate.detail
                        except (SSHTunnelError, ProcessError) as exc:
                            tunnel_message = (
                                "KT 本地服务已启动，但 SSH 隧道未启动："
                                f"{self._safe_detail(exc)}；公网不可用"
                            )
                        # SSH changes public reachability.  Never decide from
                        # the pre-tunnel runtime observation, nor trust an SSH
                        # process that died during the subsequent KT probe.
                        runtime_status = self._probe_runtime()
                    else:
                        tunnel_message = "KT CLI 未 local_ready；未启动 SSH 隧道"
                    tunnel_status = self._probe_tunnel()
                    runtime_ready = self._runtime_ready(runtime_status)
                    runtime_local_ready = self._runtime_local_ready(runtime_status)
                    tunnel_ready = tunnel_started and self._tunnel_ready(tunnel_status)

                    if runtime_ready and tunnel_ready:
                        self._state = "running"
                        self._detail = self._safe_detail(
                            tunnel_status.detail or tunnel_message or runtime_status.detail
                        )
                    else:
                        self._state = "degraded"
                        messages = self._health_details(runtime_status, tunnel_status)
                        if tunnel_message:
                            messages.append(tunnel_message)
                        if not tunnel_ready and not messages:
                            messages.append("SSH 隧道未 ready；公网不可用")
                        self._detail = "；".join(self._safe_detail(item) for item in messages)
                except Exception as exc:
                    self._active_start = False
                    self._state = "error"
                    self._detail = self._safe_detail(exc)
                    self._write_state()
                    raise SupervisorError(self._detail) from exc
                self._active_start = False
                snapshot = self._snapshot(runtime_status, tunnel_status)
                self._write_state(snapshot)
                if self.reconnect:
                    # Monitor continuously, including healthy initial starts.
                    self._reconnect_stop.clear()
                    self._start_reconnect_thread()
                if not tunnel_ready or not runtime_local_ready or (
                    wait_for_public and not runtime_ready
                ):
                    raise SupervisorError(self._detail or "KT 或 SSH 未 ready")
                return snapshot

    def stop(self) -> SupervisorStatus:
        with self._command_lock:
            # Signal first so a probe already in progress cannot launch a new
            # tunnel after shutdown was requested.  Join before taking the
            # lifecycle lock or stopping either process.
            self.stop_monitoring()
            with self._lifecycle_lock:
                self._fresh_state = False
                self._active_start = False
                tunnel_detail = ""
                runtime_status = None
                try:
                    tunnel_status = self.tunnel.stop()
                    tunnel_detail = tunnel_status.detail
                finally:
                    try:
                        runtime_status = self.runtime.stop()
                    except KTRuntimeError as exc:
                        if not tunnel_detail:
                            tunnel_detail = str(exc)
                self._state = "stopped"
                self._detail = self._safe_detail(tunnel_detail)
                # Both stop methods already return post-stop observations.
                # Avoid launching additional KT status subprocesses.
                if runtime_status is not None:
                    snapshot = self._snapshot(runtime_status, tunnel_status)
                else:
                    # A failed KT stop has no returned observation; retain the
                    # failure contract with one explicit health probe.
                    snapshot = self.status()
                self._write_state(snapshot)
                return snapshot

    def restart(self) -> SupervisorStatus:
        with self._command_lock:
            self.stop()
            return self.start()

    def _start_reconnect_thread(self) -> None:
        # Only an explicit lifecycle command may clear the stop event, after
        # joining its previous worker.  Read-only status probes never get here.
        with self._lifecycle_lock:
            if not self.reconnect or self._reconnect_stop.is_set():
                return
            if self._reconnect_thread and self._reconnect_thread.is_alive():
                return
            self._reconnect_thread = threading.Thread(
                target=self._reconnect_loop, name="kt-mcp-reconnect", daemon=True
            )
            self._reconnect_thread.start()

    def _reconnect_loop(self) -> None:
        while not self._reconnect_stop.wait(self._reconnect_interval):
            with self._lifecycle_lock:
                if self._reconnect_stop.is_set() or not self.reconnect:
                    return
                try:
                    runtime_status = self._probe_runtime()
                    tunnel_status = self._probe_tunnel()
                    if self._reconnect_stop.is_set():
                        return
                    self._status_from_health(runtime_status, tunnel_status, adopt=True)
                    if (
                        self._reconnect_stop.is_set()
                        or not self._runtime_local_ready(runtime_status)
                        or not tunnel_status.configured
                        or self._tunnel_ready(tunnel_status)
                    ):
                        continue
                    try:
                        with self._reconnect_gate:
                            if self._reconnect_stop.is_set():
                                return
                            self.tunnel.start()
                    except Exception as exc:
                        self._state = "degraded"
                        self._detail = "SSH 重连失败；公网不可用：" + self._safe_detail(exc)
                        self._write_state(self._snapshot(runtime_status, tunnel_status))
                        continue
                    if self._reconnect_stop.is_set():
                        return
                    # A reconnected SSH route can change KT public readiness;
                    # sample KT again and then prove SSH is still ours/alive.
                    runtime_status = self._probe_runtime()
                    tunnel_status = self._probe_tunnel()
                    if self._reconnect_stop.is_set():
                        return
                    self._status_from_health(runtime_status, tunnel_status, adopt=True)
                except Exception as exc:
                    # Transient probes (including a failed process fallback)
                    # or state-file I/O must not silently kill the monitor.
                    self._state = "degraded"
                    self._detail = "健康检查暂时失败：" + self._safe_detail(exc)


__all__ = ["STATES", "Supervisor", "SupervisorError", "SupervisorStatus"]
