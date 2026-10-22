"""Cache upgrades keep device identity; tests never touch a real installation."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module():
    spec = importlib.util.spec_from_file_location("upgrade_launcher", ROOT / "scripts/single_exe_launcher.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def installation(tmp_path, monkeypatch):
    launcher = module()
    target = tmp_path / "新客户 with spaces" / "app"
    target.mkdir(parents=True)
    for name in launcher.REQUIRED_PROGRAM_FILES:
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("old:" + name).encode())
    (target / launcher.MARKER_NAME).write_text("a" * 64 + "\n")
    state = {
        "data/profile.json": b'{"synthetic": "encrypted-not-an-actual-token"}',
        "data/installation_id": b"test-installation-identity",
        "data/device-ssh/device_ed25519": b"synthetic-private-key-do-not-replace",
        "data/device-ssh/known_hosts": b"synthetic-pinned-host-key",
        "data/kt-home/endpoint.json": b"synthetic-endpoint-state",
        "workspace/customer.txt": b"customer-work",
        "notes.txt": b"not-program-data",
    }
    for name, content in state.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (target / "src/removed_in_new_version.py").write_text("old-only")
    payload = tmp_path / "new.zip"
    with zipfile.ZipFile(payload, "w") as archive:
        for name in launcher.REQUIRED_PROGRAM_FILES:
            archive.writestr(launcher.APP_DIR_NAME + "/" + name, ("new:" + name).encode())
        archive.writestr(launcher.APP_DIR_NAME + "/scripts/new.py", b"new component")
    monkeypatch.setattr(launcher, "_cache_root", lambda: target)
    monkeypatch.setattr(launcher, "_payload_path", lambda: payload)
    monkeypatch.setattr(launcher, "_cache_process_ids", lambda path: [])
    return launcher, target, payload, state


def test_upgrade_existing_profile_keeps_every_state_byte_and_absolute_root(installation):
    launcher, target, payload, state = installation
    assert launcher._install_payload() == target
    for name, data in state.items():
        assert (target / name).read_bytes() == data
    for name in launcher.REQUIRED_PROGRAM_FILES:
        assert (target / name).read_bytes() == ("new:" + name).encode()
    assert not (target / "src/removed_in_new_version.py").exists()
    backups = list(target.parent.glob(".app.program-backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "src/removed_in_new_version.py").read_text() == "old-only"
    assert not (backups[0] / "data").exists() and not (backups[0] / "workspace").exists()
    assert launcher._is_complete_install(target, launcher._payload_digest(payload))
    assert not launcher._journal_path(target).exists()


def test_same_version_fast_path_does_not_require_process_exit(installation, monkeypatch):
    launcher, target, _, _ = installation
    launcher._install_payload()
    monkeypatch.setattr(launcher, "_cache_process_ids", lambda _: pytest.fail("No update needed"))
    assert launcher._install_payload() == target


def test_running_old_version_is_not_killed_or_modified(installation, monkeypatch):
    launcher, target, _, _ = installation
    before = snapshot(target)
    monkeypatch.setattr(launcher, "_cache_process_ids", lambda _: [12345])
    with pytest.raises(RuntimeError, match="托盘"):
        launcher._install_payload()
    assert snapshot(target) == before
    assert not list(target.parent.glob(".app.program-backup-*"))


def test_unknown_process_state_is_not_assumed_stopped(installation, monkeypatch):
    launcher, target, _, _ = installation
    before = snapshot(target)
    def failed(_):
        raise RuntimeError("inspection unavailable")
    monkeypatch.setattr(launcher, "_cache_process_ids", failed)
    with pytest.raises(RuntimeError, match="inspection"):
        launcher._install_payload()
    assert snapshot(target) == before


def test_same_digest_but_missing_program_is_repaired(installation):
    launcher, target, payload, state = installation
    (target / launcher.MARKER_NAME).write_text(launcher._payload_digest(payload))
    (target / "src/kt_mcp_client/portable.py").unlink()
    assert launcher._install_payload() == target
    assert (target / "src/kt_mcp_client/portable.py").read_bytes().startswith(b"new:")
    assert (target / "data/profile.json").read_bytes() == state["data/profile.json"]


def test_mid_swap_failure_restores_program_and_marker(installation, monkeypatch):
    launcher, target, _, _ = installation
    before = snapshot(target)
    original = os.replace
    def replace(src, dst):
        if Path(src).name == "src" and Path(src).parent.name == launcher.APP_DIR_NAME:
            raise PermissionError("synthetic locked program")
        return original(src, dst)
    monkeypatch.setattr(launcher.os, "replace", replace)
    with pytest.raises(RuntimeError, match="已恢复"):
        launcher._install_payload()
    assert snapshot(target) == before
    assert not launcher._journal_path(target).exists()


def test_marker_failure_also_restores_all_program_files(installation, monkeypatch):
    launcher, target, _, _ = installation
    before = snapshot(target)
    def failed(*args):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(launcher, "_write_marker", failed)
    with pytest.raises(RuntimeError, match="已恢复"):
        launcher._install_payload()
    assert snapshot(target) == before


def test_interrupted_recovery_retries_before_next_install(installation, monkeypatch):
    launcher, target, _, state = installation
    before = snapshot(target)
    original_marker, original_recover = launcher._write_marker, launcher._recover_update
    def marker_fail(*args):
        raise OSError("synthetic interruption")
    def recover_fail(path):
        if launcher._journal_path(path).exists():
            raise OSError("synthetic recovery interruption")
    monkeypatch.setattr(launcher, "_write_marker", marker_fail)
    monkeypatch.setattr(launcher, "_recover_update", recover_fail)
    with pytest.raises(RuntimeError, match="恢复尚未完成"):
        launcher._install_payload()
    assert launcher._journal_path(target).exists()
    monkeypatch.setattr(launcher, "_recover_update", original_recover)
    monkeypatch.setattr(launcher, "_write_marker", original_marker)
    launcher._recover_update(target)
    assert snapshot(target) == before
    launcher._install_payload()
    assert all((target / name).read_bytes() == data for name, data in state.items())


def test_same_digest_repair_does_not_treat_uncommitted_marker_as_success(installation, monkeypatch):
    launcher, target, payload, _ = installation
    digest = launcher._payload_digest(payload)
    (target / launcher.MARKER_NAME).write_text(digest + "\n")
    (target / "src/kt_mcp_client/portable.py").unlink()
    before = snapshot(target)
    original = launcher._write_journal
    def journal(path, record):
        if record["phase"] == "committed":
            raise OSError("commit interrupted after writing version marker")
        original(path, record)
    monkeypatch.setattr(launcher, "_write_journal", journal)
    with pytest.raises(RuntimeError, match="已恢复"):
        launcher._install_payload()
    assert snapshot(target) == before


@pytest.mark.parametrize("bad_path", [
    "data/profile.json", "workspace/customer.txt", "../escape", "src/../../escape",
    "src/app.py:alternate", "src /file.py", "src/dir./file.py", "src\\file.py",
])
def test_archive_cannot_write_device_data_or_escape_stage(installation, tmp_path, bad_path):
    launcher, target, payload, _ = installation
    before = snapshot(target)
    with zipfile.ZipFile(payload, "a") as archive:
        info = zipfile.ZipInfo()
        # Assign after construction so Windows ZipInfo does not normalize the
        # deliberately invalid backslash before writing this test archive.
        info.filename = launcher.APP_DIR_NAME + "/" + bad_path
        archive.writestr(info, "untrusted")
    with pytest.raises(RuntimeError, match="unsafe"):
        launcher._install_payload()
    assert snapshot(target) == before


def test_forged_recovery_cannot_remove_data(installation):
    launcher, target, _, _ = installation
    before = snapshot(target)
    backup = target.parent / (".app.program-backup-" + "a" * 32)
    backup.mkdir()
    record = {"version": 1, "phase": "swapping", "digest": "b" * 64,
              "backup": backup.name, "entries": [{"name": "data", "had_old": False}]}
    launcher._write_journal(target, record)
    with pytest.raises(RuntimeError, match="无效"):
        launcher._recover_update(target)
    assert snapshot(target) == before


def test_blank_cache_initializes_without_inheriting_other_device_state(installation):
    launcher, target, _, _ = installation
    # Use a separate pristine target, not deletion of the synthetic old profile.
    new_target = target.parent / "fresh"
    launcher._cache_root = lambda: new_target
    assert launcher._install_payload() == new_target
    assert not (new_target / "data").exists()
    assert (target / "data/profile.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows process inspection")
def test_real_process_probe_detects_only_owned_synthetic_worker(tmp_path):
    launcher = module()
    target = tmp_path / "客户缓存 with spaces"
    target.mkdir()
    script = target / "synthetic_worker.py"
    script.write_text("import time; time.sleep(60)")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KT_", "PYTHON"))}
    child = subprocess.Popen([sys.executable, "-B", str(script)], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert child.pid in launcher._cache_process_ids(target)
        with pytest.raises(RuntimeError, match="仍在运行"):
            launcher._require_stopped_cache(target)
    finally:
        child.terminate()
        child.wait(timeout=10)
    assert child.pid not in launcher._cache_process_ids(target)
