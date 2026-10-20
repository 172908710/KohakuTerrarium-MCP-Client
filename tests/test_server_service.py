"""HTTP-level tests for the dependency-free provisioning service."""

import http.client
import json
import threading
from pathlib import Path

from server.registry import AllocationRegistry, RegistrySettings
from server.service import ProvisioningHTTPServer


ORIGIN = "https://mcp.example.test"
ENDPOINT = f"{ORIGIN}/mcp/{'A' * 43}"


def _server(tmp_path: Path, token: str = "test-token") -> ProvisioningHTTPServer:
    registry = AllocationRegistry(
        RegistrySettings(
            db_path=tmp_path / "registry.sqlite3",
            routes_path=tmp_path / "routes.map",
            public_origin=ORIGIN,
            port_start=18766,
            port_end=18770,
        ),
        reload=lambda: None,
    )
    server = ProvisioningHTTPServer(("127.0.0.1", 0), registry, provision_token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _request(server: ProvisioningHTTPServer, method: str, path: str, body=None, headers=None):
    connection = http.client.HTTPConnection(*server.server_address, timeout=3)
    encoded = None if body is None else json.dumps(body).encode("utf-8")
    request_headers = {"Connection": "close"}
    if encoded is not None:
        request_headers.update({"Content-Type": "application/json", "Content-Length": str(len(encoded)), "X-KT-Provision-Token": "test-token"})
    request_headers.update(headers or {})
    connection.request(method, path, body=encoded, headers=request_headers)
    response = connection.getresponse()
    raw = response.read()
    status = response.status
    connection.close()
    return status, json.loads(raw.decode("utf-8"))


def test_service_health_and_unknown_paths(tmp_path):
    server = _server(tmp_path)
    try:
        assert _request(server, "GET", "/healthz") == (200, {"status": "ok"})
        assert _request(server, "GET", "/nope")[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_service_provisions_and_is_idempotent(tmp_path):
    server = _server(tmp_path)
    body = {"origin": ORIGIN, "installation_id": "a" * 32, "endpoint_url": ENDPOINT}
    try:
        first_status, first = _request(server, "POST", "/provision", body)
        second_status, second = _request(server, "POST", "/provision", body)
        assert first_status == second_status == 200
        assert first == second
        assert first["url"] == ENDPOINT
        assert first["remote_port"] == 18766
    finally:
        server.shutdown()
        server.server_close()


def test_service_writes_server_context_route_include_with_mcp_headers(tmp_path):
    server = _server(tmp_path)
    body = {"origin": ORIGIN, "installation_id": "a" * 32, "endpoint_url": ENDPOINT}
    try:
        assert _request(server, "POST", "/provision", body)[0] == 200
        routes = (tmp_path / "routes.map").read_text(encoding="ascii")
        assert f"location = /mcp/{'A' * 43} {{" in routes
        assert "    proxy_pass http://127.0.0.1:18766;" in routes
        assert "    proxy_http_version 1.1;" in routes
        assert "    proxy_set_header Upgrade $http_upgrade;" in routes
        assert '    proxy_set_header Connection "upgrade";' in routes
        assert "    proxy_set_header Host $host;" in routes
        assert "    proxy_set_header X-Real-IP $remote_addr;" in routes
        assert "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in routes
        assert "    proxy_set_header X-Forwarded-Proto $scheme;" in routes
        assert "    proxy_read_timeout 86400;" in routes
        assert "    proxy_buffering off;" in routes
    finally:
        server.shutdown()
        server.server_close()


def test_service_rejects_missing_or_wrong_auth(tmp_path):
    server = _server(tmp_path, token="token")
    body = {"origin": ORIGIN, "installation_id": "a" * 32, "endpoint_url": ENDPOINT}
    try:
        assert _request(server, "POST", "/provision", body)[0] == 401
        assert _request(
            server,
            "POST",
            "/provision",
            body,
            headers={"X-KT-Provision-Token": "token"},
        )[0] == 200
    finally:
        server.shutdown()
        server.server_close()


def test_service_rejects_missing_endpoint_url(tmp_path):
    server = _server(tmp_path)
    try:
        status, payload = _request(
            server, "POST", "/provision", {"origin": ORIGIN, "installation_id": "a" * 32}
        )
        assert status == 400
        assert payload["error"] == "invalid provisioning request"
    finally:
        server.shutdown()
        server.server_close()


def test_service_rejects_unknown_origin_without_creating_route(tmp_path):
    server = _server(tmp_path)
    body = {
        "origin": "https://other.example.test",
        "installation_id": "a" * 32,
        "endpoint_url": "https://other.example.test/mcp/" + "B" * 43,
    }
    try:
        assert _request(server, "POST", "/provision", body)[0] == 400
        assert server.registry.list_allocations() == []
    finally:
        server.shutdown()
        server.server_close()
