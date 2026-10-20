from pathlib import Path
import importlib.util
import json
import subprocess

import pytest

from kt_mcp_client import portable
from kt_mcp_client.config import load, save


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "便携版 bundle"
    for rel in ("runtime/python.exe", "runtime/pythonw.exe", "runtime/Lib/os.py"):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test placeholder", encoding="utf-8")
    return root


def test_first_run_creates_local_profile_without_credentials(bundle, monkeypatch, tmp_path):
    external = tmp_path / "not-used-appdata"
    monkeypatch.setenv("KT_MCP_CLIENT_CONFIG_DIR", str(external))
    filename = portable.initialize(bundle, verify=False)
    assert filename == bundle / "data/profile.json"
    assert not external.exists()
    profile = load(filename)
    assert Path(profile.kt_python) == bundle / "runtime/python.exe"
    expected_workspaces = portable._initial_workspaces(bundle) or [
        {"workspace_id": "localpc", "path": str(bundle / "workspace")}
    ]
    assert Path(profile.workspace_path) == Path(expected_workspaces[0]["path"]).resolve()
    assert profile.workspaces == [
        {"workspace_id": item["workspace_id"], "path": str(Path(item["path"]).resolve())}
        for item in expected_workspaces
    ]
    assert Path(profile.kt_home) == bundle / "data/kt-home"
    assert profile.local_port == 8865
    assert profile.remote_forward_port == 18766
    assert not profile.secret
    assert not profile.endpoint_url
    assert not profile.endpoint_verified
    assert not profile.ssh_host
    assert not (bundle / "data/kt-home/mcp-serve/endpoint/connection.json").exists()


def test_first_run_discovers_available_drive_roots_and_merges_registry(bundle, monkeypatch, tmp_path):
    formal = tmp_path / "formal-workspace"
    formal.mkdir()
    drive_c = tmp_path / "drive-c"
    drive_c.mkdir()
    drive_d = tmp_path / "drive-d"
    drive_d.mkdir()
    monkeypatch.setattr(
        portable,
        "_production_workspaces",
        lambda: [
            {"workspace_id": "localpc", "path": str(formal)},
            {"workspace_id": "disk_c", "path": str(drive_c)},
        ],
    )
    monkeypatch.setattr(
        portable,
        "_discover_drive_workspaces",
        lambda: [
            {"workspace_id": "disk_c", "path": str(drive_c)},
            {"workspace_id": "disk_d", "path": str(drive_d)},
        ],
    )

    filename = portable.initialize(bundle, verify=False)
    profile = load(filename)
    assert profile.workspaces == [
        {"workspace_id": "localpc", "path": str(formal.resolve())},
        {"workspace_id": "disk_c", "path": str(drive_c.resolve())},
        {"workspace_id": "disk_d", "path": str(drive_d.resolve())},
    ]


def test_first_run_uses_bundled_localpc_when_no_drive_is_available(bundle, monkeypatch):
    monkeypatch.setattr(portable, "_production_workspaces", lambda: [])
    monkeypatch.setattr(portable, "_discover_drive_workspaces", lambda: [])

    filename = portable.initialize(bundle, verify=False)
    profile = load(filename)
    assert profile.workspaces == [
        {"workspace_id": "localpc", "path": str((bundle / "workspace").resolve())}
    ]


def test_existing_profile_is_not_rewritten(bundle):
    filename = portable.initialize(bundle, verify=False)
    profile = load(filename)
    profile.display_name = "user-customized"
    save(profile, filename)
    before = filename.read_bytes()
    assert portable.initialize(bundle, verify=False) == filename
    assert filename.read_bytes() == before


def test_invalid_existing_profile_is_not_overwritten(bundle):
    filename = portable.initialize(bundle, verify=False)
    filename.write_text('{"kt_python":"","workspace_path":""}', encoding="utf-8")
    before = filename.read_bytes()
    with pytest.raises(portable.PortableError, match="已有配置未被覆盖"):
        portable.initialize(bundle, verify=False)
    assert filename.read_bytes() == before


def test_missing_runtime_fails_before_creating_data(bundle):
    (bundle / "runtime/python.exe").unlink()
    with pytest.raises(portable.PortableError, match="随包 Python 不完整"):
        portable.initialize(bundle, verify=False)
    assert not (bundle / "data").exists()


def test_explicit_config_directory(bundle, tmp_path):
    target = tmp_path / "explicit-profile"
    filename = portable.initialize(bundle, str(target), verify=False)
    assert filename == target / "profile.json"
    assert not (bundle / "data").exists()


def test_relocated_live_data_is_not_silently_reused(bundle):
    filename = portable.initialize(bundle, verify=False)
    marker = filename.parent / "portable-location.json"
    marker.write_text(json.dumps({"root": "X:/different-installation"}), encoding="utf-8")
    before = filename.read_bytes()
    with pytest.raises(portable.PortableError, match="旧安装位置"):
        portable.initialize(bundle, verify=False)
    assert filename.read_bytes() == before


@pytest.mark.parametrize("returncode,output", [(2, "invalid choice: mcp-serve"), (0, "some other help")])
def test_probe_rejects_wrong_kt(bundle, monkeypatch, returncode, output):
    monkeypatch.setattr(portable.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, returncode, output, ""))
    with pytest.raises(portable.PortableError, match="不支持"):
        portable.probe_runtime(bundle / "runtime/python.exe")


def test_probe_requires_actual_cli_flags(bundle, monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "--home-dir --mode --config --non-interactive", "")
    monkeypatch.setattr(portable.subprocess, "run", run)
    portable.probe_runtime(bundle / "runtime/python.exe")
    assert calls[0][0][0] == str(bundle / "runtime/python.exe")
    assert "-I" in calls[0][0]
    assert calls[0][1]["timeout"] == 25


def test_archive_excludes_user_data_and_real_env(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/build_portable.py"
    spec = importlib.util.spec_from_file_location("portable_builder", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for rel in ["data/profile.json", "workspace/private.txt", ".venv/password.txt", "server/.env",
                "src/__pycache__/secret.pyc", "server/.env.example", "src/app.py", "launch.py"]:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
    files = {p.relative_to(tmp_path).as_posix() for p in module.distribution_files(tmp_path)}
    assert files == {"server/.env.example", "src/app.py", "launch.py"}
