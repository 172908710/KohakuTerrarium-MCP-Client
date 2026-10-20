"""Tests for the un-deployed server-side provisioning scaffold."""

import asyncio
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.app import AppSecurity, create_app
from server.registry import (
    AllocationRegistry,
    EndpointConflict,
    InvalidInstallationID,
    OriginMismatch,
    RegistryError,
    RegistrySettings,
)
from server.service import MAX_BODY_BYTES, RateLimiter


ORIGIN = "https://mcp.example.test"
FIRST_ENDPOINT = f"{ORIGIN}/mcp/{'A' * 43}"
SECOND_ENDPOINT = f"{ORIGIN}/mcp/{'B' * 43}"


def _registry(tmp_path: Path, *, reload=None, reserved_secret_hashes=()) -> AllocationRegistry:
    return AllocationRegistry(
        RegistrySettings(
            db_path=tmp_path / "registry.sqlite3",
            routes_path=tmp_path / "routes.map",
            public_origin=ORIGIN,
            port_start=18766,
            port_end=18770,
            reserved_secret_hashes=reserved_secret_hashes,
        ),
        # ``reload=None`` remains the test helper override: no real Nginx
        # helper is invoked in unit tests.
        reload=(reload if reload is not None else lambda: None),
    )


def test_two_installations_get_distinct_secret_and_port(tmp_path):
    registry = _registry(tmp_path)
    first = registry.provision("a" * 32, ORIGIN, FIRST_ENDPOINT)
    second = registry.provision("b" * 32, ORIGIN, SECOND_ENDPOINT)

    assert first.secret != second.secret
    assert first.remote_port != second.remote_port
    assert first.remote_port == 18766
    assert second.remote_port == 18767
    routes = (tmp_path / "routes.map").read_text(encoding="ascii")
    assert f"location = /mcp/{first.secret} {{" in routes
    assert f"    proxy_pass http://127.0.0.1:18766;" in routes
    assert f"location = /mcp/{second.secret} {{" in routes
    assert f"    proxy_pass http://127.0.0.1:18767;" in routes
    assert 'proxy_set_header Upgrade $http_upgrade;' in routes
    assert 'proxy_set_header Connection "upgrade";' in routes
    assert "proxy_set_header Host $host;" in routes
    assert "proxy_set_header X-Real-IP $remote_addr;" in routes
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in routes
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in routes
    assert "proxy_read_timeout 86400;" in routes
    assert "proxy_buffering off;" in routes
    assert '"/mcp/' not in routes


def test_repeated_provisioning_is_idempotent(tmp_path):
    registry = _registry(tmp_path)
    first = registry.provision("a" * 32, ORIGIN, FIRST_ENDPOINT)
    second = registry.provision("a" * 32, ORIGIN, FIRST_ENDPOINT)
    assert second == first
    assert len(registry.list_allocations()) == 1


def test_production_port_cannot_be_in_pool(tmp_path):
    with pytest.raises(ValueError, match="18765"):
        RegistrySettings(
            db_path=tmp_path / "db",
            routes_path=tmp_path / "map",
            public_origin=ORIGIN,
            port_start=18765,
            port_end=18766,
        )


def test_routes_path_must_share_db_directory(tmp_path):
    with pytest.raises(ValueError, match="same directory"):
        RegistrySettings(
            db_path=tmp_path / "registry" / "registry.sqlite3",
            routes_path=tmp_path / "other" / "routes.map",
            public_origin=ORIGIN,
        )


def test_routes_path_cannot_replace_db_file(tmp_path):
    db_path = tmp_path / "registry.sqlite3"
    with pytest.raises(ValueError, match="must not overwrite"):
        RegistrySettings(
            db_path=db_path,
            routes_path=db_path,
            public_origin=ORIGIN,
        )


def test_reserved_secret_hash_is_rejected_without_exposing_secret(tmp_path):
    secret = "R" * 43
    reserved_hash = hashlib.sha256(secret.encode("ascii")).hexdigest()
    registry = _registry(tmp_path, reserved_secret_hashes={reserved_hash})

    with pytest.raises(EndpointConflict, match="reserved") as error:
        registry.provision("a" * 32, ORIGIN, f"{ORIGIN}/mcp/{secret}")

    assert secret not in str(error.value)
    assert registry.list_allocations() == []
    assert secret not in (tmp_path / "routes.map").read_text(encoding="ascii")


