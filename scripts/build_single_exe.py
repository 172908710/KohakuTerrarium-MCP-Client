"""Build one Windows EXE launcher around the verified portable payload.

The EXE is a self-extracting launcher, not a second KT implementation.  It
keeps the existing portable runtime, OpenSSH client, control extension and
persistent profile behavior unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = ROOT / "dist" / "KohakuTerrarium-MCP-Client-Windows-x64-portable.zip"
ENTRY = ROOT / "scripts" / "single_exe_launcher.py"
PYINSTALLER_NAME = "PyInstaller"


def _run(command: list[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def build(output: Path) -> Path:
    if not PAYLOAD.is_file():
        _run([sys.executable, str(ROOT / "scripts" / "build_portable.py"), "--zip-only"])
    if not PAYLOAD.is_file():
        raise RuntimeError("portable payload was not built")
    try:
        import PyInstaller  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "PyInstaller is required in the build venv; install "
            "pyinstaller==6.22.0 first"
        ) from exc

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    work = ROOT / ".single-exe-build"
    dist = work / "dist"
    spec = work / "spec"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    try:
        _run([
            sys.executable,
            "-m",
            PYINSTALLER_NAME,
            "--noconfirm",
            "--clean",
            "--onefile",
            "--windowed",
            "--name",
            "KohakuTerrarium-MCP-Client",
            "--distpath",
            str(dist),
            "--workpath",
            str(work / "work"),
            "--specpath",
            str(spec),
            "--add-data",
            f"{PAYLOAD};payload",
            str(ENTRY),
        ])
        built = dist / "KohakuTerrarium-MCP-Client.exe"
        if not built.is_file():
            raise RuntimeError("PyInstaller did not produce the EXE")
        shutil.copy2(built, output)
        # Compute the digest in Python so the build does not depend on a
        # second shell (and so paths containing quotes cannot alter a command).
        with output.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        output.with_suffix(output.suffix + ".sha256").write_text(
            f"{digest}  {output.name}\n", encoding="ascii"
        )
        return output
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "KohakuTerrarium-MCP-Client.exe",
    )
    args = parser.parse_args()
    result = build(args.output)
    print(result)


if __name__ == "__main__":
    main()
