"""Provisioning client regression tests."""

import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from kt_mcp_client import provisioning
from tests.helpers import set_test_endpoint
from kt_mcp_client.config import DeviceProfile, load, save
from kt_mcp_client.provisioning import (
    DEFAULT_ORIGIN,
    KT_MCP_PROVISION_TOKEN,
    MAX_RESPONSE_BYTES,
    PROVISIONING_NOT_CONFIGURED_MESSAGE,
    ProvisioningError,
    ProvisioningUnavailable,
    discover_default_origin,
    discover_production_origin,
    get_installation_id,
    installation_id_path,
    provision_and_save,
    request_independent_endpoint,
)

TOKEN = "P" * 43
OTHER_TOKEN = "Q" * 43
ENDPOINT = f"https://device.example.invalid/mcp/{TOKEN}"
OTHER_ENDPOINT = f"https://device.example.invalid/mcp/{OTHER_TOKEN}"


class FakeResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self.status = status
        self.payload = payload
        self.closed = False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def close(self) -> None:
        self.closed = True


class SizedResponse:
    def __init__(self, raw: bytes, content_length: str | None = None) -> None:
        self.raw = raw
        self.content_length = content_length
        self.closed = False
        self.read_size: int | None = None

    def getheader(self, name: str):
        if name.casefold() == "content-length":
            return self.content_length
        return None

    def read(self, size: int | None = None) -> bytes:
        self.read_size = size
        return self.raw if size is None else self.raw[:size]

    def close(self) -> None:
        self.closed = True


def test_response_size_limit_rejects_oversized_content_length_and_closes():
    response = SizedResponse(b"{}", str(MAX_RESPONSE_BYTES + 1))
    with pytest.raises(ProvisioningError, match="响应过大"):
        provisioning._read_response(response)
    assert response.closed is True
    assert response.read_size is None


def test_response_size_limit_rejects_oversized_actual_body_and_closes():
    response = SizedResponse(b"x" * (MAX_RESPONSE_BYTES + 1))
    with pytest.raises(ProvisioningError, match="响应过大"):
        provisioning._read_response(response)
    assert response.closed is True
    assert response.read_size == MAX_RESPONSE_BYTES + 1


def test_response_size_limit_keeps_zero_argument_test_double_compatible():
    response = FakeResponse({"ok": True})
    assert provisioning._read_response(response) == b'{"ok": true}'
    assert response.closed is True


def verified_profile(tmp_path: Path, endpoint: str = ENDPOINT) -> DeviceProfile:
    profile = DeviceProfile(
        kt_home=str(tmp_path / "kt-home"),
        public_origin="https://device.example.invalid",
        secret="legacy-" + TOKEN,
    )
    set_test_endpoint(profile, endpoint)
    return profile


def verified_config(tmp_path: Path, endpoint: str = ENDPOINT) -> Path:
    config = tmp_path / "independent"
    save(verified_profile(tmp_path, endpoint), config)
    return config


def provision_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KT_MCP_PROVISION_URL", "https://provision.example.invalid/api")
    monkeypatch.setenv("KT_MCP_PROVISION_ORIGIN_MAP", '{"https://device.example.invalid": ["provision.example.invalid"]}')
    monkeypatch.setenv(KT_MCP_PROVISION_TOKEN, "enrollment-token")


def test_production_origin_discovery_uses_public_origin_only(tmp_path):
    secret = "production-secret-must-not-be-used"
    connection = tmp_path / "connection.json"
    connection.write_text(
        json.dumps(
            {
                "public_origin": "https://production.example.invalid/",
                "url": f"https://production.example.invalid/mcp/{secret}",
                "secret": secret,
            }
        ),
        encoding="utf-8",
    )
    assert discover_production_origin(connection) == "https://production.example.invalid"
    assert secret not in discover_default_origin(connection)


def test_origin_discovery_falls_back_for_missing_or_invalid_file(tmp_path):
    missing = tmp_path / "missing.json"
    invalid = tmp_path / "invalid.json"
    invalid.write_text("not json", encoding="utf-8")
    malformed = tmp_path / "malformed.json"
    malformed.write_text(json.dumps({"public_origin": "http://not-https.example"}), encoding="utf-8")
    assert discover_default_origin(missing) == DEFAULT_ORIGIN
    assert discover_default_origin(invalid) == DEFAULT_ORIGIN
    assert discover_default_origin(malformed) == DEFAULT_ORIGIN