def test_reserved_secret_hashes_are_loaded_from_environment(tmp_path, monkeypatch):
    secret = "S" * 43
    reserved_hash = hashlib.sha256(secret.encode("ascii")).hexdigest().upper()
    monkeypatch.setenv("KT_MCP_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv("KT_MCP_REGISTRY_DB", str(tmp_path / "registry.sqlite3"))
    monkeypatch.setenv("KT_MCP_ROUTES_MAP", str(tmp_path / "routes.map"))
    monkeypatch.setenv("KT_MCP_RESERVED_SECRET_HASHES", f" {reserved_hash}, ")
    settings = RegistrySettings.from_env()
    assert settings.reserved_secret_hashes == frozenset({reserved_hash.lower()})


def test_wrong_origin_is_rejected(tmp_path):
    registry = _registry(tmp_path)
    with pytest.raises(OriginMismatch):
        registry.provision("a" * 32, "https://other.example.test", "https://other.example.test/mcp/" + "C" * 43)


def test_api_does_not_return_url_when_route_reload_fails(tmp_path):
    def reload_failure():
        raise RuntimeError("nginx failed")

    registry = _registry(tmp_path, reload=reload_failure)
    client = TestClient(create_app(registry, security=AppSecurity(admin_token="test-token")))
    response = client.post(
        "/provision",
        headers={"X-KT-Provision-Token": "test-token"},
        json={"origin": ORIGIN, "installation_id": "a" * 32, "endpoint_url": FIRST_ENDPOINT},
    )
    assert response.status_code == 503
    assert "url" not in response.json()
    assert registry.list_allocations() == []


def test_fastapi_rejects_oversized_provision_body(tmp_path):
    registry = _registry(tmp_path)
    client = TestClient(
        create_app(registry, security=AppSecurity(admin_token="staging-token"))
    )
    response = client.post(
        "/provision",
        headers={"X-KT-Provision-Token": "staging-token"},
        content=b"x" * (MAX_BODY_BYTES + 1),
    )
    assert response.status_code == 413
    assert registry.list_allocations() == []


def _asgi_post_status(app, body: bytes, headers: dict[str, str]) -> int:
    async def invoke() -> int:
        messages: list[dict] = []
        delivered = False

        async def receive() -> dict:
            nonlocal delivered
            if delivered:
                return {"type": "http.disconnect"}
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message: dict) -> None:
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/provision",
            "raw_path": b"/provision",
            "query_string": b"",
            "headers": [(key.encode(), value.encode()) for key, value in headers.items()],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "root_path": "",
        }
        await app(scope, receive, send)
        return next(message["status"] for message in messages if message["type"] == "http.response.start")

    return asyncio.run(invoke())


def test_fastapi_rejects_chunked_provision_body_without_content_length(tmp_path):
    registry = _registry(tmp_path)
    app = create_app(registry, security=AppSecurity(admin_token="staging-token"))
    body = b"{}"
    assert _asgi_post_status(
        app,
        body,
        {
            "x-kt-provision-token": "staging-token",
            "content-type": "application/json",
            # No Content-Length simulates a chunked/unknown-length request.
        },
    ) == 413
    assert registry.list_allocations() == []


def test_fastapi_rejects_actual_body_over_limit_even_with_small_header(tmp_path):
    registry = _registry(tmp_path)
    app = create_app(registry, security=AppSecurity(admin_token="staging-token"))
    assert _asgi_post_status(
        app,
        b"x" * (MAX_BODY_BYTES + 1),
        {
            "x-kt-provision-token": "staging-token",
            "content-type": "application/json",
            "content-length": "1",
        },
    ) == 413
    assert registry.list_allocations() == []


