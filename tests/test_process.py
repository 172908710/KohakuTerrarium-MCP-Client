"""Tests for process identity and safe stop behavior."""

import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from kt_mcp_client.process import ManagedProcess, ProcessError, pid_identity_matches
from kt_mcp_client import process as process_module


@pytest.fixture
def windows_psutil(monkeypatch):
    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    def unexpected_process(pid):
        pytest.fail("Synthetic process lookup was not configured")

    fake = SimpleNamespace(
        Process=unexpected_process, NoSuchProcess=NoSuchProcess, AccessDenied=AccessDenied
    )
    monkeypatch.setattr(process_module, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(process_module, "psutil", fake)
    return fake


def test_windows_cmdline_uses_psutil_with_complete_quoted_arguments(windows_psutil, monkeypatch):
    arguments = [
        r"C:\Bundle With Spaces\ssh.exe", "-N", "-T", "-R",
        "127.0.0.1:18766:127.0.0.1:8865", "-i", r"C:\Key Folder\device_key",
        "user@host", 'argument with "quotes"', "trailing space \\",
    ]
    pids = []

    def lookup(pid):
        pids.append(pid)
        return SimpleNamespace(cmdline=lambda: arguments)

    monkeypatch.setattr(windows_psutil, "Process", lookup)
    monkeypatch.setattr(process_module, "_run_no_window", lambda *a: pytest.fail("Unexpected shell fallback"))

    assert process_module._read_proc_cmdline(123) == subprocess.list2cmdline(arguments)
    assert pids == [123]


@pytest.mark.parametrize("stage", ["lookup", "cmdline"])
def test_windows_exited_pid_skips_all_shell_fallbacks(windows_psutil, monkeypatch, stage):
    def exited(*args):
        raise windows_psutil.NoSuchProcess(123)

    monkeypatch.setattr(
        windows_psutil, "Process",
        exited if stage == "lookup" else lambda pid: SimpleNamespace(cmdline=exited),
    )
    monkeypatch.setattr(process_module, "_run_no_window", lambda *a: pytest.fail("Exited PID must not retry"))

    assert process_module._read_proc_cmdline(123) is None


@pytest.mark.parametrize("failure", ["denied", "unsupported", "oserror", "missing_dependency"])
def test_windows_psutil_failure_retains_cim_fallback(windows_psutil, monkeypatch, failure):
    errors = {
        "denied": windows_psutil.AccessDenied(123),
        "unsupported": NotImplementedError(),
        "oserror": OSError("not available"),
    }

    def failed_cmdline():
        raise errors[failure]

    if failure == "missing_dependency":
        monkeypatch.setattr(process_module, "psutil", None)
    else:
        monkeypatch.setattr(windows_psutil, "Process", lambda pid: SimpleNamespace(cmdline=failed_cmdline))
    commands = []
    expected = r'C:\Bundle\ssh.exe -N -T -R 127.0.0.1:18766:127.0.0.1:8865 user@host'

    def cim(command):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, expected, "")

    monkeypatch.setattr(process_module, "_run_no_window", cim)

    assert process_module._read_proc_cmdline(123) == expected
    assert len(commands) == 1
    assert commands[0][0] == "powershell.exe"
    assert "ProcessId=123" in commands[0][-1]


def test_windows_empty_psutil_cmdline_cannot_prove_identity(windows_psutil, monkeypatch):
    monkeypatch.setattr(windows_psutil, "Process", lambda pid: SimpleNamespace(cmdline=lambda: []))
    monkeypatch.setattr(process_module, "_run_no_window", lambda *a: pytest.fail("Unexpected shell fallback"))

    assert process_module._read_proc_cmdline(123) is None
    assert not pid_identity_matches(123, ("-N", "user@host"), "ssh.exe")


def test_windows_fast_lookup_keeps_full_identity_requirements(windows_psutil, monkeypatch):
    arguments = [r"C:\Bundle\ssh.exe", "-N", "-T", "-R", "127.0.0.1:18766:127.0.0.1:8865", "user@host"]
    monkeypatch.setattr(windows_psutil, "Process", lambda pid: SimpleNamespace(cmdline=lambda: arguments))
    monkeypatch.setattr(process_module, "_run_no_window", lambda *a: pytest.fail("Unexpected shell fallback"))

    assert pid_identity_matches(123, arguments[1:], arguments[0])
    assert not pid_identity_matches(123, (), arguments[0])
    assert not pid_identity_matches(123, arguments[1:], "other.exe")
    assert not pid_identity_matches(123, [*arguments[1:-1], "other@host"], arguments[0])
    assert not pid_identity_matches(123, [*arguments[1:], "127.0.0.1:18765:127.0.0.1:8765"], arguments[0])


def test_empty_identity_tokens_never_match():
    assert not pid_identity_matches(1, (), "")


def test_managed_process_persists_complete_nonsecret_identity_and_stops(tmp_path):
    record = tmp_path / "process.json"
    process = ManagedProcess("fake", tmp_path / "process.log", record_path=record, secrets=("never-secret",))
    status = process.start(
        [sys.executable, "-c", "import time; time.sleep(30)", "ssh", "-N", "-T", "-R", "18766:127.0.0.1:8865", "user@host"],
        identity_tokens=("-c", "time.sleep(30)", "ssh", "-N", "-T", "-R", "18766:127.0.0.1:8865", "user@host"),
    )
    assert status.running
    payload = json.loads(record.read_text(encoding="utf-8"))
    assert set(("executable", "identity_hash", "identity_tokens")).issubset(payload)
    assert "never-secret" not in record.read_text(encoding="utf-8")
    assert "user@host" in payload["identity_tokens"]
    assert "-N" in payload["identity_tokens"] and "-T" in payload["identity_tokens"]
    assert process.stop(timeout=2)


def test_stop_refuses_pid_reuse_or_missing_identity(tmp_path, monkeypatch):
    record = tmp_path / "process.json"
    record.write_text(json.dumps({"pid": 123, "executable": "python", "identity_tokens": ["original-token"]}), encoding="utf-8")
    process = ManagedProcess("fake", tmp_path / "process.log", record_path=record)
    monkeypatch.setattr("kt_mcp_client.process.pid_identity_matches", lambda pid, tokens, executable: False)
    assert not process.stop()
    assert record.exists()


def test_terminate_pid_rejects_empty_identity():
    with pytest.raises(ProcessError):
        ManagedProcess._terminate_pid(1, identity_tokens=(), executable="python", timeout=0, allow_force=False)
