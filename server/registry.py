"""Persistent allocation registry for the independent MCP gateway.

This module deliberately owns only the new-device registry.  It never reads the
production KT-MCP-Bridge files and it never changes the existing production
allocation (127.0.0.1:18765).
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit


INSTALLATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
SECRET_HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
PRODUCTION_REMOTE_PORT = 18765
DEFAULT_PORT_START = 18766
DEFAULT_PORT_END = 18865


class RegistryError(RuntimeError):
    """Raised when an allocation cannot be persisted or synchronized."""


class InvalidInstallationID(ValueError):
    """Raised for a client identifier outside the documented format."""


class OriginMismatch(ValueError):
    """Raised when a client asks for an origin not owned by this gateway."""


class EndpointConflict(ValueError):
    """Raised when a device tries to change an already registered secret."""


def parse_endpoint_url(value: str) -> tuple[str, str]:
    """Return the HTTPS origin and bearer secret from one complete MCP URL."""

    raw = str(value or "")
    text = raw.strip()
    if not text or raw != text or any(character.isspace() for character in text):
        raise ValueError("endpoint_url must be a valid HTTPS MCP URL")
    try:
        parsed = urlsplit(text)
        _ = parsed.port
        hostname = parsed.hostname
    except (TypeError, ValueError) as exc:
        raise ValueError("endpoint_url must be a valid HTTPS MCP URL") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or not hostname
        or "\\" in parsed.netloc
        or parsed.netloc.endswith(":")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/mcp/")
        or parsed.path.count("/") != 2
    ):
        raise ValueError("endpoint_url must be an HTTPS /mcp/<secret> URL")
    secret = parsed.path.removeprefix("/mcp/")
    if not SECRET_RE.fullmatch(secret):
        raise ValueError("endpoint_url contains an invalid MCP secret")
    return f"https://{parsed.netloc.lower()}", secret


def normalize_reserved_secret_hashes(value: object) -> frozenset[str]:
    """Validate and canonicalize configured SHA-256 secret digests.

    Only digests are accepted here.  The production endpoint secret itself is
    never represented in configuration or error messages.
    """

    if isinstance(value, str):
        if not value.strip():
            return frozenset()
        values = value.split(",")
    else:
        values = tuple(value or ())
    normalized: set[str] = set()
    for item in values:
        digest = str(item).strip().lower()
        if not digest:
            continue
        if not SECRET_HASH_RE.fullmatch(digest):
            raise ValueError("reserved_secret_hashes must contain SHA-256 hex hashes")
        normalized.add(digest)
    return frozenset(normalized)


class CapacityExhausted(RegistryError):
    """Raised when no independent reverse-forward port remains."""


@dataclass(frozen=True)
class RegistrySettings:
    db_path: Path
    routes_path: Path
    public_origin: str
    port_start: int = DEFAULT_PORT_START
    port_end: int = DEFAULT_PORT_END
    reload_helper: Path | None = None
    reserved_secret_hashes: frozenset[str] = field(default_factory=frozenset, repr=False)

    def __post_init__(self) -> None:
        db_path = Path(self.db_path).expanduser()
        routes_path = Path(self.routes_path).expanduser()
        object.__setattr__(self, "db_path", db_path)
        object.__setattr__(self, "routes_path", routes_path)
        object.__setattr__(self, "public_origin", normalize_origin(self.public_origin))
        try:
            resolved_db = db_path.resolve()
            resolved_routes = routes_path.resolve()
        except OSError as exc:
            raise ValueError("db_path and routes_path must be resolvable paths") from exc
        if resolved_db.parent != resolved_routes.parent:
            raise ValueError("routes_path must be in the same directory as db_path")
        if resolved_db == resolved_routes:
            raise ValueError("routes_path must not overwrite db_path")
        object.__setattr__(
            self,
            "reserved_secret_hashes",
            normalize_reserved_secret_hashes(self.reserved_secret_hashes),
        )
        if not (1 <= int(self.port_start) <= 65535):
            raise ValueError("port_start must be between 1 and 65535")
        if not (1 <= int(self.port_end) <= 65535):
            raise ValueError("port_end must be between 1 and 65535")
        if int(self.port_start) > int(self.port_end):
            raise ValueError("port_start must not exceed port_end")
        if int(self.port_start) <= PRODUCTION_REMOTE_PORT <= int(self.port_end):
            raise ValueError("the production remote port 18765 is forbidden")
        if self.reload_helper is not None:
            helper = Path(self.reload_helper).expanduser()
            if not helper.is_absolute():
                raise ValueError("reload_helper must be an absolute path")
            object.__setattr__(self, "reload_helper", helper)

    @classmethod
    def from_env(cls) -> "RegistrySettings":
        origin = os.environ.get("KT_MCP_PUBLIC_ORIGIN", "").strip()
        if not origin:
            raise ValueError("KT_MCP_PUBLIC_ORIGIN is required")
        start = int(os.environ.get("KT_MCP_PORT_START", str(DEFAULT_PORT_START)))
        end = int(os.environ.get("KT_MCP_PORT_END", str(DEFAULT_PORT_END)))
        helper_text = os.environ.get("KT_MCP_RELOAD_HELPER", "").strip()
        return cls(
            db_path=Path(
                os.environ.get(
                    "KT_MCP_REGISTRY_DB", "/var/lib/kt-mcp-provision/registry.sqlite3"
                )
            ),
            routes_path=Path(
                os.environ.get(
                    "KT_MCP_ROUTES_MAP", "/var/lib/kt-mcp-provision/routes.map"
                )
            ),
            public_origin=origin,
            port_start=start,
            port_end=end,
            reload_helper=Path(helper_text) if helper_text else None,
            reserved_secret_hashes=normalize_reserved_secret_hashes(
                os.environ.get("KT_MCP_RESERVED_SECRET_HASHES", "")
            ),
        )


@dataclass(frozen=True)
class Allocation:
    installation_id: str
    secret: str = field(repr=False)
    remote_port: int
    enabled: bool = True

    @property
    def path(self) -> str:
        return f"/mcp/{self.secret}"

    @property
    def url(self) -> str:
        raise RuntimeError("Allocation.url requires a Registry public origin")



def normalize_origin(value: str) -> str:
    """Return one canonical HTTPS origin with no path or credentials."""

    text = str(value or "").strip()
    if not text or any(character.isspace() for character in text):
        raise ValueError("public origin must be a valid HTTPS origin")
    try:
        parsed = urlsplit(text)
        _ = parsed.port
        hostname = parsed.hostname
    except (TypeError, ValueError) as exc:
        raise ValueError("public origin must be a valid HTTPS origin") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or not hostname
        or "\\" in parsed.netloc
        or parsed.netloc.endswith(":")
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("public origin must be an HTTPS origin without path/query")
    # DNS host names are case-insensitive; keep one canonical form for strict
    # origin comparisons with the client and reverse-proxy configuration.
    return f"https://{parsed.netloc.lower()}"


class AllocationRegistry:
    """SQLite-backed registry plus an Nginx server-context route include.

    The registry stores the URL-safe credential in a permissioned SQLite file
    so a repeat request from the same installation is idempotent. The generated
    Nginx include is also sensitive: it contains the credential in request
    paths and must be readable only by Nginx and this service.
    """

    def __init__(
        self,
        settings: RegistrySettings,
        *,
        reload: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._reload_override = reload
        if reload is None and settings.reload_helper is None:
            raise RegistryError("a reload helper is required before issuing device URLs")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.settings.db_path,
            timeout=10,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    installation_id TEXT PRIMARY KEY,
                    secret TEXT NOT NULL UNIQUE,
                    remote_port INTEGER NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS devices_enabled_port ON devices(enabled, remote_port)"
            )
            expected = self._routes_content(connection)
            try:
                current = self.settings.routes_path.read_text(encoding="ascii")
            except FileNotFoundError:
                current = None
            except (OSError, UnicodeError) as exc:
                raise RegistryError("cannot read the existing route map") from exc
            if current is None:
                if connection.execute("SELECT 1 FROM devices LIMIT 1").fetchone():
                    # Existing allocations must never start with a missing map;
                    # publish and validate the complete route set before serving.
                    self._sync_routes(connection)
                else:
                    self._write_routes(expected)
            elif current != expected:
                # A stale map can route an old secret or omit a live device.
                # Reconcile it at startup and fail closed if reload fails.
                self._sync_routes(connection)

    @staticmethod
    def _validate_installation_id(value: str) -> str:
        text = str(value or "").strip()
        if not INSTALLATION_ID_RE.fullmatch(text):
            raise InvalidInstallationID("installation_id must be 32 lowercase hex characters")
        return text

    def _row_to_allocation(self, row: sqlite3.Row) -> Allocation:
        installation_id = str(row["installation_id"])
        if not INSTALLATION_ID_RE.fullmatch(installation_id):
            raise RegistryError("registry contains an invalid installation_id")
        secret = str(row["secret"])
        if not SECRET_RE.fullmatch(secret):
            raise RegistryError("registry contains an invalid secret")
        try:
            remote_port = int(row["remote_port"])
        except (TypeError, ValueError) as exc:
            raise RegistryError("registry contains an invalid remote port") from exc
        if (
            not int(self.settings.port_start) <= remote_port <= int(self.settings.port_end)
            or remote_port == PRODUCTION_REMOTE_PORT
        ):
            raise RegistryError("registry contains an invalid remote port")
        return Allocation(
            installation_id=installation_id,
            secret=secret,
            remote_port=remote_port,
            enabled=bool(row["enabled"]),
        )

    def _choose_port(self, connection: sqlite3.Connection) -> int:
        # Disabled rows still retain their unique port in SQLite.  Keep those
        # ports reserved rather than selecting one that the INSERT constraint
        # can never accept.
        rows = connection.execute("SELECT remote_port FROM devices").fetchall()
        used = {int(row[0]) for row in rows}
        for port in range(int(self.settings.port_start), int(self.settings.port_end) + 1):
            if port != PRODUCTION_REMOTE_PORT and port not in used:
                return port
        raise CapacityExhausted("no independent reverse-forward port is available")

    def _routes_content(self, connection: sqlite3.Connection) -> str:
        rows = connection.execute(
            """
            SELECT secret, remote_port FROM devices
            WHERE enabled = 1 ORDER BY remote_port ASC
            """
        ).fetchall()
        lines = [
            "# Generated by kt-mcp-provision; contains endpoint credentials.",
            "# This is a server-context include; load it inside the existing Nginx server {} block.",
            "# Do not edit manually. The existing production /mcp/ location remains the fallback.",
        ]
        for row in rows:
            secret = str(row["secret"])
            if not SECRET_RE.fullmatch(secret):
                raise RegistryError("registry contains an invalid secret")
            try:
                port = int(row["remote_port"])
            except (TypeError, ValueError) as exc:
                raise RegistryError("registry contains an invalid remote port") from exc
            if (
                not int(self.settings.port_start) <= port <= int(self.settings.port_end)
                or port == PRODUCTION_REMOTE_PORT
            ):
                raise RegistryError("registry contains an invalid remote port")
            lines.extend(
                [
                    f"location = /mcp/{secret} {{",
                    f"    proxy_pass http://127.0.0.1:{port};",
                    "    proxy_http_version 1.1;",
                    "    proxy_set_header Upgrade $http_upgrade;",
                    '    proxy_set_header Connection "upgrade";',
                    "    proxy_set_header Host $host;",
                    "    proxy_set_header X-Real-IP $remote_addr;",
                    "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
                    "    proxy_set_header X-Forwarded-Proto $scheme;",
                    "    proxy_read_timeout 86400;",
                    "    proxy_buffering off;",
                    "}",
                    "",
                ]
            )
        return "\n".join(lines) + "\n"

    def _write_routes(self, content: str) -> None:
        target = self.settings.routes_path
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(temporary, 0o640)
            os.replace(temporary, target)
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                temporary.unlink()
            except OSError:
                pass
            raise

    def _reload_nginx(self) -> None:
        if self._reload_override is not None:
            self._reload_override()
            return
        helper = self.settings.reload_helper
        if helper is None:
            raise RegistryError("Nginx reload helper is not configured")
        result = subprocess.run(
            [str(helper)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            raise RegistryError("Nginx route reload failed")

    def _sync_routes(self, connection: sqlite3.Connection) -> None:
        target = self.settings.routes_path
        route_written = False
        try:
            existed = target.exists()
            previous = target.read_bytes() if existed else b""
            previous_mode = target.stat().st_mode & 0o7777 if existed else None
            self._write_routes(self._routes_content(connection))
            route_written = True
            self._reload_nginx()
        except Exception as exc:
            if route_written:
                try:
                    if existed:
                        try:
                            previous_text = previous.decode("ascii")
                        except UnicodeDecodeError:
                            previous_text = ""
                        self._write_routes(previous_text)
                        if previous_mode is not None and os.name != "nt":
                            os.chmod(target, previous_mode)
                    else:
                        target.unlink(missing_ok=True)
                    # A helper may have applied the new map before reporting a
                    # failure. Reload the restored map before failing closed.
                    self._reload_nginx()
                except Exception:
                    # The original synchronization error is the useful one;
                    # the service must remain fail-closed rather than return a URL.
                    pass
            if isinstance(exc, RegistryError):
                raise
            raise RegistryError("Nginx route synchronization failed") from exc

    def provision(self, installation_id: str, origin: str, endpoint_url: str) -> Allocation:
        installation = self._validate_installation_id(installation_id)
        requested_origin = normalize_origin(origin)
        endpoint_origin, requested_secret = parse_endpoint_url(endpoint_url)
        if requested_origin != endpoint_origin:
            raise OriginMismatch("origin and endpoint_url do not match")
        if requested_origin != self.settings.public_origin:
            raise OriginMismatch("origin is not served by this provisioning API")
        if (
            hashlib.sha256(requested_secret.encode("ascii")).hexdigest()
            in self.settings.reserved_secret_hashes
        ):
            # Compare only the digest. Never persist, print, or return the
            # production secret represented by the configured reservation.
            raise EndpointConflict("MCP secret is reserved")

        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM devices WHERE installation_id = ?",
                    (installation,),
                ).fetchone()
                if row is not None:
                    allocation = self._row_to_allocation(row)
                    if not allocation.enabled:
                        raise RegistryError("device allocation is disabled")
                    if allocation.secret != requested_secret:
                        raise EndpointConflict(
                            "installation_id is already registered to another MCP secret"
                        )
                if row is None:
                    if connection.execute(
                        "SELECT 1 FROM devices WHERE secret = ?", (requested_secret,)
                    ).fetchone():
                        raise EndpointConflict("MCP secret is already registered")
                    port = self._choose_port(connection)
                    connection.execute(
                        "INSERT INTO devices(installation_id, secret, remote_port) VALUES (?, ?, ?)",
                        (installation, requested_secret, port),
                    )
                    row = connection.execute(
                        "SELECT * FROM devices WHERE installation_id = ?",
                        (installation,),
                    ).fetchone()
                allocation = self._row_to_allocation(row)
                try:
                    self._sync_routes(connection)
                    # Keep the IMMEDIATE transaction open while rendering and
                    # reloading the route map.  Committing before this point
                    # lets another worker publish a later map and can leave a
                    # stale route behind when this allocation must be rolled
                    # back after a reload failure.
                    try:
                        connection.commit()
                    except Exception:
                        # A commit can fail after the helper accepted the map.
                        # Roll back the DB first, then best-effort resync the
                        # map from the durable state so no orphaned route is
                        # returned or left active.
                        try:
                            connection.rollback()
                            self._sync_routes(connection)
                        except Exception:
                            pass
                        raise
                except Exception:
                    # The open transaction rolls back a newly allocated row;
                    # existing rows remain idempotent and can be retried after
                    # a transient reload failure.
                    raise
                return allocation
            except Exception:
                try:
                    connection.rollback()
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()

    def public_url(self, allocation: Allocation) -> str:
        return f"{self.settings.public_origin}{allocation.path}"

    def list_allocations(self) -> list[Allocation]:
        with self._lock:
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT * FROM devices ORDER BY remote_port ASC"
                ).fetchall()
                return [self._row_to_allocation(row) for row in rows]


__all__ = [
    "Allocation",
    "AllocationRegistry",
    "CapacityExhausted",
    "DEFAULT_PORT_END",
    "DEFAULT_PORT_START",
    "INSTALLATION_ID_RE",
    "InvalidInstallationID",
    "OriginMismatch",
    "EndpointConflict",
    "parse_endpoint_url",
    "normalize_reserved_secret_hashes",
    "PRODUCTION_REMOTE_PORT",
    "RegistryError",
    "RegistrySettings",
    "SECRET_HASH_RE",
    "SECRET_RE",
    "normalize_origin",
]