def test_fastapi_replays_small_provision_body_to_endpoint(tmp_path):
    registry = _registry(tmp_path)
    client = TestClient(
        create_app(registry, security=AppSecurity(admin_token="staging-token"))
    )
    body = {
        "origin": ORIGIN,
        "installation_id": "a" * 32,
        "endpoint_url": FIRST_ENDPOINT,
    }
    response = client.post(
        "/provision",
        headers={"X-KT-Provision-Token": "staging-token"},
        json=body,
    )
    assert response.status_code == 200
    assert response.json()["url"] == FIRST_ENDPOINT


def test_api_returns_device_url_and_remote_port_and_requires_optional_auth(tmp_path):
    registry = _registry(tmp_path)
    client = TestClient(
        create_app(registry, security=AppSecurity(admin_token="staging-token"))
    )
    body = {"origin": ORIGIN, "installation_id": "a" * 32, "endpoint_url": FIRST_ENDPOINT}
    assert client.post("/provision", json=body).status_code == 401
    response = client.post(
        "/provision",
        headers={"X-KT-Provision-Token": "staging-token"},
        json=body,
    )
    assert response.status_code == 200
    url = response.json()["url"]
    assert url.startswith(f"{ORIGIN}/mcp/")
    assert url == FIRST_ENDPOINT
    assert response.json()["remote_port"] == 18766


def test_missing_reload_helper_fails_closed(tmp_path):
    settings = RegistrySettings(
        db_path=tmp_path / "db",
        routes_path=tmp_path / "map",
        public_origin=ORIGIN,
    )
    with pytest.raises(RegistryError, match="reload helper"):
        AllocationRegistry(settings)


def test_same_installation_cannot_change_secret(tmp_path):
    registry = _registry(tmp_path)
    registry.provision("a" * 32, ORIGIN, FIRST_ENDPOINT)
    with pytest.raises(EndpointConflict):
        registry.provision("a" * 32, ORIGIN, SECOND_ENDPOINT)


def test_secret_cannot_be_reused_by_another_installation(tmp_path):
    registry = _registry(tmp_path)
    registry.provision("a" * 32, ORIGIN, FIRST_ENDPOINT)
    with pytest.raises(EndpointConflict):
        registry.provision("b" * 32, ORIGIN, FIRST_ENDPOINT)


def test_endpoint_origin_must_match_request_origin(tmp_path):
    registry = _registry(tmp_path)
    with pytest.raises(OriginMismatch):
        registry.provision("a" * 32, ORIGIN, "https://other.example.test/mcp/" + "C" * 43)


def test_invalid_installation_id_is_rejected_before_allocation(tmp_path):
    registry = _registry(tmp_path)
    with pytest.raises(InvalidInstallationID):
        registry.provision("not-an-installation", ORIGIN, FIRST_ENDPOINT)
    assert registry.list_allocations() == []


def test_app_rejects_unconfigured_security(tmp_path):
    client = TestClient(create_app(_registry(tmp_path), security=AppSecurity()))
    response = client.post(
        "/provision",
        json={"origin": ORIGIN, "installation_id": "a" * 32, "endpoint_url": FIRST_ENDPOINT},
    )
    assert response.status_code == 503


def test_health_has_no_provisioning_details(tmp_path):
    client = TestClient(create_app(_registry(tmp_path), security=AppSecurity(admin_token="test-token")))
    assert client.get("/healthz").json() == {"status": "ok"}


def test_fastapi_rate_limits_auth_attempts_and_ignores_spoofed_peer_header(tmp_path):
    registry = _registry(tmp_path)
    client = TestClient(
        create_app(
            registry,
            security=AppSecurity(admin_token="test-token"),
            rate_limiter=RateLimiter(limit=1, window=600),
        )
    )
    body = {"origin": ORIGIN, "installation_id": "a" * 32, "endpoint_url": FIRST_ENDPOINT}
    assert client.post(
        "/provision",
        headers={"X-KT-Provision-Token": "wrong", "X-Real-IP": "198.51.100.1"},
        json=body,
    ).status_code == 401
    assert client.post(
        "/provision",
        headers={"X-KT-Provision-Token": "test-token", "X-Real-IP": "198.51.100.2"},
        json=body,
    ).status_code == 429
