"""Per-user Windows logon startup registration.

The client deliberately uses ``HKCU`` rather than a machine-wide registry
location.  This needs no administrator permission and keeps the setting local
to the current Windows account.  No profile values or credentials are placed
in the registry; the command only points back to this installation and passes
the profile path.
"""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any


VALUE_NAME = "KohakuTerrarium-MCP-Client"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

try:  # ``winreg`` is Windows-only; importing the source must stay portable.
    import winreg  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - exercised on non-Windows hosts
    winreg = None  # type: ignore[assignment]


class StartupError(RuntimeError):
    """Raised when the per-user startup value cannot be read or changed."""


def _registry() -> Any:
    if os.name != "nt" or winreg is None:
        raise StartupError("Windows 开机自启只支持 Windows")
    return winreg


def _config_argument(config_path: str | os.PathLike[str] | None) -> list[str]:
    if config_path is None:
        return []
    value = str(config_path).strip()
    return ["--config", value] if value else []


def startup_command(config_path: str | os.PathLike[str] | None = None) -> str:
    """Build the command stored in HKCU Run for this exact installation.

    A bundled ``pythonw.exe`` is preferred for source/portable launches so a
    console window does not flash at logon.  In a frozen executable,
    ``sys.executable`` is already the single EXE and is used directly.
    """

    # The one-file launcher exports its outer EXE path to the extracted
    # runtime.  Use that path so logon startup continues to go through the
    # same safe payload/version checks instead of bypassing the EXE.
    launcher_exe = os.environ.get("KT_MCP_LAUNCHER_EXE", "").strip()
    if launcher_exe and Path(launcher_exe).is_file():
        argv = [str(Path(launcher_exe).resolve())]
    else:
        executable = Path(sys.executable).resolve()
        if not getattr(sys, "frozen", False):
            # ``pythonw.exe`` has the same interpreter but does not create a
            # console.  Keep the regular executable when a sibling is unavailable
            # (for example in a Unix development environment).
            candidate = executable.with_name("pythonw.exe")
            if candidate.is_file():
                executable = candidate
            script = Path(sys.argv[0])
            if script.is_file():
                argv = [str(executable), "-I", "-X", "utf8", str(script.resolve())]
            else:
                argv = [str(executable), "-I", "-X", "utf8", "-m", "kt_mcp_client"]
        else:
            argv = [str(executable)]
    argv.extend(_config_argument(config_path))
    # The startup mode starts in the tray and automatically restores a saved
    # profile.  It is intentionally a normal argument, not a secret value.
    argv.append("--startup")
    if os.name == "nt":
        # Windows' Run values use CommandLineToArgvW-compatible quoting.
        return subprocess.list2cmdline(argv)
    # This branch is useful for diagnostics/tests on non-Windows hosts.
    return shlex.join(argv)


def is_startup_enabled(*, value_name: str = VALUE_NAME) -> bool:
    """Return whether this client's HKCU Run value is present."""

    registry = _registry()
    try:
        with registry.OpenKey(registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_READ) as key:
            registry.QueryValueEx(key, value_name)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise StartupError(f"无法读取开机自启设置：{exc}") from exc
    return True


def set_startup_enabled(
    enabled: bool,
    config_path: str | os.PathLike[str] | None = None,
    *,
    value_name: str = VALUE_NAME,
) -> bool:
    """Enable/disable this client for the current Windows user.

    Returns the resulting state.  ``enabled=False`` is idempotent when the
    value does not exist.
    """

    registry = _registry()
    try:
        if enabled:
            with registry.CreateKey(registry.HKEY_CURRENT_USER, RUN_KEY) as key:
                registry.SetValueEx(
                    key,
                    value_name,
                    0,
                    registry.REG_SZ,
                    startup_command(config_path),
                )
        else:
            with registry.OpenKey(
                registry.HKEY_CURRENT_USER, RUN_KEY, 0, registry.KEY_SET_VALUE
            ) as key:
                try:
                    registry.DeleteValue(key, value_name)
                except FileNotFoundError:
                    pass
    except FileNotFoundError:
        if enabled:
            raise StartupError("无法创建当前用户的开机自启设置") from None
    except OSError as exc:
        raise StartupError(f"无法修改开机自启设置：{exc}") from exc
    return bool(enabled)


__all__ = [
    "RUN_KEY",
    "StartupError",
    "VALUE_NAME",
    "is_startup_enabled",
    "set_startup_enabled",
    "startup_command",
]
