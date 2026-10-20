from __future__ import annotations

import json
from pathlib import Path

from kt_mcp_client.config import DeviceProfile
from kt_mcp_client.kt_runtime import KTRuntime
from kt_mcp_client.process import STUDIO_CONTROL_CONFIG_ENV


def _runtime(tmp_path: Path) -> KTRuntime:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile = DeviceProfile(
        workspace_path=str(workspace),
        workspace_id="localpc",
        kt_python=str(Path(__file__).resolve()),
        kt_home=str(tmp_path / "kt-home"),
        public_origin="https://example.invalid",
    )
    return KTRuntime(profile, tmp_path / "runtime", config_root=tmp_path)


def test_studio_control_config_is_per_device_and_self_contained(tmp_path):
    runtime = _runtime(tmp_path)
    config = runtime._ensure_studio_control_config()

    assert config == tmp_path / "control" / "control.json"
    payload = json.loads(config.read_text(encoding="utf-8"))
    assert payload["api_base"] == "http://127.0.0.1:8001"
    assert payload["receipts_db"] == str(tmp_path / "control" / "receipts.sqlite3")
    assert "KT-MCP-Bridge" not in str(config)
    assert not (tmp_path / "control" / "receipts.sqlite3").exists()


def test_studio_extension_environment_is_explicit(tmp_path):
    runtime = _runtime(tmp_path)
    env = runtime._child_environment()

    assert env[STUDIO_CONTROL_CONFIG_ENV] == str(tmp_path / "control" / "control.json")
    assert env["PYTHONPATH"] == str(Path(__file__).resolve().parents[1] / "control")
    assert "KT_MCP_PROVISION_TOKEN" not in env