def test_provisioning_posts_canonical_endpoint_and_persistent_installation_id(tmp_path, monkeypatch):
    provision_env(monkeypatch)
    calls: list[Request] = []

    def opener(request: Request, *, timeout: float):
        calls.append(request)
        return FakeResponse({"url": ENDPOINT, "remote_port": 18766})

    config = verified_config(tmp_path)
    assert request_independent_endpoint(
        "https://device.example.invalid/", ENDPOINT, profile=verified_profile(tmp_path), config_path=config, opener=opener
    ) == ENDPOINT
    assert request_independent_endpoint(
        "https://device.example.invalid", ENDPOINT, profile=verified_profile(tmp_path), config_path=config, opener=opener
    ) == ENDPOINT
    assert len(calls) == 2
    payload = json.loads(calls[0].data.decode("utf-8"))
    assert calls[0].get_full_url() == "https://provision.example.invalid/api"
    assert calls[0].method == "POST"
    assert payload == {
        "origin": "https://device.example.invalid",
        "endpoint_url": ENDPOINT,
        "installation_id": payload["installation_id"],
    }
    assert payload["installation_id"] == json.loads(calls[1].data.decode("utf-8"))["installation_id"]
    assert installation_id_path(config).read_text(encoding="ascii").strip() == payload["installation_id"]


def test_provisioning_sends_enrollment_token(tmp_path, monkeypatch):
    provision_env(monkeypatch)

    def opener(request: Request, *, timeout: float):
        assert request.get_header("X-kt-provision-token") == "enrollment-token"
        return FakeResponse({"url": ENDPOINT, "remote_port": 18766})

    config = verified_config(tmp_path)
    assert request_independent_endpoint(
        "https://device.example.invalid", ENDPOINT, profile=verified_profile(tmp_path), config_path=config, opener=opener
    ) == ENDPOINT


def test_provisioning_requires_enrollment_token(tmp_path, monkeypatch):
    monkeypatch.setenv("KT_MCP_PROVISION_URL", "https://provision.example.invalid/api")
    monkeypatch.setenv("KT_MCP_PROVISION_ORIGIN_MAP", '{"https://device.example.invalid": ["provision.example.invalid"]}')
    monkeypatch.delenv(KT_MCP_PROVISION_TOKEN, raising=False)
    with pytest.raises(ProvisioningUnavailable, match="KT_MCP_PROVISION_TOKEN"):
        request_independent_endpoint(
            "https://device.example.invalid", ENDPOINT, profile=verified_profile(tmp_path), config_path=tmp_path,
            opener=lambda *args, **kwargs: pytest.fail("network must not be called"),
        )


def test_provisioning_rejects_http_override_before_sending_installation_id(tmp_path, monkeypatch):
    monkeypatch.setenv("KT_MCP_PROVISION_URL", "http://provision.example.invalid/api")
    monkeypatch.setenv(KT_MCP_PROVISION_TOKEN, "enrollment-token")
    with pytest.raises(ProvisioningUnavailable, match="注册地址无效"):
        request_independent_endpoint("https://device.example.invalid", ENDPOINT, profile=verified_profile(tmp_path), config_path=tmp_path)


def test_client_redirect_opener_does_not_forward_token(tmp_path, monkeypatch):
    provision_env(monkeypatch)
    monkeypatch.setattr(
        provisioning.urllib.request,
        "build_opener",
        lambda handler: type(
            "Opener",
            (),
            {"open": lambda self, request, timeout: (_ for _ in ()).throw(
                HTTPError(request.full_url, 302, "redirect", {}, None)
            )},
        )(),
    )
    with pytest.raises(ProvisioningError, match="请求失败"):
        request_independent_endpoint("https://device.example.invalid", ENDPOINT, profile=verified_profile(tmp_path), config_path=tmp_path)


def test_provisioning_rejects_response_that_changes_endpoint_secret(tmp_path, monkeypatch):
    provision_env(monkeypatch)
    with pytest.raises(ProvisioningError, match="不一致"):
        request_independent_endpoint(
            "https://device.example.invalid", ENDPOINT, profile=verified_profile(tmp_path), config_path=tmp_path,
            opener=lambda request, timeout: FakeResponse({"endpoint_url": OTHER_ENDPOINT}),
        )


