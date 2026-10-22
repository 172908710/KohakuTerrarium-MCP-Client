"""Dependency-free HTTP service for the separate provisioning API.

Run this only behind the existing HTTPS reverse proxy.  It binds to loopback,
never serves the MCP stream itself, and returns a URL only after the registry
has synchronized the secret-to-port map and reloaded the proxy successfully.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .registry import (
    AllocationRegistry,
    CapacityExhausted,
    EndpointConflict,
    InvalidInstallationID,
    OriginMismatch,
    RegistryError,
    RegistrySettings,
)

from .enrollment import EnrollmentService, EnrollmentSettings, EnrollmentDenied, EnrollmentConflict

MAX_BODY_BYTES = 4096
RATE_LIMIT_COUNT = 30
RATE_LIMIT_WINDOW_SECONDS = 600.0


class RateLimiter:
    def __init__(self, limit: int = RATE_LIMIT_COUNT, window: float = RATE_LIMIT_WINDOW_SECONDS) -> None:
        self.limit = limit
        self.window = window
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            events = self._events[key]
            while events and now - events[0] >= self.window:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True


class ProvisioningHandler(BaseHTTPRequestHandler):
    server: "ProvisioningHTTPServer"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        # Do not log request bodies or endpoint credentials.  systemd captures
        # only concise method/path/status lines when diagnostics are needed.
        return

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def _client_key(self) -> str:
        peer = str(self.client_address[0])
        # Only trust the proxy-provided address when the TCP peer is local.
        # If this service is accidentally bound publicly, an attacker must
        # not be able to forge X-Real-IP values to evade the limiter.
        if peer not in {"127.0.0.1", "::1"}:
            return peer
        forwarded = self.headers.get("X-Real-IP", "").strip()
        if forwarded and len(forwarded) <= 128 and "\r" not in forwarded and "\n" not in forwarded:
            try:
                ipaddress.ip_address(forwarded)
            except ValueError:
                pass
            else:
                return forwarded
        return peer

    def _authorized(self) -> bool:
        expected = self.server.provision_token
        supplied = self.headers.get("X-KT-Provision-Token", "")
        return bool(expected) and hmac.compare_digest(supplied, expected)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/healthz":
            self._json(200, {"status": "ok"})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/provision":
            self._json(404, {"error": "not found"})
            return
        if not self.server.rate_limiter.allow(self._client_key()):
            self._json(429, {"error": "too many provisioning requests"})
            return
        # Count failed attempts too, so a leaked or guessed token cannot be
        # brute-forced without tripping the same per-client limiter.
        token = self.headers.get("X-KT-Provision-Token", "")
        customer_key = token.startswith("ktd_")
        if customer_key:
            if self.server.enrollment is None:
                self._json(503, {"error": "automatic device enrollment is not enabled"})
                return
            try:
                self.server.enrollment.check(token)
            except EnrollmentDenied:
                self._json(401, {"error": "invalid device activation key"})
                return
            except Exception:
                self._json(503, {"error": "device enrollment unavailable"})
                return
        elif not self._authorized():
            self._json(401, {"error": "provisioning authorization failed"})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._json(413, {"error": "request body too large"})
            return
        try:
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("invalid request object")
            device_mode = "enrollment_version" in payload or "ssh_public_key" in payload
            if customer_key and not device_mode:
                self._json(401, {"error": "customer keys require device enrollment"})
                return
            fields = {"origin", "installation_id", "endpoint_url"}
            if device_mode:
                fields |= {"enrollment_version", "ssh_public_key"}
            if not isinstance(payload, dict) or set(payload) != fields:
                raise ValueError("invalid request fields")
            if device_mode and (type(payload["enrollment_version"]) is not int or payload["enrollment_version"] != 2):
                raise ValueError("invalid device enrollment version")
            origin = payload["origin"]
            installation_id = payload["installation_id"]
            endpoint_url = payload["endpoint_url"]
            if not all(isinstance(value, str) for value in (origin, installation_id, endpoint_url)):
                raise ValueError("invalid request types")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            self._json(400, {"error": "invalid provisioning request"})
            return
        if device_mode:
            if self.server.enrollment is None:
                self._json(503, {"error": "automatic device enrollment is not enabled"})
                return
            try:
                if customer_key:
                    enrolled = self.server.enrollment.provision(token, installation_id, origin, endpoint_url, payload["ssh_public_key"])
                else:
                    enrolled = self.server.enrollment.provision_shared(
                        token, installation_id, origin, endpoint_url, payload["ssh_public_key"],
                        expected_token=self.server.provision_token,
                    )
            except EnrollmentDenied:
                self._json(401, {"error": "invalid device activation key"})
            except (EnrollmentConflict, EndpointConflict):
                self._json(409, {"error": "device activation conflict"})
            except ValueError:
                self._json(400, {"error": "invalid device enrollment"})
            except Exception:
                self._json(503, {"error": "device enrollment unavailable"})
            else:
                self._json(200, enrolled)
            return
        try:
            allocation = self.server.registry.provision(
                installation_id, origin, endpoint_url
            )
        except (InvalidInstallationID, OriginMismatch, EndpointConflict, ValueError) as exc:
            self._json(400, {"error": str(exc)})
            return
        except CapacityExhausted:
            self._json(503, {"error": "no independent device capacity"})
            return
        except RegistryError:
            # Never return an endpoint if route synchronization failed.
            self._json(503, {"error": "provisioning is temporarily unavailable"})
            return
        except Exception:
            # Filesystem, SQLite, or subprocess failures are deliberately
            # fail-closed and must still produce a complete HTTP response.
            self._json(503, {"error": "provisioning is temporarily unavailable"})
            return
        self._json(
            200,
            {
                "url": self.server.registry.public_url(allocation),
                "remote_port": allocation.remote_port,
            },
        )


class ProvisioningHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        registry: AllocationRegistry,
        *,
        provision_token: str = "",
        enrollment: EnrollmentService | None = None,
    ) -> None:
        token = provision_token.strip()
        if not token:
            raise ValueError("KT_MCP_PROVISION_ADMIN_TOKEN 必须配置；拒绝匿名 provisioning")
        super().__init__(address, ProvisioningHandler)
        self.registry = registry
        self.enrollment = enrollment
        self.provision_token = token
        self.rate_limiter = RateLimiter()


def main() -> None:
    token = os.environ.get("KT_MCP_PROVISION_ADMIN_TOKEN", "").strip()
    if not token:
        raise SystemExit("KT_MCP_PROVISION_ADMIN_TOKEN 必须配置；拒绝匿名 provisioning")
    settings = RegistrySettings.from_env()
    registry = AllocationRegistry(settings)
    host = os.environ.get("KT_MCP_PROVISION_BIND", "127.0.0.1")
    port = int(os.environ.get("KT_MCP_PROVISION_PORT", "18080"))
    enrollment_settings = EnrollmentSettings.from_env()
    enrollment = EnrollmentService(registry, enrollment_settings) if enrollment_settings else None
    server = ProvisioningHTTPServer((host, port), registry, provision_token=token, enrollment=enrollment)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":  # pragma: no cover
    main()
