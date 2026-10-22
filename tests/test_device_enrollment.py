"""Synthetic fresh-device enrollment tests; no production files or VPS changes."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from urllib.error import HTTPError

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from kt_mcp_client import enrollment as client_enrollment
from kt_mcp_client import provisioning
from kt_mcp_client.config import DeviceProfile, load, save
from helpers import set_test_endpoint
from server.app import AppSecurity, create_app
from server.enrollment import (EnrollmentService, EnrollmentSettings, EnrollmentDenied,
                               EnrollmentConflict, authorized_key, issue_key, revoke_key,
                               canonical_public_key, token_hash)
from server.registry import AllocationRegistry, RegistrySettings, RegistryError
from server.service import ProvisioningHTTPServer

ORIGIN = "https://device.example.invalid"
ENDPOINT = ORIGIN + "/mcp/" + "Q" * 43


def pub():
    return Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode("ascii")


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.delenv("KT_MCP_DEVICE_ENROLLMENT_ENABLED", raising=False)
    monkeypatch.setenv("KT_MCP_PROVISION_URL", ORIGIN + "/provision")
    registry = AllocationRegistry(RegistrySettings(tmp_path / "db.sqlite3", tmp_path / "routes.map", ORIGIN), reload=lambda: None)
    service = EnrollmentService(registry, EnrollmentSettings("ssh.example.invalid", "kt-mcp-devices", pub(), 2222))
    return registry, service


def test_one_key_one_device_and_idempotent_retry(backend):
    registry, service = backend
    key, _ = issue_key(registry.settings.db_path, "customer-one")
    public = pub()
    first = service.provision(key, "1" * 32, ORIGIN, ENDPOINT, public)
    assert service.provision(key, "1" * 32, ORIGIN, ENDPOINT, public) == first
    with pytest.raises(EnrollmentConflict):
        service.provision(key, "2" * 32, ORIGIN, ORIGIN + "/mcp/" + "R" * 43, pub())
    assert len(registry.list_allocations()) == 1
    assert first["ssh"]["client_key_fingerprint"] == client_enrollment.fingerprint(public)
    assert "private" not in json.dumps(first)


def test_two_customers_get_separate_public_keys_and_ports(backend):
    registry, service = backend
    outputs = []
    for index, letter in [(1, "A"), (2, "B")]:
        key, _ = issue_key(registry.settings.db_path, f"customer-{index}")
        outputs.append(service.provision(key, str(index) * 32, ORIGIN, ORIGIN + "/mcp/" + letter * 43, pub()))
    assert outputs[0]["remote_port"] != outputs[1]["remote_port"]
    assert outputs[0]["ssh"]["client_key_fingerprint"] != outputs[1]["ssh"]["client_key_fingerprint"]


def test_concurrent_key_redemption_cannot_open_two_devices(backend):
    registry, service = backend
    key, _ = issue_key(registry.settings.db_path, "one-seat")
    barrier = threading.Barrier(2)
    def enroll(index):
        barrier.wait()
        try:
            service.provision(key, str(index) * 32, ORIGIN, ORIGIN + "/mcp/" + str(index) * 43, pub())
            return "ok"
        except EnrollmentConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(enroll, (1, 2)))
    assert sorted(result) == ["conflict", "ok"]
    assert len(registry.list_allocations()) == 1


def test_proxy_reload_failure_does_not_consume_activation(backend):
    registry, service = backend
    key, digest = issue_key(registry.settings.db_path, "rollback")
    public = pub()
    registry._reload_override = lambda: (_ for _ in ()).throw(RuntimeError("synthetic failure"))
    with pytest.raises(RegistryError):
        service.provision(key, "1" * 32, ORIGIN, ENDPOINT, public)
    with sqlite3.connect(registry.settings.db_path) as db:
        assert db.execute("SELECT installation_id FROM device_activation_keys WHERE token_hash=?", (digest,)).fetchone()[0] is None
    assert registry.list_allocations() == []
    assert authorized_key(registry.settings.db_path, "kt-mcp-devices", "kt-mcp-devices", public) == ""
    registry._reload_override = lambda: None
    assert service.provision(key, "1" * 32, ORIGIN, ENDPOINT, public)["remote_port"] == 18766


def test_activation_expiry_revocation_and_restricted_authorization(backend):
    registry, service = backend
    key, digest = issue_key(registry.settings.db_path, "customer")
    public = pub()
    result = service.provision(key, "1" * 32, ORIGIN, ENDPOINT, public)
    line = authorized_key(registry.settings.db_path, "kt-mcp-devices", "kt-mcp-devices", public)
    assert line == f'restrict,port-forwarding,permitlisten="127.0.0.1:{result["remote_port"]}" {public}'
    assert authorized_key(registry.settings.db_path, "root", "kt-mcp-devices", public) == ""
    assert authorized_key(registry.settings.db_path, "kt-mcp-devices", "kt-mcp-devices", pub()) == ""
    assert revoke_key(registry.settings.db_path, digest)
    assert authorized_key(registry.settings.db_path, "kt-mcp-devices", "kt-mcp-devices", public) == ""
    with pytest.raises(EnrollmentDenied):
        service.provision(key, "1" * 32, ORIGIN, ENDPOINT, public)
    key2, digest2 = issue_key(registry.settings.db_path, "expired")
    with sqlite3.connect(registry.settings.db_path) as db:
        db.execute("UPDATE device_activation_keys SET expires_at=0 WHERE token_hash=?", (digest2,))
    with pytest.raises(EnrollmentDenied):
        service.check(key2)
    with sqlite3.connect(registry.settings.db_path) as db:
        assert key not in str(db.execute("SELECT * FROM device_activation_keys").fetchall())


@pytest.mark.parametrize("invalid", ["", "ssh-ed25519 AAAA", 'command="sh" ssh-ed25519 AAAA', "ssh-rsa AAAA", "ssh-ed25519 AAAA\nextra"])
def test_key_parser_rejects_options_other_types_and_malformed_blobs(invalid):
    with pytest.raises(ValueError):
        canonical_public_key(invalid)
    with pytest.raises(ValueError):
        client_enrollment.public_key(invalid)


def test_shared_key_can_enroll_but_customer_key_cannot_admin_provision(backend):
    registry, service = backend
    key, _ = issue_key(registry.settings.db_path, "customer")
    client = TestClient(create_app(registry, security=AppSecurity("test-admin"), enrollment=service))
    legacy = {"origin": ORIGIN, "installation_id": "1" * 32, "endpoint_url": ENDPOINT}
    payload = {**legacy, "enrollment_version": 2, "ssh_public_key": pub()}
    assert client.post("/provision", json=payload, headers={"X-KT-Provision-Token": "test-admin"}).status_code == 200
    assert client.post("/provision", json=legacy, headers={"X-KT-Provision-Token": key}).status_code == 401
    payload.update(installation_id="2" * 32, endpoint_url=ORIGIN + "/mcp/" + "R" * 43, ssh_public_key=pub())
    assert client.post("/provision", json=payload, headers={"X-KT-Provision-Token": key}).status_code == 200


def test_disabled_server_does_not_pretend_to_enroll(backend):
    registry, _ = backend
    client = TestClient(create_app(registry, security=AppSecurity("test-admin")))
    assert client.post("/provision", json={"origin": ORIGIN, "installation_id": "1" * 32,
        "endpoint_url": ENDPOINT, "enrollment_version": 2, "ssh_public_key": pub()}).status_code == 503


class Reply:
    status = 200
    def __init__(self, payload):
        self.payload = payload
    def read(self, size=-1):
        return json.dumps(self.payload).encode("utf-8")
    def close(self):
        pass


def device_profile(tmp_path, key):
    root = tmp_path / "fresh-device"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    profile = DeviceProfile(public_origin=ORIGIN, provision_token=key, kt_home=str(root / "kt-home"),
                            workspace_path=str(workspace), tools_config=str(root / "tools.yaml"))
    set_test_endpoint(profile, ENDPOINT)
    return root, profile


def test_fresh_client_only_origin_key_generates_and_saves_ssh(backend, tmp_path):
    registry, service = backend
    key, _ = issue_key(registry.settings.db_path, "fresh-device")
    root, profile = device_profile(tmp_path, key)
    sent = []
    def open_url(request, timeout):
        body = json.loads(request.data)
        sent.append(body)
        assert set(body) == {"origin", "installation_id", "endpoint_url", "enrollment_version", "ssh_public_key"}
        assert b"PRIVATE KEY" not in request.data and key.encode() not in request.data
        return Reply(service.provision(request.get_header("X-kt-provision-token"), body["installation_id"],
                                      body["origin"], body["endpoint_url"], body["ssh_public_key"]))
    assert provisioning.provision_and_save(ORIGIN, ENDPOINT, root, profile=profile, opener=open_url) == ENDPOINT
    saved = load(root)
    assert saved.ssh_host == "ssh.example.invalid" and saved.ssh_port == 2222
    assert saved.ssh_user == "kt-mcp-devices"
    assert Path(saved.ssh_identity_file).is_file()
    assert "[ssh.example.invalid]:2222 ssh-ed25519 " in Path(saved.ssh_known_hosts).read_text()
    assert key not in (root / "profile.json").read_text()
    private_before = Path(saved.ssh_identity_file).read_bytes()
    provisioning.provision_and_save(ORIGIN, ENDPOINT, root, profile=profile, opener=open_url)
    assert private_before == Path(saved.ssh_identity_file).read_bytes()
    assert sent[0] == sent[1]


def test_response_host_key_change_is_rejected_without_profile_mutation(backend, tmp_path):
    registry, service = backend
    key, _ = issue_key(registry.settings.db_path, "pin-test")
    root, profile = device_profile(tmp_path, key)
    def open_url(request, timeout):
        b = json.loads(request.data)
        return Reply(service.provision(key, b["installation_id"], b["origin"], b["endpoint_url"], b["ssh_public_key"]))
    provisioning.provision_and_save(ORIGIN, ENDPOINT, root, profile=profile, opener=open_url)
    before = (root / "profile.json").read_bytes()
    service.settings = replace(service.settings, ssh_host_key=pub())
    with pytest.raises(provisioning.ProvisioningError, match="主机密钥"):
        provisioning.provision_and_save(ORIGIN, ENDPOINT, root, profile=profile, opener=open_url)
    assert (root / "profile.json").read_bytes() == before


def test_old_server_never_causes_private_key_upload_or_downgrade(backend, tmp_path):
    registry, _ = backend
    key, _ = issue_key(registry.settings.db_path, "old-api")
    root, profile = device_profile(tmp_path, key)
    calls = []
    def reject(request, timeout):
        calls.append(request)
        raise HTTPError(request.full_url, 422, "old server", {}, None)
    with pytest.raises(provisioning.ProvisioningError, match="旧版"):
        provisioning.provision_and_save(ORIGIN, ENDPOINT, root, profile=profile, opener=reject)
    assert len(calls) == 1
    assert not profile.ssh_host
    assert not (root / "profile.json").exists()
    assert (root / "device-ssh/id_ed25519").exists()


def test_endpoint_proof_required_before_device_key_generation(backend, tmp_path):
    registry, _ = backend
    key, _ = issue_key(registry.settings.db_path, "proof")
    root, profile = device_profile(tmp_path, key)
    profile.endpoint_verified = False
    with pytest.raises(provisioning.ProvisioningError):
        provisioning.provision_and_save(ORIGIN, ENDPOINT, root, profile=profile,
            opener=lambda *a, **k: pytest.fail("no network allowed"))
    assert not (root / "device-ssh").exists()


def test_stdlib_server_supports_the_same_enrollment_contract(backend):
    import http.client
    registry, service = backend
    key, _ = issue_key(registry.settings.db_path, "stdlib")
    server = ProvisioningHTTPServer(("127.0.0.1", 0), registry, provision_token="test-admin", enrollment=service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        body = json.dumps({"origin": ORIGIN, "installation_id": "1" * 32, "endpoint_url": ENDPOINT,
                           "enrollment_version": 2, "ssh_public_key": pub()})
        connection.request("POST", "/provision", body, {"Content-Type": "application/json", "X-KT-Provision-Token": key})
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["ssh"]["user"] == "kt-mcp-devices"
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
