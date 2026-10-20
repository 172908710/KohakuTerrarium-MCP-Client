"""First-run initialization for the self-contained Windows distribution.

No installation, production configuration discovery, endpoint creation, or
network enrollment is performed here. Those remain explicit runtime actions.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import string

from .config import (
    ConfigError,
    DeviceProfile,
    PRODUCTION_BRIDGE_ROOT,
    config_file,
    canonicalize_workspace,
    load,
    save,
    validate,
)
from .provisioning import DEFAULT_ORIGIN


class PortableError(RuntimeError):
    """The bundled distribution is incomplete or cannot initialize safely."""


def _production_workspaces() -> list[dict[str, str]]:
    """Read only the formal workspace registry when it is present locally.

    A clean portable bundle must still work on a different computer, so the
    production bindings are used only when all three registered paths exist.
    No endpoint URL, secret, or production KT home is read or copied.
    """

    registry = (
        Path(PRODUCTION_BRIDGE_ROOT)
        / "kt-home"
        / "mcp-serve"
        / "endpoint"
        / "workspaces.json"
    )
    try:
        payload = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    records = payload.get("workspaces") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return []
    result: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            return []
        workspace_id = str(record.get("workspace_id", "")).strip()
        workspace_path = str(record.get("path", "")).strip()
        try:
            path = canonicalize_workspace(workspace_path)
        except ConfigError:
            return []
        if not workspace_id or not workspace_path or workspace_id in seen_ids or not path.is_dir():
            return []
        seen_ids.add(workspace_id)
        result.append({"workspace_id": workspace_id, "path": str(path)})
    return result if result else []


def _discover_drive_workspaces() -> list[dict[str, str]]:
    """Return one workspace entry for every usable local drive root.

    A clean bundle can be copied to a machine with a different drive layout,
    so drive letters must be discovered at first run instead of being copied
    from the build machine. ``Path.is_dir`` also includes mounted/network
    drives that are currently available to this process. Discovery is best
    effort: inaccessible or malformed roots are skipped and the caller keeps
    the bundled ``localpc`` fallback when no roots are available.
    """

    result: list[dict[str, str]] = []
    for letter in string.ascii_uppercase:
        candidate = Path(f"{letter}:\\")
        try:
            if not candidate.is_dir():
                continue
            path = canonicalize_workspace(candidate)
        except (ConfigError, OSError, RuntimeError, ValueError):
            continue
        result.append({"workspace_id": f"disk_{letter.lower()}", "path": str(path)})
    return result


def _initial_workspaces(root: Path) -> list[dict[str, str]]:
    """Merge formal registrations with this machine's available drive roots.

    Formal entries stay first so an existing device keeps its named default
    workspace. Drive roots are appended and de-duplicated by canonical path;
    IDs are made unique as well because the profile schema rejects duplicate
    IDs. ``root`` is accepted for the bundled fallback and future callers,
    while discovery itself never depends on the installation path.
    """

    del root  # The fallback path is created by ``initialize`` after discovery.
    merged: list[dict[str, str]] = []
    seen_paths: set[Path] = set()
    seen_ids: set[str] = set()
    for source in (_production_workspaces(), _discover_drive_workspaces()):
        for item in source:
            if not isinstance(item, dict):
                continue
            workspace_id = str(item.get("workspace_id", "")).strip()
            workspace_path = str(item.get("path", "")).strip()
            if not workspace_id or not workspace_path:
                continue
            try:
                path = canonicalize_workspace(workspace_path)
            except (ConfigError, OSError, RuntimeError, ValueError):
                continue
            if path in seen_paths:
                continue
            base_id = workspace_id
            suffix = 2
            while workspace_id in seen_ids:
                workspace_id = f"{base_id}_{suffix}"
                suffix += 1
            seen_paths.add(path)
            seen_ids.add(workspace_id)
            merged.append({"workspace_id": workspace_id, "path": str(path)})
    return merged


def bundled_python(root: Path) -> Path:
    root = root.resolve()
    executable = root / "runtime" / "python.exe"
    required = (executable, root / "runtime" / "pythonw.exe", root / "runtime" / "Lib" / "os.py")
    if not all(path.is_file() for path in required):
        raise PortableError("随包 Python 不完整。请完整解压便携版 ZIP，不要只复制启动脚本。")
    if executable.resolve().parent != (root / "runtime").resolve():
        raise PortableError("随包 Python 路径无效。")
    return executable


def probe_runtime(executable: Path) -> None:
    """Check actual CLI capabilities; finding python.exe alone is insufficient."""
    try:
        result = subprocess.run(
            [str(executable), "-I", "-X", "utf8", "-m", "kohakuterrarium", "mcp-serve", "setup", "--help"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=25, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PortableError("随包 KT 无法启动；请运行 Check-KT-MCP-Client.cmd 检查完整性。") from exc
    if result.returncode or not all(flag in result.stdout for flag in ("--home-dir", "--mode", "--config", "--non-interactive")):
        raise PortableError("随包 KT 不支持所需的 mcp-serve setup 命令；请重新构建运行环境。")


def prepare_environment(root: Path) -> None:
    """Affect this process and its children only, never system settings."""
    runtime = root / "runtime"
    os.environ.pop("PYTHONHOME", None)
    os.environ.pop("PYTHONPATH", None)
    os.environ["PYTHONNOUSERSITE"] = "1"
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["TCL_LIBRARY"] = str(runtime / "tcl" / "tcl8.6")
    os.environ["TK_LIBRARY"] = str(runtime / "tcl" / "tk8.6")
    prefix = [str(runtime), str(root / "tools" / "openssh")]
    os.environ["PATH"] = os.pathsep.join(prefix + [os.environ.get("PATH", "")])


def initialize(root: Path, requested_config: str | None = None, *, verify: bool = True) -> Path:
    """Create a local profile using the bundle's own interpreter and workspace.

    Existing profiles are never overwritten. Live data is bound to its original
    directory: distribute a clean ZIP, not another device's credentials/PIDs.
    """
    root = root.resolve()
    executable = bundled_python(root)
    if verify:
        probe_runtime(executable)
    filename = config_file(requested_config if requested_config is not None else root / "data")
    marker = filename.parent / "portable-location.json"
    if marker.exists():
        try:
            location = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PortableError("便携版位置记录损坏；未修改已有配置。") from exc
        if location.get("root") != str(root):
            raise PortableError("此 data 目录属于旧安装位置。请先停止旧实例，并从干净 ZIP 解压；不要分发带密钥的 data。")
    if filename.exists():
        profile = load(filename)
        errors = validate(profile, filename.parent)
        if errors:
            raise PortableError("已有配置未被覆盖：" + "；".join(errors))
        return filename
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    workspaces = _initial_workspaces(root)
    if not workspaces:
        workspaces = [{"workspace_id": "localpc", "path": str(workspace)}]
    profile = DeviceProfile(
        display_name="便携版独立 MCP",
        workspace_path=workspaces[0]["path"], workspace_id=workspaces[0]["workspace_id"],
        workspaces=workspaces,
        kt_python=str(executable), kt_home=str(filename.parent / "kt-home"),
        tools_config=str(filename.parent / "tools.yaml"),
        public_origin=DEFAULT_ORIGIN.rstrip("/"),
    )
    errors = validate(profile, filename.parent)
    if errors:
        raise PortableError("初始配置校验失败：" + "；".join(errors))
    save(profile, filename)
    # This is an installation location marker, NOT an endpoint provenance proof.
    marker.write_text(json.dumps({"version": 1, "root": str(root)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return filename


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--config")
    options, forwarded = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    root = (root or Path(__file__).resolve().parents[2]).resolve()
    prepare_environment(root)
    filename = initialize(root, options.config)
    if options.check:
        profile = load(filename)
        print(json.dumps({
            "bundle_root": str(root), "profile": str(filename),
            "kt_python": profile.kt_python, "kt_version": importlib.metadata.version("kohakuterrarium"),
            "workspace": profile.workspace_path, "kt_home": profile.kt_home,
            "local_port": profile.local_port, "runtime_cli_supported": True,
            "ssh_bundled": (root / "tools" / "openssh" / "ssh.exe").is_file(),
            "provision_token_set": bool(
                profile.provision_token or os.environ.get("KT_MCP_PROVISION_TOKEN", "").strip()
            ),
            "network_tested": False, "endpoint_started": False,
        }, ensure_ascii=False, indent=2))
        return 0
    from .__main__ import main as client_main
    return client_main(["--config", str(filename), *forwarded])
