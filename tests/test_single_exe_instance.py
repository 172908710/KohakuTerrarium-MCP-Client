from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys
import textwrap
import time


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "single_exe_launcher.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("single_exe_launcher_test", LAUNCHER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wait_for_line(path: Path, expected: int, timeout: float = 8.0) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            lines = path.read_text(encoding="ascii").splitlines()
            if len(lines) >= expected:
                return lines
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {expected} lines in {path}")


def _worker_script(tmp_path: Path) -> Path:
    worker = tmp_path / "lock-worker.py"
    worker.write_text(
        textwrap.dedent(
            """
            import importlib.util
            from pathlib import Path
            import os
            import sys
            import time

            launcher_path, target_path, log_path, mode, release_path = sys.argv[1:]
            spec = importlib.util.spec_from_file_location("launcher", launcher_path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with module._payload_install_lock(Path(target_path)):
                with Path(log_path).open("a", encoding="ascii") as handle:
                    handle.write("entered\\n")
                    handle.flush()
                if mode == "crash":
                    os._exit(0)
                while not Path(release_path).exists():
                    time.sleep(0.02)
            """
        ),
        encoding="utf-8",
    )
    return worker


def _start_worker(worker: Path, target: Path, log: Path, mode: str, release: Path):
    return subprocess.Popen(
        [sys.executable, str(worker), str(LAUNCHER), str(target), str(log), mode, str(release)],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_payload_install_lock_is_reentrant_after_release(tmp_path):
    launcher = _load_launcher()
    target = tmp_path / "cache" / "app"
    with launcher._payload_install_lock(target):
        assert (target.parent / ".app.install.lock").is_file()
    with launcher._payload_install_lock(target):
        pass


def test_payload_install_lock_serializes_processes(tmp_path):
    worker = _worker_script(tmp_path)
    target = tmp_path / "cache" / "app"
    log = tmp_path / "events.log"
    release = tmp_path / "release"
    first = _start_worker(worker, target, log, "hold", release)
    try:
        _wait_for_line(log, 1)
        second = _start_worker(worker, target, log, "hold", release)
        try:
            time.sleep(0.2)
            assert len(log.read_text(encoding="ascii").splitlines()) == 1
            release.touch()
            assert first.wait(timeout=8) == 0
            assert second.wait(timeout=8) == 0
            assert len(log.read_text(encoding="ascii").splitlines()) == 2
        finally:
            if second.poll() is None:
                second.kill()
                second.wait(timeout=3)
    finally:
        release.touch()
        if first.poll() is None:
            first.kill()
            first.wait(timeout=3)


def test_payload_install_lock_releases_when_process_exits(tmp_path):
    worker = _worker_script(tmp_path)
    target = tmp_path / "cache" / "app"
    log = tmp_path / "events.log"
    release = tmp_path / "release"
    crashing = _start_worker(worker, target, log, "crash", release)
    assert crashing.wait(timeout=8) == 0
    _wait_for_line(log, 1)
    release.touch()
    survivor = _start_worker(worker, target, log, "hold", release)
    try:
        assert survivor.wait(timeout=8) == 0
        assert len(log.read_text(encoding="ascii").splitlines()) == 2
    finally:
        if survivor.poll() is None:
            survivor.kill()
            survivor.wait(timeout=3)
