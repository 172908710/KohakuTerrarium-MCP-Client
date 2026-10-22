"""Shared verification key enables independent device identities, not a shared SSH key."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import sqlite3
import threading
from urllib.error import HTTPError

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from kt_mcp_client import provisioning
from kt_mcp_client import enrollment as client_enrollment
from kt_mcp_client.config import DeviceProfile, load
from tests.helpers import set_test_endpoint
from server.app import AppSecurity, create_app
from server.enrollment import (EnrollmentService, EnrollmentSettings, EnrollmentConflict,
    EnrollmentDenied, authorized_key, issue_key, revoke_key, revoke_shared_device)
from server.enrollment_cli import main as activation_cli
from server.registry import AllocationRegistry, RegistrySettings, RegistryError
from server.service import ProvisioningHTTPServer

ORIGIN = "https://shared.example.invalid"
SHARED = "synthetic-shared-verification-key"
USER = "kt-mcp-devices"


def public_key():
    return Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode("ascii")


def endpoint(index):
    return ORIGIN + "/mcp/" + str(index) * 43


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.delenv("KT_MCP_DEVICE_ENROLLMENT_ENABLED", raising=False)
    monkeypatch.delenv("KT_MCP_PROVISION_TOKEN", raising=False)
    monkeypatch.setenv("KT_MCP_PROVISION_URL", ORIGIN + "/provision")
    registry = AllocationRegistry(RegistrySettings(tmp_path / "registry.sqlite3", tmp_path / "routes.map", ORIGIN), reload=lambda: None)
    service = EnrollmentService(registry, EnrollmentSettings("ssh.example.invalid", USER, public_key(), 2223))
    return registry, service


def enroll(service, index=1, public=None, token=SHARED):
    return service.provision_shared(token, str(index) * 32, ORIGIN, endpoint(index),
        public or public_key(), expected_token=SHARED)


def test_one_shared_key_enrolls_two_distinct_devices_and_retries(backend):
    registry, service = backend
    public1, public2 = public_key(), public_key()
    one, two = enroll(service, 1, public1), enroll(service, 2, public2)
    assert one["remote_port"] != two["remote_port"]
    assert one["ssh"]["client_key_fingerprint"] != two["ssh"]["client_key_fingerprint"]
    assert one == enroll(service, 1, public1)
    assert len(registry.list_allocations()) == 2
    with sqlite3.connect(registry.settings.db_path) as db:
        rows = db.execute("SELECT * FROM shared_key_device_bindings").fetchall()
    assert len(rows) == 2 and SHARED not in str(rows)
    assert 'permitlisten="127.0.0.1:18766"' in authorized_key(registry.settings.db_path, USER, USER, public1)
    assert 'permitlisten="127.0.0.1:18767"' in authorized_key(registry.settings.db_path, USER, USER, public2)


@pytest.mark.parametrize("token", ["", "wrong", SHARED + "x", "ktd_" + "A" * 43])
def test_shared_enrollment_never_accepts_arbitrary_or_missing_key(backend, token):
    registry, service = backend
    with pytest.raises(EnrollmentDenied):
        enroll(service, token=token)
    assert not registry.list_allocations()


def test_missing_expected_key_denied_before_database_write(backend):
    registry, service = backend
    with pytest.raises(EnrollmentDenied):
        service.provision_shared("", "1" * 32, ORIGIN, endpoint(1), public_key(), expected_token="")
    assert not registry.list_allocations()


def test_public_key_cannot_be_shared_or_silently_replaced(backend):
    registry, service = backend
    public = public_key()
    enroll(service, 1, public)
    with pytest.raises(EnrollmentConflict):
        enroll(service, 2, public)
    with pytest.raises(EnrollmentConflict):
        enroll(service, 1, public_key())
    assert len(registry.list_allocations()) == 1


def test_shared_devices_are_atomic_on_route_reload_failure(backend):
    registry, service = backend
    public = public_key()
    registry._reload_override = lambda: (_ for _ in ()).throw(RuntimeError("synthetic reload failure"))
    with pytest.raises(RegistryError):
        enroll(service, 1, public)
    assert registry.list_allocations() == []
    with sqlite3.connect(registry.settings.db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM shared_key_device_bindings").fetchone()[0] == 0
    assert authorized_key(registry.settings.db_path, USER, USER, public) == ""
    registry._reload_override = lambda: None
    assert enroll(service, 1, public)["remote_port"] == 18766


def test_existing_route_only_failed_install_can_be_completed(backend):
    registry, service = backend
    previous = registry.provision("1" * 32, ORIGIN, endpoint(1))
    result = enroll(service, 1)
    assert result["remote_port"] == previous.remote_port
    assert len(registry.list_allocations()) == 1


def test_device_revocation_cannot_be_undone_by_shared_key_retry(backend):
    registry, service = backend
    public = public_key()
    enroll(service, 1, public)
    assert revoke_shared_device(registry.settings.db_path, "1" * 32)
    assert authorized_key(registry.settings.db_path, USER, USER, public) == ""
    with pytest.raises(EnrollmentDenied):
        enroll(service, 1, public)
    assert enroll(service, 2)["remote_port"] == 18767


def test_customer_key_binding_cannot_be_bypassed_by_shared_key(backend):
    registry, service = backend
    key, digest = issue_key(registry.settings.db_path, "single-device")
    public = public_key()
    service.provision(key, "1" * 32, ORIGIN, endpoint(1), public)
    revoke_key(registry.settings.db_path, digest)
    with pytest.raises(EnrollmentConflict):
        enroll(service, 1, public)
    with pytest.raises(EnrollmentConflict):
        enroll(service, 2, public)
    assert authorized_key(registry.settings.db_path, USER, USER, public) == ""


def test_shared_binding_cannot_be_bypassed_by_customer_key(backend):
    registry, service = backend
    public = public_key()
    enroll(service, 1, public)
    revoke_shared_device(registry.settings.db_path, "1" * 32)
    key, _ = issue_key(registry.settings.db_path, "cannot-replace")
    with pytest.raises(EnrollmentConflict):
        service.provision(key, "1" * 32, ORIGIN, endpoint(1), public)
    with pytest.raises(EnrollmentConflict):
        service.provision(key, "2" * 32, ORIGIN, endpoint(2), public)


def test_two_concurrent_shared_enrollments_receive_distinct_ports(backend):
    registry, service = backend
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda index: enroll(service, index), (1, 2)))
    assert len({row["remote_port"] for row in results}) == 2
    assert len(registry.list_allocations()) == 2


def test_disabled_allocation_and_nondevice_user_do_not_authenticate(backend):
    registry, service = backend
    public = public_key()
    enroll(service, 1, public)
    assert authorized_key(registry.settings.db_path, "root", USER, public) == ""
    with sqlite3.connect(registry.settings.db_path) as db:
        db.execute("UPDATE devices SET enabled=0 WHERE installation_id=?", ("1" * 32,))
    assert authorized_key(registry.settings.db_path, USER, USER, public) == ""


class Reply:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status
    def read(self, size=-1):
        return json.dumps(self.payload).encode()
    def close(self):
        pass


def fresh_profile(tmp_path, index):
    root = tmp_path / ("fresh-" + str(index))
    (root / "workspace").mkdir(parents=True)
    profile = DeviceProfile(public_origin=ORIGIN, provision_token=SHARED, kt_home=str(root / "kt-home"), workspace_path=str(root / "workspace"))
    set_test_endpoint(profile, endpoint(index))
    return root, profile


def test_fresh_client_shared_key_saves_ssh_without_disclosing_private_key(backend, tmp_path):
    registry, service = backend
    api = TestClient(create_app(registry, security=AppSecurity(SHARED), enrollment=service))
    requests = []
    def opener(req, timeout):
        data = json.loads(req.data)
        assert set(data) == {"origin", "installation_id", "endpoint_url", "enrollment_version", "ssh_public_key"}
        assert b"PRIVATE KEY" not in req.data and SHARED.encode() not in req.data
        requests.append(data)
        response = api.post("/provision", json=data, headers={"X-KT-Provision-Token": req.get_header("X-kt-provision-token")})
        return Reply(response.json(), response.status_code)
    private_files = []
    for index in (1, 2):
        root, profile = fresh_profile(tmp_path, index)
        assert provisioning.provision_and_save(ORIGIN, endpoint(index), root, profile=profile, opener=opener) == endpoint(index)
        saved = load(root)
        assert saved.ssh_host == "ssh.example.invalid" and saved.ssh_user == USER and saved.ssh_port == 2223
        private = Path(saved.ssh_identity_file)
        before = private.read_bytes()
        private_files.append(before)
        assert SHARED not in (root / "profile.json").read_text(encoding="utf-8")
        # Protocol must remain v2 even though the saved SSH fields are complete.
        assert client_enrollment.needs_automatic_enrollment(saved, root)
        provisioning.provision_and_save(ORIGIN, endpoint(index), root, profile=profile, opener=opener)
        assert private.read_bytes() == before
        assert requests[-1] == requests[-2]
    assert private_files[0] != private_files[1]
    assert len(registry.list_allocations()) == 2


def test_preconfigured_legacy_ssh_profile_is_not_replaced(backend, tmp_path):
    root, profile = fresh_profile(tmp_path, 1)
    private, known = root / "existing-private", root / "known_hosts"
    private.write_text("synthetic-key")
    known.write_text("synthetic-pinned-host")
    profile.ssh_host, profile.ssh_user = "legacy.example.invalid", "kt-mcp-device-01"
    profile.ssh_identity_file, profile.ssh_known_hosts = str(private), str(known)
    def opener(req, timeout):
        assert set(json.loads(req.data)) == {"origin", "installation_id", "endpoint_url"}
        return Reply({"url": endpoint(1), "remote_port": 18766})
    provisioning.provision_and_save(ORIGIN, endpoint(1), root, profile=profile, opener=opener)
    assert profile.ssh_identity_file == str(private)
    assert private.read_text() == "synthetic-key"
    assert not (root / "device-ssh").exists()


@pytest.mark.parametrize("status", [400, 401, 422, 503])
def test_outdated_server_rejects_without_legacy_downgrade(backend, tmp_path, status):
    root, profile = fresh_profile(tmp_path, 1)
    sent = []
    def opener(req, timeout):
        sent.append(req)
        raise HTTPError(req.full_url, status, "synthetic failure", {}, None)
    with pytest.raises(provisioning.ProvisioningError):
        provisioning.provision_and_save(ORIGIN, endpoint(1), root, profile=profile, opener=opener)
    assert len(sent) == 1 and json.loads(sent[0].data)["enrollment_version"] == 2
    assert not profile.ssh_host
    assert (root / "device-ssh/id_ed25519").is_file()


@pytest.mark.parametrize("transport", ["asgi", "stdlib"])
def test_http_parity_for_shared_auth_legacy_and_retries(backend, transport):
    registry, service = backend
    server = thread = None
    if transport == "asgi":
        client = TestClient(create_app(registry, security=AppSecurity(SHARED), enrollment=service))
        def request(payload, token):
            result = client.post("/provision", json=payload, headers={"X-KT-Provision-Token": token})
            return result.status_code, result.json()
    else:
        server = ProvisioningHTTPServer(("127.0.0.1", 0), registry, provision_token=SHARED, enrollment=service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(payload, token):
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            try:
                connection.request("POST", "/provision", json.dumps(payload), {"Content-Type": "application/json", "X-KT-Provision-Token": token})
                result = connection.getresponse()
                return result.status, json.loads(result.read())
            finally:
                connection.close()
    payload = {"origin": ORIGIN, "installation_id": "1" * 32, "endpoint_url": endpoint(1), "enrollment_version": 2, "ssh_public_key": public_key()}
    try:
        assert request(payload, "wrong-key")[0] == 401
        status, first = request(payload, SHARED)
        assert status == 200 and first["ssh"]["port"] == 2223
        assert request(payload, SHARED) == (status, first)
        legacy = {"origin": ORIGIN, "installation_id": "2" * 32, "endpoint_url": endpoint(2)}
        assert request(legacy, SHARED)[0] == 200
        key, _ = issue_key(registry.settings.db_path, "separate-key")
        assert request(legacy, key)[0] == 401
    finally:
        if server:
            server.shutdown(); server.server_close(); thread.join(3)


def test_shared_devices_cli_can_revoke_one_without_revealing_key(backend, capsys):
    registry, service = backend
    enroll(service, 1)
    db = str(registry.settings.db_path)
    assert activation_cli(["--db", db, "shared-devices"]) == 0
    text = capsys.readouterr().out
    assert SHARED not in text
    assert json.loads(text)[0]["installation_id"] == "1" * 32
    assert activation_cli(["--db", db, "revoke-device", "--installation-id", "1" * 32]) == 0
    assert json.loads(capsys.readouterr().out)["existing_sessions_terminated"] is False
