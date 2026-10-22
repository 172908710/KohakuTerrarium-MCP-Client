"""Shared verification key or single-device activation -> independent SSH identity.

The HTTP service never receives/generates a client's private key. OpenSSH's
AuthorizedKeysCommand reads only committed, enabled bindings from this DB.
No root helper or sshd reload is needed for each customer enrollment.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import struct
import time
from typing import Any

from .registry import Allocation, AllocationRegistry

TOKEN_PATTERN = re.compile(r"^ktd_[A-Za-z0-9_-]{43}$")
USER_PATTERN = re.compile(r"^kt-mcp-device[a-z0-9_-]{0,19}$")


class EnrollmentDenied(ValueError):
    pass


class EnrollmentConflict(ValueError):
    pass


def canonical_public_key(value: object) -> str:
    if not isinstance(value, str) or len(value) > 256 or "\n" in value or "\r" in value:
        raise ValueError("invalid device public key")
    fields = value.split()
    if len(fields) != 2 or fields[0] != "ssh-ed25519":
        raise ValueError("only plain Ed25519 public keys are accepted")
    try:
        raw = base64.b64decode(fields[1], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid device public key") from exc
    prefix = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)
    if len(raw) != len(prefix) + 32 or not raw.startswith(prefix):
        raise ValueError("invalid device public key")
    return "ssh-ed25519 " + base64.b64encode(raw).decode("ascii")


def key_fingerprint(public: str) -> str:
    raw = base64.b64decode(canonical_public_key(public).split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")


def token_hash(token: str) -> str:
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise EnrollmentDenied("invalid, expired or revoked device activation key")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS device_activation_keys (
        token_hash TEXT PRIMARY KEY,
        label TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
        installation_id TEXT UNIQUE REFERENCES devices(installation_id),
        public_key TEXT UNIQUE,
        CHECK ((installation_id IS NULL) = (public_key IS NULL))
    )""")
    # Additive migration: existing single-device keys and routes are untouched.
    # A shared key is NOT a device identity and is never stored in this table.
    connection.execute("""CREATE TABLE IF NOT EXISTS shared_key_device_bindings (
        installation_id TEXT PRIMARY KEY REFERENCES devices(installation_id),
        public_key TEXT NOT NULL UNIQUE,
        revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
        created_at INTEGER NOT NULL
    )""")


@dataclass(frozen=True)
class EnrollmentSettings:
    ssh_host: str
    ssh_user: str
    ssh_host_key: str
    ssh_port: int = 22

    def __post_init__(self):
        if not isinstance(self.ssh_host, str) or len(self.ssh_host) > 253 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", self.ssh_host):
            raise ValueError("invalid device SSH host")
        if not USER_PATTERN.fullmatch(self.ssh_user):
            raise ValueError("device enrollment requires a dedicated kt-mcp-device account")
        if isinstance(self.ssh_port, bool) or not 1 <= self.ssh_port <= 65535:
            raise ValueError("invalid device SSH port")
        object.__setattr__(self, "ssh_host_key", canonical_public_key(self.ssh_host_key))

    @classmethod
    def from_env(cls) -> EnrollmentSettings | None:
        # Operator enables this only after installing/testing the restricted
        # AuthorizedKeysCommand and public host key. No opportunistic ssh-keyscan.
        if os.environ.get("KT_MCP_DEVICE_ENROLLMENT_ENABLED", "0") != "1":
            return None
        return cls(
            ssh_host=os.environ.get("KT_MCP_DEVICE_SSH_HOST", ""),
            ssh_user=os.environ.get("KT_MCP_DEVICE_SSH_USER", ""),
            ssh_host_key=os.environ.get("KT_MCP_DEVICE_SSH_HOST_KEY", ""),
            ssh_port=int(os.environ.get("KT_MCP_DEVICE_SSH_PORT", "22")),
        )


