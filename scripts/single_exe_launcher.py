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
from pathlib import Path, PurePosixPath
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
# Only program components are replaceable. Per-device state and arbitrary user
# files are never part of an upgrade, backup, recovery or cleanup operation.
PROGRAM_COMPONENTS = frozenset({
    "runtime", "tools", "src", "control", "server", "scripts", "tests",
    "launch.py", "Start-KT-MCP-Client.vbs", "Start-KT-MCP-Client.cmd",
    "Check-KT-MCP-Client.cmd", "README.md", "README_中文.md", "README_便携版.md",
    "THIRD-PARTY-NOTICES.md", "pytest.ini", "requirements-runtime.txt",
    "requirements-runtime.lock.txt", "requirements-build.txt",
})
REQUIRED_PROGRAM_FILES = (
    "launch.py", "runtime/python.exe", "runtime/pythonw.exe",
    "runtime/python314._pth", "runtime/Lib/os.py",
    "src/kt_mcp_client/portable.py", "src/kt_mcp_client/gui.py",
    "tools/openssh/ssh.exe",
)


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
    required = tuple(target / name for name in REQUIRED_PROGRAM_FILES)
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
    # Validate the WHOLE archive before writing anything. Credentials never even
    # enter staging, and Windows path/ADS aliases cannot evade the allowlist.
    with zipfile.ZipFile(payload) as archive:
        seen: set[str] = set()
        for info in archive.infolist():
            path = PurePosixPath(info.filename)
            if (path.is_absolute() or len(path.parts) < 2
                or path.parts[0] != APP_DIR_NAME
                or path.parts[1] not in PROGRAM_COMPONENTS
                or "\\" in info.orig_filename
                or any(p in {".", ".."} or ":" in p or p.endswith((" ", ".")) for p in path.parts)
                or stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF)):
                raise RuntimeError("portable payload contains an unsafe or non-program path")
            canonical = str(path).casefold()
            if canonical in seen:
                raise RuntimeError("portable payload contains duplicate paths")
            seen.add(canonical)
        for info in archive.infolist():
            archive.extract(info, stage)
    root = stage / APP_DIR_NAME
    if not all((root / name).is_file() for name in REQUIRED_PROGRAM_FILES):
        raise RuntimeError("portable payload is incomplete")
    return root


