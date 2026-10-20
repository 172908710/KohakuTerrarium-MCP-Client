"""Verify a clean ZIP in a new directory containing Chinese text and spaces.

Run with runtime/python.exe -I -X utf8 scripts/verify_portable.py.
--with-local-mcp additionally runs the explicitly isolated local MCP smoke test.
This is not a replacement for testing on a clean Windows VM.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, default=ROOT / "dist/KohakuTerrarium-MCP-Client-Windows-x64-portable.zip")
    parser.add_argument("--with-local-mcp", action="store_true")
    args = parser.parse_args()
    with args.archive.open("rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    expected = args.archive.with_suffix(".zip.sha256").read_text(encoding="ascii").split()[0]
    if digest != expected:
        raise RuntimeError("Archive checksum mismatch")
    report = {"sha256_verified": True, "relocation_verified": False, "clean_windows_vm_tested": False}
    with zipfile.ZipFile(args.archive) as archive:
        for name in archive.namelist():
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or path.parts[0] != "KohakuTerrarium-MCP-Client":
                raise RuntimeError("Unexpected archive path")
            if len(path.parts) > 1 and path.parts[1] in {"data", "workspace", ".venv", ".git", "dist"}:
                raise RuntimeError("Private/runtime state found in distribution")
            if path.name == ".env" or name.endswith(("/server/registry.sqlite3", "/server/routes.map")):
                raise RuntimeError("Server credentials/state found in distribution")
        report["archive_entries"] = len(archive.infolist())
        with tempfile.TemporaryDirectory(prefix=".portable-smoke-relocation-", dir=ROOT) as directory:
            destination = Path(directory) / "测试路径 with spaces"
            archive.extractall(destination)
            app = destination / "KohakuTerrarium-MCP-Client"
            env = os.environ.copy()
            system = Path(os.environ.get("SystemRoot", r"C:\Windows"))
            env.update(PATH=str(system / "System32") + os.pathsep + str(system),
                       PYTHONHOME="Z:\\not-a-python", PYTHONPATH="Z:\\not-a-module-path",
                       KT_MCP_CLIENT_CONFIG_DIR=str(Path(directory) / "must-not-be-used"))
            def run(arguments, timeout=45):
                p = subprocess.run([str(app / "runtime/python.exe"), "-I", "-X", "utf8", *arguments],
                                   cwd=str(app), env=env, capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=timeout)
                if p.returncode:
                    raise RuntimeError("Relocated verification failed:\n" + p.stdout + p.stderr)
                return p.stdout
            check = json.loads(run([str(app / "launch.py"), "--check"]))
            assert Path(check["kt_python"]) == app / "runtime/python.exe"
            # The first-run profile combines the local formal registry (when
            # present) with this machine's currently mounted drive roots.
            # A clean machine still falls back to app/workspace.
            discovered = json.loads(run([
                "-c",
                "import json;from pathlib import Path;"
                "from kt_mcp_client.portable import _initial_workspaces;"
                "print(json.dumps([item['path'] for item in _initial_workspaces(Path.cwd())]))",
            ]))
            expected_workspaces = {Path(item).resolve() for item in discovered}
            expected_workspaces.add((app / "workspace").resolve())
            assert Path(check["workspace"]).resolve() in expected_workspaces
            assert Path(check["profile"]) == app / "data/profile.json"
            assert not (Path(directory) / "must-not-be-used").exists()
            assert check["ssh_bundled"] and check["runtime_cli_supported"]
            gui_probe = (
                "from pathlib import Path;import tkinter as tk;"
                "from kt_mcp_client.portable import prepare_environment;"
                "from kt_mcp_client.gui import ClientGUI;"
                "r=Path.cwd();prepare_environment(r);"
                "w=tk.Tk();w.withdraw();g=ClientGUI(w,config_path=str(r/'data/profile.json'));"
                "w.update();print('GUI_OK');w.destroy()"
            )
            assert "GUI_OK" in run(["-c", gui_probe])
            report.update(relocation_verified=True, gui_created=True, initialized_local_profile=True,
                          system_python_removed_from_path=True, poisoned_python_environment_ignored=True)
            if args.with_local_mcp:
                smoke = json.loads(run([str(app / "scripts/smoke_local.py")], timeout=150))
                assert smoke["success"] and smoke["cleanup_state"] == "stopped"
                report["relocated_local_mcp"] = {k: smoke[k] for k in ("setup", "local_ready", "public_ready", "actual_endpoint_verified", "mcp_tools", "cleanup_state")}
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    report_file = args.archive.parent / "portable-verification.json"
    report_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
