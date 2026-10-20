"""Build a self-contained Windows x64 bundle from a dedicated build venv.

Usage: .venv\\Scripts\\python.exe scripts\\build_portable.py [--runtime-only|--zip-only]
Never copies a user's profile, workspace, credentials, PID files or logs.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SSH_URL = "https://github.com/PowerShell/Win32-OpenSSH/releases/download/10.0.0.0p2-Preview/OpenSSH-Win64.zip"
SSH_SHA256 = "23f50f3458c4c5d0b12217c6a5ddfde0137210a30fa870e98b29827f7b43aba5"
ARCHIVE_NAME = "KohakuTerrarium-MCP-Client-Windows-x64-portable.zip"


def ignore_cache(directory: str, names: list[str]) -> set[str]:
    return {n for n in names if n in {"__pycache__", ".pytest_cache"} or n.endswith((".pyc", ".pyo"))}


def runtime_check(runtime: Path) -> None:
    env = os.environ.copy()
    env.update(PYTHONHOME="Z:\\unavailable-python", PYTHONPATH="Z:\\unavailable-packages", PYTHONNOUSERSITE="1",
               TCL_LIBRARY=str(runtime / "tcl/tcl8.6"), TK_LIBRARY=str(runtime / "tcl/tk8.6"))
    probe = (
        "import sys,site,pathlib,tkinter,ssl,sqlite3,importlib.metadata as m;"
        "r=pathlib.Path(sys.executable).resolve().parent;"
        "assert pathlib.Path(sys.prefix).resolve()==r,(sys.prefix,r);"
        "assert not site.ENABLE_USER_SITE;"
        "import kohakuterrarium,mcp,win32api;"
        "assert pathlib.Path(kohakuterrarium.__file__).resolve().is_relative_to(r);"
        "t=tkinter.Tk();t.withdraw();t.update();t.destroy();"
        "print('Runtime/Tk/SSL/SQLite/Win32/KT:',sys.version.split()[0],m.version('kohakuterrarium'))"
    )
    for args in [["-c", probe], ["-m", "pip", "check"], ["-m", "kohakuterrarium", "mcp-serve", "setup", "--help"]]:
        result = subprocess.run([str(runtime / "python.exe"), "-I", "-X", "utf8", *args], env=env,
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=45)
        if result.returncode:
            raise RuntimeError("Bundled runtime check failed:\n" + result.stdout + result.stderr)
        print(result.stdout[:1800], flush=True)


def build_runtime() -> None:
    if os.name != "nt" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("Build on Windows x64 using the dedicated .venv.")
    prefix = Path(sys.prefix).resolve()
    if prefix != (ROOT / ".venv").resolve() or prefix == Path(sys.base_prefix).resolve():
        raise RuntimeError("Use this project's .venv, not system Python or production Bridge.")
    if metadata.version("kohakuterrarium") != "2.1.5":
        raise RuntimeError("This bundle is verified against kohakuterrarium==2.1.5.")
    target = ROOT / "runtime"
    if target.exists():
        raise RuntimeError("runtime already exists; do not replace a potentially running environment. Use --zip-only.")
    base = Path(sys.base_prefix)
    stage = ROOT / (".runtime-build-" + uuid.uuid4().hex)
    stage.mkdir()
    try:
        for path in base.iterdir():
            if path.is_file() and (path.suffix.lower() in {".exe", ".dll"} or path.name == "LICENSE.txt"):
                shutil.copy2(path, stage / path.name)
        for name in ("DLLs", "tcl"):
            shutil.copytree(base / name, stage / name, ignore=ignore_cache)
        def stdlib_ignore(directory: str, names: list[str]) -> set[str]:
            omitted = ignore_cache(directory, names)
            if Path(directory) == base / "Lib":
                omitted.update({"site-packages", "test"})
            return omitted
        shutil.copytree(base / "Lib", stage / "Lib", ignore=stdlib_ignore)
        shutil.copytree(prefix / "Lib/site-packages", stage / "Lib/site-packages", ignore=ignore_cache)
        # CPython's DLL-named _pth controls python.exe, pythonw.exe and KT children.
        pth = stage / f"python{sys.version_info.major}{sys.version_info.minor}._pth"
        pth.write_text(".\nDLLs\nLib\nLib/site-packages\n../src\nimport site\n", encoding="utf-8")
        packages = sorted([{"name": d.metadata["Name"], "version": d.version} for d in metadata.distributions()
                           if d.metadata.get("Name")], key=lambda item: item["name"].lower())
        (ROOT / "requirements-runtime.lock.txt").write_text(
            "# Windows x64 build snapshot; install in a dedicated build environment.\n" +
            "\n".join(f"{p['name']}=={p['version']}" for p in packages if p["name"].lower() != "pip") + "\n", encoding="utf-8")
        (stage / "bundle-manifest.json").write_text(json.dumps({
            "python": platform.python_version(), "platform": "Windows-x64", "kt": "2.1.5",
            "created_utc": datetime.now(timezone.utc).isoformat(), "packages": packages,
            "isolation": "python DLL _pth; no system or user site-packages", "contains_user_data": False,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        runtime_check(stage)
        stage.rename(target)
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def bundle_ssh() -> None:
    target = ROOT / "tools/openssh"
    if (target / "ssh.exe").is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kt-mcp-openssh-") as temp:
        archive = Path(temp) / "OpenSSH-Win64.zip"
        req = urllib.request.Request(SSH_URL, headers={"User-Agent": "KT-MCP-Portable-Builder"})
        with urllib.request.urlopen(req, timeout=60) as response, archive.open("wb") as out:
            shutil.copyfileobj(response, out)
        with archive.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != SSH_SHA256:
            raise RuntimeError("Official OpenSSH archive SHA256 mismatch; no binaries installed.")
        target.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive) as z:
            # Bundle client programs and licenses, not sshd or install-service scripts.
            for info in z.infolist():
                p = Path(info.filename)
                name = p.name
                if name.lower() in {"ssh.exe", "ssh-keygen.exe", "ssh-keyscan.exe", "scp.exe", "sftp.exe", "license.txt", "thirdpartynotices.txt"} or name.lower().endswith(".dll"):
                    (target / name).write_bytes(z.read(info))
        if not (target / "ssh.exe").is_file():
            raise RuntimeError("Official OpenSSH archive did not contain ssh.exe.")
        (target / "source.json").write_text(json.dumps({"url": SSH_URL, "sha256": SSH_SHA256, "version": "10.0.0.0p2-Preview"}, indent=2) + "\n", encoding="utf-8")
    result = subprocess.run([str(target / "ssh.exe"), "-V"], capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise RuntimeError("Bundled SSH client cannot run: " + result.stderr)
    print(result.stderr.strip() or result.stdout.strip(), flush=True)


def distribution_files(root: Path):
    """Explicit source/runtime allowlist; never recursively zip the whole project."""
    directories = {"runtime", "tools", "src", "control", "server", "scripts", "tests"}
    files = {"launch.py", "Start-KT-MCP-Client.vbs", "Start-KT-MCP-Client.cmd", "Check-KT-MCP-Client.cmd",
             "README_中文.md", "README_便携版.md", "THIRD-PARTY-NOTICES.md", "pytest.ini",
             "requirements-runtime.txt", "requirements-runtime.lock.txt", "requirements-build.txt"}
    for name in sorted(files):
        p = root / name
        if p.is_file():
            yield p
    for name in sorted(directories):
        for p in sorted((root / name).rglob("*")):
            rel = p.relative_to(root)
            if p.is_symlink():
                raise RuntimeError(f"Refusing symlink in distribution: {rel}")
            if not p.is_file() or any(x in {"__pycache__", ".pytest_cache", ".git", ".venv"} for x in rel.parts):
                continue
            if p.suffix.lower() in {".pyc", ".pyo", ".log"} or p.name == ".env":
                continue
            if name in {"src", "server", "scripts", "tests"}:
                if "backups" in rel.parts or (p.name.startswith(".env") and p.name != ".env.example"):
                    continue
                if p.suffix.lower() not in {".py", ".md", ".txt", ".ini", ".example", ".conf"}:
                    continue
            yield p


def build_zip() -> Path:
    output = ROOT / "dist" / ARCHIVE_NAME
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp.zip")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=5) as z:
        for p in distribution_files(ROOT):
            z.write(p, Path("KohakuTerrarium-MCP-Client") / p.relative_to(ROOT))
    with zipfile.ZipFile(temporary) as z:
        corrupt = z.testzip()
        if corrupt:
            raise RuntimeError("Zip CRC failed: " + corrupt)
    temporary.replace(output)
    with output.open("rb") as f:
        digest = hashlib.file_digest(f, "sha256").hexdigest()
    output.with_suffix(".zip.sha256").write_text(f"{digest}  {output.name}\n", encoding="ascii")
    print(json.dumps({"archive": str(output), "bytes": output.stat().st_size, "sha256": digest}), flush=True)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--runtime-only", action="store_true")
    group.add_argument("--zip-only", action="store_true")
    args = parser.parse_args()
    if not args.zip_only:
        build_runtime()
        bundle_ssh()
    else:
        runtime_check(ROOT / "runtime")
        if not (ROOT / "tools/openssh/ssh.exe").is_file():
            raise RuntimeError("Missing bundled SSH; finish building tools before packaging.")
    if not args.runtime_only:
        build_zip()


if __name__ == "__main__":
    main()
