"""Safe subprocess ownership and persistent process identity."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence


PROVISION_TOKEN_ENV = "KT_MCP_PROVISION_TOKEN"
STUDIO_CONTROL_CONFIG_ENV = "KT_STUDIO_CONTROL_CONFIG"


def child_environment(
    env: Mapping[str, str] | None = None,
    *,
    python_path: str | None = None,
    studio_control_config: str | None = None,
) -> dict[str, str]:
    """Build a child environment without bearer-token leakage.

    The optional extension values are set explicitly by the independent KT
    runtime.  They are not inherited from the user's shell, which prevents a
    production Bridge ``sitecustomize`` or control configuration from leaking
    into this process.
    """

    child = dict(os.environ if env is None else env)
    child.pop(PROVISION_TOKEN_ENV, None)
    # Never inherit a caller's Studio hook (especially the production Bridge
    # path). The independent runtime sets its own config explicitly below.
    child.pop(STUDIO_CONTROL_CONFIG_ENV, None)
    if python_path is not None:
        child["PYTHONPATH"] = str(python_path)
    if studio_control_config is not None:
        child[STUDIO_CONTROL_CONFIG_ENV] = str(studio_control_config)
    return child


class ProcessError(RuntimeError):
    """Raised when a managed process cannot be safely manipulated."""


def redact(value: object, secrets: Iterable[str] = ()) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def _command_text(command: Sequence[object], secrets: Iterable[str] = ()) -> str:
    return redact(" ".join(str(part) for part in command), secrets)


def _safe_json_write(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _run_no_window(command: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            list(command),
            capture_output=True,
            env=child_environment(),
            text=True,
            timeout=3,
            check=False,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _read_proc_cmdline(pid: int) -> str | None:
    """Read a command line without a shell and with Windows fallbacks."""

    if pid <= 0:
        return None
    if os.name != "nt":
        proc_file = Path("/proc") / str(pid) / "cmdline"
        try:
            return proc_file.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            return None

    # WMIC is removed from newer Windows releases.  Prefer CIM through a
    # no-profile, non-interactive PowerShell process, then use WMIC as a
    # compatibility fallback.  Neither command receives user input in a shell.
    powershell_script = (
        "$p=Get-CimInstance Win32_Process -Filter 'ProcessId=%d';"
        "if ($null -ne $p) { [Console]::Write($p.CommandLine) }" % pid
    )
    for executable in ("powershell.exe", "pwsh.exe"):
        result = _run_no_window(
            [executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", powershell_script]
        )
        if result and result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()

    result = _run_no_window(["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine", "/value"])
    if result:
        for line in (result.stdout or "").splitlines():
            if line.lower().startswith("commandline="):
                value = line.split("=", 1)[1].strip()
                if value:
                    return value
    # tasklist cannot prove arguments, so it is intentionally not treated as a
    # successful identity lookup.  A PID with no command line is never killed.
    return None


def pid_identity_matches(pid: int, identity_tokens: Sequence[str], executable: str = "") -> bool:
    """Prove that a PID belongs to the exact command recorded by this client.

    Empty identity tokens are refused.  This prevents a stale PID from being
    treated as ours merely because it is alive, including PID reuse.
    """

    if pid <= 0 or not identity_tokens:
        return False
    command_line = _read_proc_cmdline(pid)
    if not command_line:
        return False
    lowered = command_line.casefold()
    if executable:
        executable_name = Path(executable).name.casefold()
        if executable_name and executable_name not in lowered:
            return False
    return all(str(token) and str(token).casefold() in lowered for token in identity_tokens)


@dataclass(frozen=True)
class ProcessStatus:
    name: str
    running: bool
    pid: int | None
    identity_ok: bool
    returncode: int | None = None


class ManagedProcess:
    """Own one subprocess and stop it only after a complete identity check."""

    def __init__(
        self,
        name: str,
        log_path: str | os.PathLike[str],
        *,
        record_path: str | os.PathLike[str] | None = None,
        secrets: Iterable[str] = (),
    ) -> None:
        self.name = str(name)
        self.log_path = Path(log_path)
        self.record_path = Path(record_path) if record_path else self.log_path.with_suffix(".pid.json")
        self._secrets = tuple(secret for secret in secrets if secret)
        self._popen: subprocess.Popen[bytes] | None = None
        self._log_handle = None
        self._log_thread: threading.Thread | None = None
        self._log_lock = threading.Lock()
        self._log_pending = ""
        self._identity_tokens: tuple[str, ...] = ()
        self._executable = ""
        self._command_hash = ""
        self._restore_record_identity()

    @property
    def pid(self) -> int | None:
        if self._popen is not None:
            return self._popen.pid
        record = self._read_record()
        return int(record["pid"]) if record and str(record.get("pid", "")).isdigit() else None

    @property
    def popen(self) -> subprocess.Popen[bytes] | None:
        return self._popen

    def _read_record(self) -> dict[str, object] | None:
        try:
            with self.record_path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _restore_record_identity(self) -> None:
        record = self._read_record()
        if not record:
            return
        executable = record.get("executable")
        tokens = record.get("identity_tokens")
        if isinstance(executable, str) and executable:
            self._executable = executable
        if isinstance(tokens, list) and tokens:
            values = tuple(str(token) for token in tokens if str(token))
            if values:
                self._identity_tokens = values

    def _write_record(self, pid: int) -> None:
        if not self._executable or not self._identity_tokens:
            raise ProcessError(f"{self.name}: 不完整的进程身份，拒绝持久化")
        if any(secret and secret in "\x00".join(self._identity_tokens) for secret in self._secrets):
            raise ProcessError(f"{self.name}: 拒绝将 secret 写入身份记录")
        _safe_json_write(
            self.record_path,
            {
                "name": self.name,
                "pid": pid,
                "executable": self._executable,
                "identity_hash": self._command_hash,
                "identity_tokens": list(self._identity_tokens),
            },
        )

    def _remove_record(self) -> None:
        try:
            self.record_path.unlink()
        except OSError:
            pass

    def is_running(self) -> bool:
        return self._popen is not None and self._popen.poll() is None

    def _identity_for(self, pid: int, record: Mapping[str, object] | None = None) -> tuple[tuple[str, ...], str]:
        tokens = self._identity_tokens
        executable = self._executable
        if record:
            raw_tokens = record.get("identity_tokens")
            if not tokens and isinstance(raw_tokens, list):
                tokens = tuple(str(token) for token in raw_tokens if str(token))
            raw_executable = record.get("executable")
            if not executable and isinstance(raw_executable, str):
                executable = raw_executable
        return tokens, executable

    def identity_ok(self) -> bool:
        if self._popen is not None:
            if self._popen.poll() is not None:
                return False
            tokens, executable = self._identity_for(self._popen.pid)
            return pid_identity_matches(self._popen.pid, tokens, executable)
        self._restore_record_identity()
        record = self._read_record()
        if not record:
            return False
        try:
            pid = int(record["pid"])
        except (KeyError, TypeError, ValueError):
            return False
        tokens, executable = self._identity_for(pid, record)
        return pid_identity_matches(pid, tokens, executable)

    def status(self) -> ProcessStatus:
        if self._popen is not None:
            returncode = self._popen.poll()
            return ProcessStatus(self.name, returncode is None, self._popen.pid, self.identity_ok(), returncode)
        self._restore_record_identity()
        record = self._read_record()
        if not record:
            return ProcessStatus(self.name, False, None, False, None)
        try:
            pid = int(record["pid"])
        except (KeyError, TypeError, ValueError):
            return ProcessStatus(self.name, False, None, False, None)
        tokens, executable = self._identity_for(pid, record)
        identity = pid_identity_matches(pid, tokens, executable)
        return ProcessStatus(self.name, identity, pid, identity, None)

    def start(
        self,
        command: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        identity_tokens: Sequence[str] | None = None,
    ) -> ProcessStatus:
        if self.is_running():
            return self.status()
        persisted = self.status()
        if persisted.running and persisted.identity_ok:
            return persisted
        if not command:
            raise ProcessError(f"{self.name}: 启动命令为空")
        command_list = [os.fspath(part) for part in command]
        command_string = _command_text(command_list, self._secrets)
        joined = "\x00".join(command_list)
        for secret in self._secrets:
            if secret and secret in joined:
                raise ProcessError(f"{self.name}: 拒绝将 secret 放入命令行")
        tokens = tuple(str(token) for token in (identity_tokens if identity_tokens is not None else command_list[1:]) if str(token))
        if not tokens:
            raise ProcessError(f"{self.name}: 启动命令没有可验证的身份 token")
        if any(secret and secret in "\x00".join(tokens) for secret in self._secrets):
            raise ProcessError(f"{self.name}: 拒绝将 secret 写入身份记录")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_handle = self.log_path.open("ab", buffering=0)
        header = f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] start {command_string}\n"
        self._log_handle.write(header.encode("utf-8", "replace"))
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            self._popen = subprocess.Popen(
                command_list,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=os.fspath(cwd) if cwd else None,
                env=child_environment(env),
                shell=False,
                creationflags=creationflags,
                bufsize=0,
            )
        except OSError as exc:
            self._close_log()
            raise ProcessError(f"{self.name}: 启动失败：{exc}") from exc
        self._executable = command_list[0]
        self._identity_tokens = tokens
        digest_source = "\x00".join(command_list).encode("utf-8", "surrogatepass")
        self._command_hash = hashlib.sha256(digest_source).hexdigest()
        try:
            self._write_record(self._popen.pid)
        except Exception:
            try:
                self._popen.kill()
            except OSError:
                pass
            self._close_log()
            self._popen = None
            raise
        self._log_thread = threading.Thread(
            target=self._pump_output,
            args=(self._popen.stdout,),
            name=f"{self.name}-log",
            daemon=True,
        )
        self._log_thread.start()
        return self.status()

    def _pump_output(self, pipe: object) -> None:
        try:
            while True:
                data = pipe.read(4096)  # type: ignore[attr-defined]
                if not data:
                    break
                text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
                self._write_redacted(text, final=False)
            self._write_redacted("", final=True)
        except (OSError, ValueError):
            pass
        finally:
            try:
                pipe.close()  # type: ignore[attr-defined]
            except (OSError, ValueError, AttributeError):
                pass

    def _write_redacted(self, text: str, *, final: bool) -> None:
        with self._log_lock:
            if self._log_handle is None:
                return
            combined = self._log_pending + text
            if final:
                output = redact(combined, self._secrets)
                self._log_pending = ""
            else:
                hold = max((len(secret) for secret in self._secrets), default=1) - 1
                if hold and len(combined) <= hold:
                    self._log_pending = combined
                    return
                output = combined[:-hold] if hold else combined
                self._log_pending = combined[-hold:] if hold else ""
                output = redact(output, self._secrets)
            if output:
                self._log_handle.write(output.encode("utf-8", "replace"))

    def stop(self, *, timeout: float = 8.0, allow_force: bool = True) -> bool:
        """Stop only our process after re-checking the complete identity."""

        process = self._popen
        if process is None:
            self._restore_record_identity()
            record = self._read_record()
            if not record:
                return False
            try:
                pid = int(record["pid"])
            except (KeyError, TypeError, ValueError):
                return False
            tokens, executable = self._identity_for(pid, record)
            if not pid_identity_matches(pid, tokens, executable):
                return False
            self._terminate_pid(pid, identity_tokens=tokens, executable=executable, timeout=timeout, allow_force=allow_force)
            self._remove_record()
            return True
        if process.poll() is not None:
            self._close_log()
            self._remove_record()
            self._popen = None
            return False
        tokens, executable = self._identity_for(process.pid)
        if not pid_identity_matches(process.pid, tokens, executable):
            raise ProcessError(f"{self.name}: PID 身份校验失败，拒绝停止")
        try:
            if os.name != "nt":
                process.send_signal(signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=max(0.0, timeout))
        except subprocess.TimeoutExpired:
            if not allow_force:
                raise ProcessError(f"{self.name}: 停止超时")
            if not pid_identity_matches(process.pid, tokens, executable):
                raise ProcessError(f"{self.name}: 强制停止前 PID 身份已变化")
            process.kill()
            process.wait(timeout=3)
        finally:
            self._close_log()
            self._remove_record()
            self._popen = None
        return True

    @staticmethod
    def _terminate_pid(
        pid: int,
        *,
        identity_tokens: Sequence[str],
        executable: str,
        timeout: float,
        allow_force: bool,
    ) -> None:
        if not identity_tokens or not executable:
            raise ProcessError(f"PID {pid}: 缺少完整身份，拒绝停止")
        if not pid_identity_matches(pid, identity_tokens, executable):
            raise ProcessError(f"PID {pid}: 身份校验失败，拒绝停止")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if not pid_identity_matches(pid, identity_tokens, executable):
                return
            time.sleep(0.05)
        if not allow_force:
            raise ProcessError(f"PID {pid}: 停止超时")
        # Re-check immediately before force termination.  Never use empty
        # tokens: PID reuse must fail closed.
        if not pid_identity_matches(pid, identity_tokens, executable):
            return
        if os.name == "nt":  # pragma: no cover - Windows-only branch
            result = _run_no_window(["taskkill", "/PID", str(pid), "/T", "/F"])
            # taskkill only receives a PID; the identity check immediately
            # before it is the proof that this PID is still ours.  If the
            # command reports failure, fail closed rather than treating a
            # liveness-only result as success.
            if result is None or result.returncode != 0:
                raise ProcessError(f"PID {pid}: 强制停止失败")
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                return

    def _close_log(self) -> None:
        thread = self._log_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._log_thread = None
        with self._log_lock:
            if self._log_handle is not None:
                if self._log_pending:
                    self._log_handle.write(redact(self._log_pending, self._secrets).encode("utf-8", "replace"))
                    self._log_pending = ""
                try:
                    self._log_handle.close()
                except OSError:
                    pass
                self._log_handle = None

    def __enter__(self) -> "ManagedProcess":
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


__all__ = ["ManagedProcess", "ProcessError", "ProcessStatus", "child_environment", "pid_identity_matches", "redact"]