def _is_link(path: Path) -> bool:
    try:
        value = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(value.st_mode) or bool(
        getattr(value, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _cache_process_ids(target: Path) -> list[int]:
    """Observe real processes, not stale saved PIDs; never stop anything.

    The standalone launcher has no psutil dependency. Use the installed Windows
    management API, return only PIDs, and fail closed if inspection fails.
    """
    if os.name != "nt":
        raise RuntimeError("缓存升级目前只支持 Windows。")
    shell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = r'''
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($env:KT_CACHE_INSPECT_ROOT).TrimEnd('\')
$prefix = $root + '\'
$ids = @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
    ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) -or
    ($_.Name -match '^(pythonw?|ssh)(\.exe)?$' -and $_.CommandLine -and
     $_.CommandLine.IndexOf($root, [StringComparison]::OrdinalIgnoreCase) -ge 0)
} | ForEach-Object { [int]$_.ProcessId })
[Console]::Write((ConvertTo-Json -Compress -InputObject @($ids)))
'''
    env = {k: v for k, v in os.environ.items() if not k.startswith(("KT_", "PYTHON"))}
    env["KT_CACHE_INSPECT_ROOT"] = str(target.resolve())
    try:
        result = subprocess.run(
            [str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=20, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        value = json.loads(result.stdout) if result.returncode == 0 else None
        if not isinstance(value, list) or any(type(pid) is not int or pid <= 0 for pid in value):
            raise ValueError("invalid process inspection")
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("无法确认旧版是否已退出；未更新缓存，也未修改 data。请退出旧版后重试。") from exc


def _require_stopped_cache(target: Path) -> None:
    if target.exists() and _cache_process_ids(target):
        raise RuntimeError(
            "旧版客户端或本地 MCP/SSH 仍在运行；请在托盘菜单选择“退出并停止本地 MCP”，"
            "再打开新版 EXE。不要删除缓存或 data；更新会保留配置和密钥。"
        )


def _journal_path(target: Path) -> Path:
    return target.parent / f".{target.name}.update.json"


def _write_journal(target: Path, record: dict) -> None:
    path = _journal_path(target)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(record, handle, ensure_ascii=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_journal(target: Path) -> tuple[dict, Path]:
    path = _journal_path(target)
    if _is_link(path) or path.stat().st_size > 65536:
        raise RuntimeError("缓存升级记录无效；未触碰 data。")
    record = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(record, dict) or record.get("version") != 1
        or record.get("phase") not in {"swapping", "committed"}
        or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("digest", "")))
        or not re.fullmatch(re.escape(f".{target.name}.program-backup-") + r"[0-9a-f]{32}", str(record.get("backup", "")))):
        raise RuntimeError("缓存升级记录无效；未触碰 data。")
    entries = record.get("entries")
    if (not isinstance(entries, list) or not entries
        or any(not isinstance(e, dict) or e.get("name") not in PROGRAM_COMPONENTS | {MARKER_NAME}
               or type(e.get("had_old")) is not bool for e in entries)
        or len({e["name"] for e in entries}) != len(entries)):
        raise RuntimeError("缓存升级组件记录无效；未触碰 data。")
    backup = target.parent / record["backup"]
    if _is_link(target) or _is_link(backup) or not backup.is_dir():
        raise RuntimeError("缓存升级备份无效；未触碰 data。")
    for entry in entries:
        if _is_link(target / entry["name"]) or _is_link(backup / entry["name"]):
            raise RuntimeError("缓存升级路径含有链接；未触碰 data。")
    return record, backup


def _remove_program_component(path: Path) -> None:
    if path.name not in PROGRAM_COMPONENTS | {MARKER_NAME} or _is_link(path):
        raise RuntimeError("拒绝删除非程序组件。")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _recover_update(target: Path) -> None:
    """Roll back an interrupted program swap; keep all per-device data in place."""
    journal = _journal_path(target)
    if not journal.exists():
        return
    record, backup = _read_journal(target)
    if record["phase"] == "committed" and _is_complete_install(target, record["digest"]):
        # Both the version marker and atomic commit record were published.
        journal.unlink()
        return
    _require_stopped_cache(target)
    for entry in reversed(record["entries"]):
        dest = target / entry["name"]
        old = backup / entry["name"]
        if old.exists():
            _remove_program_component(dest)
            os.replace(old, dest)
        elif not entry["had_old"]:
            _remove_program_component(dest)
        # If had_old but no backup exists, that component was not yet moved
        # (or was already restored by an interrupted recovery).
    journal.unlink()


def _upgrade_program(target: Path, source: Path, digest: str) -> None:
    # Stage and validate first; updating never parses/decrypts a customer profile.
    _require_stopped_cache(target)
    names = sorted(item.name for item in source.iterdir())
    if not names or any(name not in PROGRAM_COMPONENTS for name in names):
        raise RuntimeError("缓存包包含非程序目录。")
    target.mkdir(parents=True, exist_ok=True)
    if _is_link(target) or any(_is_link(target / name) for name in names + [MARKER_NAME]):
        raise RuntimeError("拒绝覆盖链接类型的缓存目录。")
    backup = target.parent / (f".{target.name}.program-backup-" + uuid.uuid4().hex)
    backup.mkdir(mode=0o700)
    # Remove the old launch entry first, publish the new entry last. Old
    # launchers cannot start a half-published program tree via launch.py.
    order = ["launch.py"] + [n for n in names if n != "launch.py"] + [MARKER_NAME]
    record = {"version": 1, "phase": "swapping", "digest": digest, "backup": backup.name,
              "entries": [{"name": name, "had_old": (target / name).exists()} for name in order]}
    _write_journal(target, record)
    try:
        for entry in record["entries"]:
            if entry["had_old"]:
                os.replace(target / entry["name"], backup / entry["name"])
        for name in [n for n in names if n != "launch.py"] + ["launch.py"]:
            os.replace(source / name, target / name)
        if not all((target / name).is_file() for name in REQUIRED_PROGRAM_FILES):
            raise RuntimeError("更新后的程序文件不完整。")
        _write_marker(target / MARKER_NAME, digest)
        record["phase"] = "committed"
        _write_journal(target, record)
    except BaseException as exc:
        try:
            _recover_update(target)
        except Exception as recovery:
            raise RuntimeError(
                f"程序升级中断，恢复尚未完成；data 未被删除。请退出旧版后重新运行新版。程序备份：{backup}"
            ) from recovery
        raise RuntimeError("程序升级未完成，已恢复旧程序；data 和密钥未修改，请重试。") from exc
    _journal_path(target).unlink()
    # Program-only backup deliberately retained; contains no data/workspace.


def _install_payload() -> Path:
    payload = _payload_path()
    digest = _payload_digest(payload)
    target = _cache_root()
    target.parent.mkdir(parents=True, exist_ok=True)
    with _payload_install_lock(target):
        # Also lock the fast path so another launcher cannot start a partial
        # update. A journal left by a crash is handled before trusting markers.
        _recover_update(target)
        if _is_complete_install(target, digest):
            return target
        _require_stopped_cache(target)
        stage = Path(tempfile.mkdtemp(prefix="kt-single-exe-", dir=str(target.parent)))
        try:
            source = _safe_extract(payload, stage)
            _upgrade_program(target, source, digest)
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
        if candidates:
            result = _launch(root, wait=wait)
        else:
            # Serialize process creation with other cache installers. Once
            # Popen returns, later upgrades observe this runtime as in use.
            with _payload_install_lock(root):
                if _journal_path(root).exists():
                    raise RuntimeError("另一个缓存升级尚未完成，请重新运行。")
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
