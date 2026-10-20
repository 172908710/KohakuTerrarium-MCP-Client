"""Tests for process identity and safe stop behavior."""

import json
import subprocess
import sys
import time

import pytest

from kt_mcp_client.process import ManagedProcess, ProcessError, pid_identity_matches


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
