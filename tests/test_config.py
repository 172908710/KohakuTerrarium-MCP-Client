"""Config persistence and endpoint provenance tests."""

import json
import sys
from pathlib import Path

import pytest

from tests.helpers import set_test_endpoint
from kt_mcp_client.config import (
    ConfigError,
    DEFAULT_MCP_PATH,
    DeviceProfile,
    canonicalize_kt_python,
    canonicalize_origin,
    canonicalize_runtime_root,
    load,
    load_successful_connection,
    remember_successful_connection,
    LAST_CONNECTION_FILENAME,
    mcp_url,
    parse_endpoint_url,
    set_actual_endpoint,
    set_actual_endpoint,
    public_dict,
    save,
    validate,
)

TOKEN = "A" * 43
TOKEN_2 = "B" * 43


def profile(tmp_path: Path, **overrides) -> DeviceProfile:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    home = tmp_path / "kt-home"
    home.mkdir(exist_ok=True)
    values = dict(
        display_name="test-device",
        workspace_path=str(workspace),
        kt_python=sys.executable,
        kt_home=str(home),
        public_origin="https://device.example.invalid",
        secret=TOKEN,
    )
    values.update(overrides)
    return DeviceProfile(**values)


def set_endpoint(value: DeviceProfile, endpoint: str) -> str:
    return set_test_endpoint(value, endpoint)


def test_config_is_isolated_and_secret_is_protected(tmp_path, monkeypatch):
    config_dir = tmp_path / "isolated-config"
    monkeypatch.setenv("KT_MCP_CLIENT_CONFIG_DIR", str(config_dir))
    original = profile(tmp_path)
    original.kt_home = str(config_dir / "kt-home")
    filename = save(original)
    assert filename == config_dir / "profile.json"
    raw = filename.read_text(encoding="utf-8")
    assert original.secret not in raw
    assert "secret_protected" in raw
    assert "secret" not in public_dict(original)
    assert TOKEN not in json.dumps(public_dict(original), ensure_ascii=False)
    loaded = load()
    assert loaded.secret == TOKEN
    assert not loaded.endpoint_verified
    assert not loaded.provenance
    assert not loaded.proof
    assert loaded.endpoint_url == ""
    assert loaded.tools_config == str(config_dir / "tools.yaml")