class EnrollmentService:
    def __init__(self, registry: AllocationRegistry, settings: EnrollmentSettings):
        self.registry = registry
        self.settings = settings
        with registry._connect() as connection:
            initialize_schema(connection)

    def check(self, token: str) -> None:
        digest = token_hash(token)
        with self.registry._connect() as connection:
            row = connection.execute("SELECT * FROM device_activation_keys WHERE token_hash=?", (digest,)).fetchone()
        self._check_row(row)

    @staticmethod
    def _check_row(row):
        if row is None or row["revoked"] or (row["installation_id"] is None and row["expires_at"] <= time.time()):
            raise EnrollmentDenied("invalid, expired or revoked device activation key")

    def provision(self, token: str, installation: str, origin: str, endpoint: str, public: str) -> dict[str, Any]:
        digest = token_hash(token)
        public = canonical_public_key(public)
        self.check(token)

        def bind(connection: sqlite3.Connection, allocation: Allocation) -> None:
            # Recheck under the SAME IMMEDIATE transaction that allocates the
            # port and publishes the route. Concurrent use cannot bind two PCs.
            row = connection.execute("SELECT * FROM device_activation_keys WHERE token_hash=?", (digest,)).fetchone()
            self._check_row(row)
            if connection.execute(
                "SELECT 1 FROM shared_key_device_bindings WHERE installation_id=? OR public_key=?",
                (installation, public),
            ).fetchone():
                raise EnrollmentConflict("device or public key already bound through shared enrollment")
            if row["installation_id"] is not None:
                if row["installation_id"] != installation or row["public_key"] != public:
                    raise EnrollmentConflict("activation key already belongs to another device")
                return
            try:
                connection.execute("UPDATE device_activation_keys SET installation_id=?, public_key=? WHERE token_hash=?",
                                   (installation, public, digest))
            except sqlite3.IntegrityError as exc:
                raise EnrollmentConflict("device or public key already enrolled") from exc

        allocation = self.registry.provision(installation, origin, endpoint, enrollment_gate=bind)
        return self._response(allocation, public)

    def provision_shared(self, token: str, installation: str, origin: str,
                         endpoint: str, public: str, *, expected_token: str) -> dict[str, Any]:
        """Authenticate the existing verification key, then bind this device only.

        Never treat an arbitrary non-ktd string as authorization. Re-check the
        configured secret here even when an HTTP caller already authenticated it.
        Rotating the shared verification key does not rotate/revoke device keys.
        """
        if (not isinstance(token, str) or not isinstance(expected_token, str)
            or not token or not expected_token or token.startswith("ktd_")
            or len(token) > 4096 or len(expected_token) > 4096
            or not hmac.compare_digest(token.encode("utf-8"), expected_token.encode("utf-8"))):
            raise EnrollmentDenied("invalid shared verification key")
        public = canonical_public_key(public)

        def bind(connection: sqlite3.Connection, allocation: Allocation) -> None:
            # Cross-mode conflicts include revoked records: an admin-shared key
            # must not silently undo a customer-key revocation or take its port.
            if connection.execute(
                "SELECT 1 FROM device_activation_keys WHERE installation_id=? OR public_key=?",
                (installation, public),
            ).fetchone():
                raise EnrollmentConflict("device or public key already bound to an activation key")
            row = connection.execute(
                "SELECT public_key,revoked FROM shared_key_device_bindings WHERE installation_id=?",
                (installation,),
            ).fetchone()
            if row is not None:
                if row["revoked"]:
                    raise EnrollmentDenied("device authorization has been revoked")
                if row["public_key"] != public:
                    raise EnrollmentConflict("device identity changed; existing public key was not replaced")
                return
            try:
                connection.execute(
                    "INSERT INTO shared_key_device_bindings(installation_id,public_key,created_at) VALUES(?,?,?)",
                    (installation, public, int(time.time())),
                )
            except sqlite3.IntegrityError as exc:
                raise EnrollmentConflict("device public key is already bound") from exc

        allocation = self.registry.provision(installation, origin, endpoint, enrollment_gate=bind)
        return self._response(allocation, public)

    def _response(self, allocation: Allocation, public: str) -> dict[str, Any]:
        return {
            "url": self.registry.public_url(allocation), "remote_port": allocation.remote_port,
            "enrollment_version": 2,
            "ssh": {"host": self.settings.ssh_host, "port": self.settings.ssh_port,
                    "user": self.settings.ssh_user, "host_key": self.settings.ssh_host_key,
                    "client_key_fingerprint": key_fingerprint(public), "remote_bind": "127.0.0.1"},
        }