def test_provisioning_rejects_response_from_another_origin(tmp_path, monkeypatch):
    provision_env(monkeypatch)
    other = f"https://other.example.invalid/mcp/{TOKEN}"
    with pytest.raises(ProvisioningError, match="origin"):
        request_independent_endpoint(
            "https://device.example.invalid", ENDPOINT, profile=verified_profile(tmp_path), config_path=tmp_path,
            opener=lambda request, timeout: FakeResponse({"endpoint_url": other}),
        )


def test_provisioning_rejects_production_remote_port(tmp_path, monkeypatch):
    provision_env(monkeypatch)
    with pytest.raises(ProvisioningError, match="remote_port"):
        request_independent_endpoint(
            "https://device.example.invalid", ENDPOINT, profile=verified_profile(tmp_path), config_path=tmp_path,
            opener=lambda request, timeout: FakeResponse({"url": ENDPOINT, "remote_port": 18765}),
        )


def test_installation_id_is_stable_and_non_secret(tmp_path):
    first = get_installation_id(tmp_path)
    second = get_installation_id(tmp_path)
    assert first == second
    assert len(first) == 32
    assert first.encode("ascii") in installation_id_path(tmp_path).read_bytes()


def test_unavailable_provisioning_api_has_clear_message(tmp_path, monkeypatch):
    provision_env(monkeypatch)
    with pytest.raises(ProvisioningUnavailable, match="无法连接") as error:
        request_independent_endpoint(
            "https://device.example.invalid", ENDPOINT, profile=verified_profile(tmp_path), config_path=tmp_path,
            opener=lambda request, timeout: (_ for _ in ()).throw(URLError("offline")),
        )
    assert "注册结果尚未确认" in str(error.value)
    assert ENDPOINT not in str(error.value)


def test_404_provisioning_api_has_clear_message(tmp_path, monkeypatch):
    provision_env(monkeypatch)
    with pytest.raises(ProvisioningUnavailable, match="HTTP 404"):
        request_independent_endpoint(
            "https://device.example.invalid", ENDPOINT, profile=verified_profile(tmp_path), config_path=tmp_path,
            opener=lambda request, timeout: FakeResponse({"error": "not found"}, status=404),
        )


def test_provision_and_save_requires_verified_local_endpoint(tmp_path, monkeypatch):
    provision_env(monkeypatch)
    with pytest.raises(ProvisioningError, match="profile endpoint|内存 provenance"):
        provision_and_save(
            "https://device.example.invalid", ENDPOINT, tmp_path / "independent",
            profile=DeviceProfile(public_origin="https://device.example.invalid"),
            opener=lambda *args, **kwargs: pytest.fail("network must not be called"),
        )


def test_provision_and_save_requires_request_endpoint_to_match_profile(tmp_path, monkeypatch):
    provision_env(monkeypatch)
    config = verified_config(tmp_path)
    current = verified_profile(tmp_path)
    with pytest.raises(ProvisioningError, match="不一致"):
        provision_and_save(
            "https://device.example.invalid", OTHER_ENDPOINT, config, profile=current,
            opener=lambda *args, **kwargs: pytest.fail("network must not be called"),
        )


def test_provision_and_save_only_updates_verified_independent_profile(tmp_path, monkeypatch):
    provision_env(monkeypatch)
    config = verified_config(tmp_path)
    current = verified_profile(tmp_path)
    calls: list[Request] = []

    def opener(request: Request, *, timeout: float):
        calls.append(request)
        return FakeResponse({"url": ENDPOINT, "remote_port": 18766})

    assert provision_and_save(
        "https://device.example.invalid", ENDPOINT, profile=verified_profile(tmp_path), config_path=config, opener=opener
    ) == ENDPOINT
    assert len(calls) == 1


def test_provision_and_save_does_not_save_mismatched_response(tmp_path, monkeypatch):
    provision_env(monkeypatch)
    config = verified_config(tmp_path)
    current = verified_profile(tmp_path)
    with pytest.raises(ProvisioningError, match="不一致"):
        provision_and_save(
            "https://device.example.invalid", ENDPOINT, config, profile=current,
            opener=lambda request, timeout: FakeResponse({"url": OTHER_ENDPOINT}),
        )
    assert not load(config).endpoint_verified