def test_multi_workspace_registry_round_trips_and_mirrors_legacy_fields(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    value = profile(
        tmp_path,
        workspaces=[
            {"workspace_id": "localpc", "path": str(first)},
            {"workspace_id": "second", "path": str(second)},
        ],
    )
    filename = save(value, tmp_path / "multi")
    loaded = load(filename)
    assert loaded.workspace_id == "localpc"
    assert Path(loaded.workspace_path) == first.resolve()
    assert loaded.workspaces == [
        {"workspace_id": "localpc", "path": str(first.resolve())},
        {"workspace_id": "second", "path": str(second.resolve())},
    ]


def test_multi_workspace_registry_rejects_duplicate_ids_or_paths(tmp_path):
    first = tmp_path / "first"
    first.mkdir()
    duplicate_id = profile(
        tmp_path,
        workspaces=[
            {"workspace_id": "same", "path": str(first)},
            {"workspace_id": "same", "path": str(tmp_path)},
        ],
    )
    assert any("重复" in error for error in validate(duplicate_id))
    duplicate_path = profile(
        tmp_path,
        workspaces=[
            {"workspace_id": "one", "path": str(first)},
            {"workspace_id": "two", "path": str(first)},
        ],
    )
    assert any("重复" in error for error in validate(duplicate_path))


def test_provision_token_is_dpapi_protected_and_restored(tmp_path, monkeypatch):
    config_dir = tmp_path / "token-config"
    monkeypatch.setenv("KT_MCP_CLIENT_CONFIG_DIR", str(config_dir))
    original = profile(tmp_path, secret="")
    original.kt_home = str(config_dir / "kt-home")
    original.provision_token = "enrollment-token"
    filename = save(original)
    raw = filename.read_text(encoding="utf-8")
    assert original.provision_token not in raw
    assert "provision_token_protected" in raw
    loaded = load()
    assert loaded.provision_token == original.provision_token
    assert "provision_token" not in public_dict(original)


def test_last_successful_connection_protects_key_and_survives_failed_profile_update(tmp_path):
    value = profile(tmp_path, provision_token="last-good-key")
    filename = remember_successful_connection(value, tmp_path)
    assert filename.name == LAST_CONNECTION_FILENAME
    raw = filename.read_text(encoding="utf-8")
    assert "last-good-key" not in raw
    assert "provision_token_protected" in raw
    if sys.platform == "win32":
        assert "dpapi:" in raw
    assert load_successful_connection(tmp_path) == (value.public_origin, "last-good-key")
    value.public_origin = "https://bad-next.example.invalid"
    value.provision_token = "invalid-later-key"
    save(value, tmp_path)
    assert load_successful_connection(tmp_path) == ("https://device.example.invalid", "last-good-key")
    assert "invalid-later-key" not in filename.read_text(encoding="utf-8")


def test_last_connection_migrates_legacy_profile_without_trusting_endpoint(tmp_path):
    value = profile(tmp_path, provision_token="legacy-key")
    save(value, tmp_path)
    assert load_successful_connection(tmp_path) == (value.public_origin, "legacy-key")
    assert (tmp_path / LAST_CONNECTION_FILENAME).exists()
    value.provision_token = "failed-new-key"
    save(value, tmp_path)
    assert load_successful_connection(tmp_path)[1] == "legacy-key"
    assert not load(tmp_path).endpoint_verified


def test_last_connection_bad_data_never_returns_credentials(tmp_path):
    file = tmp_path / LAST_CONNECTION_FILENAME
    file.write_text('{"version": 1, "origin": "http://unsafe", "provision_token_protected":""}')
    with pytest.raises(ConfigError):
        load_successful_connection(tmp_path)
    file.write_text("not-json")
    with pytest.raises(ConfigError):
        load_successful_connection(tmp_path)


def test_last_connection_new_install_is_empty(tmp_path):
    assert load_successful_connection(tmp_path) == ("", "")


def test_actual_endpoint_url_is_required_and_migrates_legacy_secret(tmp_path):
    value = profile(tmp_path, secret=TOKEN)
    with pytest.raises(ConfigError):
        mcp_url(value)
    actual = f"https://device.example.invalid/mcp/{TOKEN_2}"
    assert set_endpoint(value, actual) == actual
    assert value.secret == TOKEN_2
    assert mcp_url(value) == actual
    assert value.mcp_path == DEFAULT_MCP_PATH


def test_endpoint_set_accepts_registered_session_proof(tmp_path):
    value = profile(tmp_path)
    assert set_test_endpoint(value, f"https://device.example.invalid/mcp/{TOKEN_2}")


def test_endpoint_set_rejects_unregistered_proof(tmp_path):
    value = profile(tmp_path)
    value._active_runtime_proof = "test-proof"
    with pytest.raises(ConfigError, match="session proof"):
        set_actual_endpoint(
            value,
            f"https://device.example.invalid/mcp/{TOKEN_2}",
            proof="other-proof",
        )



def test_failed_endpoint_refresh_invalidates_previous_endpoint(tmp_path):
    value = profile(tmp_path)
    set_endpoint(value, f"https://device.example.invalid/mcp/{TOKEN_2}")
    value._active_runtime_proof = "test-proof"
    with pytest.raises(ConfigError):
        set_actual_endpoint(
            value,
            f"https://other.example.invalid/mcp/{TOKEN}",
            proof="test-proof",
        )
    with pytest.raises(ConfigError):
        mcp_url(value)


def test_origin_host_is_canonicalized_like_upstream_kt(tmp_path):
    value = profile(tmp_path, public_origin="https://DEVICE.Example.invalid/")
    assert canonicalize_origin(value.public_origin) == "https://device.example.invalid"
    assert set_endpoint(value, f"https://device.example.invalid/mcp/{TOKEN_2}").startswith(
        "https://device.example.invalid/"
    )


def test_endpoint_parser_rejects_invalid_url_and_secret():
    with pytest.raises(ConfigError):
        parse_endpoint_url("http://device.example.invalid/mcp/" + TOKEN)
    with pytest.raises(ConfigError):
        parse_endpoint_url("https://device.example.invalid/not-mcp/" + TOKEN)
    with pytest.raises(ConfigError):
        parse_endpoint_url("https://device.example.invalid/mcp/short")


def test_strict_origin_path_and_mcp_path_validation(tmp_path):
    assert validate(profile(tmp_path, public_origin="https://device.example.invalid/path"))
    assert validate(profile(tmp_path, public_origin="http://device.example.invalid"))
    assert validate(profile(tmp_path, public_origin="https://device.example.invalid/?x=1"))
    assert validate(profile(tmp_path, mcp_path="/other/"))
    assert not validate(profile(tmp_path))


def test_validation_rejects_production_port_and_old_bridge_home(tmp_path):
    value = profile(
        tmp_path,
        local_port=8765,
        kt_home="C:/Users/Administrator/AppData/Local/KT-MCP-Bridge/kt-home",
    )
    errors = validate(value)
    assert any("8765" in error for error in errors)
    assert any("Bridge" in error for error in errors)


def test_old_config_root_is_rejected_before_save_creates_directory(tmp_path):
    forbidden = tmp_path / "KT-MCP-Bridge" / "new-config"
    with pytest.raises(ConfigError):
        save(profile(tmp_path), forbidden)
    assert not forbidden.exists()


def test_runtime_root_must_be_inside_new_config_root(tmp_path):
    config_root = tmp_path / "config"
    with pytest.raises(ConfigError):
        canonicalize_runtime_root(tmp_path / "elsewhere" / "runtime", config_root)
    assert canonicalize_runtime_root(config_root / "runtime", config_root) == (config_root / "runtime").resolve()


def test_production_kt_python_is_rejected_even_when_current_bridge_python(tmp_path, monkeypatch):
    production_python = "C:/Users/Administrator/AppData/Local/KT-MCP-Bridge/env/python.exe"
    monkeypatch.setenv("KT_MCP_BRIDGE_PYTHON", production_python)
    with pytest.raises(ConfigError):
        canonicalize_kt_python(production_python)
    with pytest.raises(ConfigError):
        canonicalize_kt_python("C:/Users/Administrator/AppData/Local/KT-MCP-Bridge/env/Scripts/python.exe")


def test_plaintext_secret_in_json_is_rejected(tmp_path):
    filename = tmp_path / "profile.json"
    filename.write_text(json.dumps({"profile": {"secret": "plaintext"}, "secret_protected": ""}), encoding="utf-8")
    with pytest.raises(ConfigError):
        load(filename)


def test_unverified_marker_flag_must_be_boolean(tmp_path):
    value = profile(tmp_path, secret=TOKEN)
    set_endpoint(value, f"https://device.example.invalid/mcp/{TOKEN_2}")
    filename = save(value, tmp_path / "typed-marker")
    payload = json.loads(filename.read_text(encoding="utf-8"))
    payload["endpoint_verified"] = "true"
    filename.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load(tmp_path / "typed-marker")
    assert not loaded.endpoint_verified
    assert not loaded.provenance
    assert not loaded.proof


def test_endpoint_marker_requires_valid_verified_secret_and_invalidates_identity_changes(tmp_path):
    value = profile(tmp_path, secret=TOKEN)
    set_endpoint(value, f"https://device.example.invalid/mcp/{TOKEN_2}")
    assert value.endpoint_verified
    assert value.mcp_url().endswith(TOKEN_2)
    invalid = profile(tmp_path, secret="", endpoint_verified=True)
    save(invalid, tmp_path / "invalid-config")
    assert not load(tmp_path / "invalid-config").endpoint_verified
    unverified_marker = profile(tmp_path, endpoint_verified=True)
    with pytest.raises(ConfigError):
        unverified_marker.mcp_url()
    save(unverified_marker, tmp_path / "unverified-config")
    assert not load(tmp_path / "unverified-config").endpoint_verified


def test_loaded_profile_cannot_reuse_persisted_endpoint_marker(tmp_path):
    value = profile(tmp_path)
    endpoint = f"https://device.example.invalid/mcp/{TOKEN_2}"
    set_endpoint(value, endpoint)
    save(value, tmp_path / "config")
    loaded = load(tmp_path / "config")
    assert not loaded.endpoint_verified
    assert not loaded.provenance
    assert not loaded.proof
    with pytest.raises(ConfigError):
        loaded.mcp_url()


def test_identity_changes_invalidate_current_endpoint_before_save(tmp_path):
    value = profile(tmp_path)
    set_endpoint(value, f"https://device.example.invalid/mcp/{TOKEN_2}")
    value.local_port += 1
    save(value, tmp_path / "config")
    assert not value.endpoint_verified
    assert not value.provenance
    assert not load(tmp_path / "config").endpoint_verified