def issue_key(db_path: Path, label: str, *, valid_hours: int = 168) -> tuple[str, str]:
    """Local operator CLI only. Store a hash; reveal the random activation key once."""
    if not label.strip() or len(label) > 128 or not 1 <= valid_hours <= 8760:
        raise ValueError("a label and an activation lifetime of 1-8760 hours are required")
    if not Path(db_path).is_file():
        raise ValueError("existing provisioning registry is required")
    token = "ktd_" + secrets.token_urlsafe(32)
    digest = token_hash(token)
    with sqlite3.connect(db_path, timeout=10) as connection:
        initialize_schema(connection)
        connection.execute("INSERT INTO device_activation_keys(token_hash,label,created_at,expires_at) VALUES(?,?,?,?)",
                           (digest, label, int(time.time()), int(time.time()) + valid_hours * 3600))
    return token, digest


def revoke_key(db_path: Path, digest: str) -> bool:
    """Stop new enrollments and SSH authentications. Existing sessions need termination by the operator."""
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("expected activation ID (SHA256), not plaintext key")
    with sqlite3.connect(db_path, timeout=10) as connection:
        result = connection.execute("UPDATE device_activation_keys SET revoked=1 WHERE token_hash=?", (digest,))
    return result.rowcount == 1


def revoke_shared_device(db_path: Path, installation_id: str) -> bool:
    """Block new authentication/re-enrollment for this device, not all customers.

    Existing SSH sessions are not terminated by a database update alone.
    """
    if not re.fullmatch(r"[0-9a-f]{32}", installation_id):
        raise ValueError("expected a device installation ID")
    with sqlite3.connect(db_path, timeout=10) as connection:
        result = connection.execute(
            "UPDATE shared_key_device_bindings SET revoked=1 WHERE installation_id=?",
            (installation_id,),
        )
    return result.rowcount == 1


def authorized_key(db_path: Path, requested_user: str, configured_user: str, public: str,
                   *, port_start: int = 18766, port_end: int = 18865) -> str:
    """Read-only sshd helper. No registration, DB creation, reload or shell calls."""
    if requested_user != configured_user or not USER_PATTERN.fullmatch(configured_user):
        return ""
    public = canonical_public_key(public)
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        rows = connection.execute("""SELECT d.remote_port FROM device_activation_keys a
            JOIN devices d ON d.installation_id=a.installation_id
            WHERE a.public_key=? AND a.revoked=0 AND d.enabled=1""", (public,)).fetchall()
        # Keep the helper compatible during an additive schema upgrade. Never
        # create tables from sshd or grant access if storage is inconsistent.
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shared_key_device_bindings'"
        ).fetchone():
            rows += connection.execute("""SELECT d.remote_port FROM shared_key_device_bindings a
                JOIN devices d ON d.installation_id=a.installation_id
                WHERE a.public_key=? AND a.revoked=0 AND d.enabled=1""", (public,)).fetchall()
        row = rows[0] if len(rows) == 1 else None
    finally:
        connection.close()
    if row is None:
        return ""
    port = row[0]
    if not isinstance(port, int) or port == 18765 or not 1 <= port_start <= port <= port_end <= 65535:
        return ""
    # restrict disables PTY/agent/X11/rc by default; selectively permit only
    # remote TCP forwarding, with the exact loopback listen endpoint. sshd's
    # dedicated Match block additionally denies sessions and local forwarding.
    return f'restrict,port-forwarding,permitlisten="127.0.0.1:{port}" {public}'
