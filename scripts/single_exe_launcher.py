"""Small one-file launcher for the existing portable KT client.

The embedded portable ZIP is extracted to a stable cache, never to PyInstaller's
temporary ``_MEIPASS`` directory.  When the executable is placed beside the
current project on this computer, the existing project root is reused so its
DPAPI-protected profile, SSH key and verified endpoint remain unchanged.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile


PAYLOAD_NAME = "KohakuTerrarium-MCP-Client-Windows-x64-portable.zip"
MARKER_NAME = ".single-exe-payload-sha256"
APP_DIR_NAME = "KohakuTerrarium-MCP-Client"


def _payload_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    candidate = base / "payload" / PAYLOAD_NAME
    if not candidate.is_file():
        raise RuntimeError("embedded portable payload is missing")
    return candidate


def _current_project_candidates() -> list[Path]:
    executable_parent = Path(sys.executable).resolve().parent
    candidates = [
        executable_parent / APP_DIR_NAME,
        executable_parent,
    ]
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        candidate = candidate.resolve(strict=False)
        if candidate in seen:
            continue
        seen.add(candidate)
        if _is_existing_project(candidate):
            result.append(candidate)
    return result


def _is_existing_project(candidate: Path) -> bool:
    """Accept only a complete portable root with its own location marker.

    The marker prevents a single EXE from silently executing an arbitrary
    directory named ``KohakuTerrarium-MCP-Client`` next to the executable.
    A portable installation always writes this marker during first-run setup.
    """

    required = (
        candidate / "launch.py",
        candidate / "runtime" / "pythonw.exe",
        candidate / "runtime" / "python.exe",
        candidate / "data" / "profile.json",
        candidate / "data" / "portable-location.json",
    )
    if not all(path.is_file() for path in required):
        return False
    try:
        marker = json.loads(
            (candidate / "data" / "portable-location.json").read_text(encoding="utf-8")
        )
        marked_root = Path(str(marker["root"])).resolve(strict=False)
    except (KeyError, OSError, TypeError, ValueError, UnicodeError):
        return False
    return marked_root == candidate.resolve(strict=False)


def _cache_root() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        local_appdata = str(Path.home() / "AppData" / "Local")
    return Path(local_appdata) / "KohakuTerrarium" / "MCPClientSingle" / "app"


def _payload_digest(payload: Path) -> str:
    digest = hashlib.sha256()
    with payload.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_complete_install(target: Path, digest: str) -> bool:
    """Verify the marker and files before trusting an extracted payload."""

    marker = target / MARKER_NAME
    required = (
        target / "launch.py",
        target / "runtime" / "pythonw.exe",
        target / "runtime" / "python.exe",
    )
    try:
        marker_digest = marker.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        return False
    return (
        re.fullmatch(r"[0-9a-f]{64}", marker_digest or "") is not None
        and marker_digest == digest
        and all(path.is_file() for path in required)
    )


@contextmanager
def _payload_install_lock(target: Path):
    """Serialize payload extraction without a stale PID marker.

    The lock is a one-byte OS file-range lock. Windows releases ``msvcrt``
    locks automatically when the owning process exits, including crashes;
    POSIX ``flock`` provides the same property for source-level tests. The
    lock file is kept beside the cache, never inside the extracted payload.
    """

    target = target.resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.parent / f".{target.name}.install.lock"
    flags = os.O_RDWR | os.O_CREAT
    fd = os.open(str(lock_path), flags, 0o600)
    handle = os.fdopen(fd, "r+b", buffering=0)
    try:
        # Ensure the one-byte range exists before asking the CRT to lock it.
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:  # pragma: no cover - exercised only by source-level Unix runs
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised only by source-level Unix runs
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError):
            pass
        handle.close()


def _write_marker(path: Path, digest: str) -> None:
    """Atomically publish a complete payload marker."""

    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="ascii", newline="\n") as handle:
            handle.write(digest + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _safe_extract(payload: Path, stage: Path) -> Path:
    with zipfile.ZipFile(payload) as archive:
        for info in archive.infolist():
            # A symlink could escape ``stage`` after extraction even when its
            # archive name itself passes the path traversal check.
            if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
                raise RuntimeError("portable payload contains a symlink")
            target = (stage / info.filename).resolve(strict=False)
            if stage.resolve() not in target.parents:
                raise RuntimeError("portable payload contains an unsafe path")
            archive.extract(info, stage)
    root = stage / APP_DIR_NAME
    if not (root / "launch.py").is_file() or not (root / "runtime" / "pythonw.exe").is_file():
        raise RuntimeError("portable payload is incomplete")
    return root


def _install_payload() -> Path:
    payload = _payload_path()
    digest = _payload_digest(payload)
    target = _cache_root()
    marker = target / MARKER_NAME
    if _is_complete_install(target, digest):
        return target

    target.parent.mkdir(parents=True, exist_ok=True)
    with _payload_install_lock(target):
        # Re-check after acquiring the lock: another launcher may have
        # completed extraction while this process was waiting.
        if _is_complete_install(target, digest):
            return target

        # Never replace a live cached installation.  A fresh executable can be
        # retried after the previous client has been stopped.  The data
        # directory is intentionally retained; this message explains why an
        # update is held.
        if (target / "data" / "profile.json").is_file():
            raise RuntimeError(
                "单 EXE 缓存版本已变化或不完整；为保护现有 data/凭据，未覆盖缓存。"
                f"请先关闭客户端并处理缓存：{target}"
            )

        stage = Path(
            tempfile.mkdtemp(prefix="kt-single-exe-", dir=str(target.parent))
        )
        try:
            source = _safe_extract(payload, stage)
            target.mkdir(parents=True, exist_ok=True)
            for item in source.iterdir():
                destination = target / item.name
                if item.name.casefold() in {"data", "workspace"}:
                    continue
                if item.is_dir():
                    shutil.copytree(item, destination, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, destination)
            # The marker is published only after all required files are present.
            if not all(
                path.is_file()
                for path in (
                    target / "launch.py",
                    target / "runtime" / "pythonw.exe",
                    target / "runtime" / "python.exe",
                )
            ):
                raise RuntimeError("portable payload extraction is incomplete")
            _write_marker(marker, digest)
            return target
        finally:
            shutil.rmtree(stage, ignore_errors=True)


def _redact(text: object) -> str:
    value = str(text)
    value = re.sub(r"https?://[^\s\"'<>]+/mcp/[^\s\"'<>]+", "<redacted-endpoint>", value)
    return re.sub(r"[A-Za-z0-9_-]{43,128}", "<redacted-value>", value)


def _launch(root: Path, *, wait: bool = False) -> int:
    # Diagnostic/lifecycle commands are synchronous and use python.exe so
    # callers can inspect their exit code. A normal double-click remains
    # detached and windowless, preserving the compact GUI startup behavior.
    python = root / "runtime" / ("python.exe" if wait else "pythonw.exe")
    launch = root / "launch.py"
    if not python.is_file() or not launch.is_file():
        raise RuntimeError("portable client files are incomplete")
    command = [str(python), "-I", "-X", "utf8", str(launch), *sys.argv[1:]]
    # Keep inherited stdout/stderr for the synchronous diagnostic mode.  A
    # normal GUI launch is hidden so no console flashes on double-click.
    flags = (
        0
        if wait
        else (getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    )
    if wait:
        result = subprocess.run(
            command,
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=flags,
            check=False,
        )
        if result.stdout and sys.stdout is not None:
            print(result.stdout, end="", file=sys.stdout)
        if result.stderr and sys.stderr is not None:
            print(result.stderr, end="", file=sys.stderr)
        return result.returncode
    process = subprocess.Popen(
        command,
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # Let the extracted client register the outer one-file EXE for
        # Windows logon startup instead of bypassing the payload launcher.
        env={**os.environ, "KT_MCP_LAUNCHER_EXE": str(Path(sys.executable).resolve())},
        creationflags=flags,
        close_fds=True,
    )
    return process.pid


def main() -> int:
    try:
        candidates = _current_project_candidates()
        root = candidates[0] if candidates else _install_payload()
        # Keep the GUI's normal double-click detached, but preserve the
        # synchronous CLI contract for diagnostic/lifecycle commands.
        cli_commands = {"--check", "--status", "--start", "--stop", "--url", "--provision"}
        wait = any(argument in cli_commands for argument in sys.argv[1:])
        result = _launch(root, wait=wait)
        # Detached GUI startup is successful once the child was spawned; do
        # not leak its PID as the launcher's process exit code.
        return result if wait else 0
    except Exception as exc:
        message = "KohakuTerrarium MCP Client 启动失败：" + _redact(exc)
        if os.name == "nt":  # pragma: no cover - exercised by the packaged EXE
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message[:2000], "KT MCP Client", 0x10)
        else:
            print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
