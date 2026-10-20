"""Minimal provisioning API for a separately deployed gateway.

This application is intentionally not imported by the Windows client.  It is a
server-side artifact that must be deployed behind the existing HTTPS origin by
an operator who controls the VPS. It returns an endpoint only after the
secret-to-port route has been atomically persisted and the reverse proxy has
accepted the reload.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .registry import (
    AllocationRegistry,
    CapacityExhausted,
    EndpointConflict,
    InvalidInstallationID,
    OriginMismatch,
    RegistryError,
    RegistrySettings,
)
from .service import MAX_BODY_BYTES, RateLimiter


class ProvisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: str = Field(min_length=1, max_length=512)
    installation_id: str = Field(min_length=32, max_length=32)
    endpoint_url: str = Field(min_length=1, max_length=512)


class ProvisionResponse(BaseModel):
    url: str
    remote_port: int


@dataclass(frozen=True)
class AppSecurity:
    """Required shared secret for controlled provisioning enrollment."""

    admin_token: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> "AppSecurity":
        return cls(os.environ.get("KT_MCP_PROVISION_ADMIN_TOKEN", "").strip())

    def check(self, supplied: str | None) -> None:
        if not self.admin_token:
            raise HTTPException(status_code=503, detail="provisioning authorization is not configured")
        if not hmac.compare_digest(supplied or "", self.admin_token):
            raise HTTPException(status_code=401, detail="provisioning authorization failed")


def create_app(
    registry: AllocationRegistry | None = None,
    *,
    security: AppSecurity | None = None,
    rate_limiter: RateLimiter | None = None,
) -> FastAPI:
    if registry is None:
        registry = AllocationRegistry(RegistrySettings.from_env())
    security = AppSecurity.from_env() if security is None else security
    rate_limiter = RateLimiter() if rate_limiter is None else rate_limiter
    app = FastAPI(title="KT MCP provisioning", docs_url=None, redoc_url=None)
    app.state.registry = registry
    app.state.security = security
    app.state.rate_limiter = rate_limiter

    @app.middleware("http")
    async def enforce_provision_body_limit(request: Request, call_next: Any):
        # Starlette/Pydantic otherwise buffers an unbounded body before the
        # endpoint can validate the small provisioning schema.
        if request.method != "POST" or request.url.path != "/provision":
            return await call_next(request)
        content_length = request.headers.get("content-length")
        if content_length is None:
            return JSONResponse(
                {"detail": "request body too large"}, status_code=413
            )
        content_length = content_length.strip()
        if not content_length.isdigit() or int(content_length) > MAX_BODY_BYTES:
            return JSONResponse(
                {"detail": "request body too large"}, status_code=413
            )
        chunks: list[bytes] = []
        body_size = 0
        async for chunk in request.stream():
            if not chunk:
                continue
            body_size += len(chunk)
            if body_size > MAX_BODY_BYTES:
                return JSONResponse(
                    {"detail": "request body too large"}, status_code=413
                )
            chunks.append(chunk)
        # Cache the bounded body so BaseHTTPMiddleware can replay it to
        # FastAPI/Pydantic instead of handing the downstream app an exhausted
        # receive channel.
        request._body = b"".join(chunks)
        return await call_next(request)

    def client_key(request: Request) -> str:
        peer = str(request.client.host if request.client is not None else "unknown")
        # Only trust X-Real-IP when the ASGI peer is a local reverse proxy.
        # A direct/public client must not be able to forge a new limiter key.
        if peer not in {"127.0.0.1", "::1"}:
            return peer
        forwarded = request.headers.get("x-real-ip", "").strip()
        if forwarded and len(forwarded) <= 128 and "\r" not in forwarded and "\n" not in forwarded:
            try:
                ipaddress.ip_address(forwarded)
            except ValueError:
                pass
            else:
                return forwarded
        return peer

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/provision", response_model=ProvisionResponse)
    def provision(
        payload: ProvisionRequest,
        request: Request,
        x_kt_provision_token: str | None = Header(default=None),
    ) -> ProvisionResponse:
        if not app.state.rate_limiter.allow(client_key(request)):
            raise HTTPException(status_code=429, detail="too many provisioning requests")
        security.check(x_kt_provision_token)
        try:
            allocation = registry.provision(
                payload.installation_id, payload.origin, payload.endpoint_url
            )
        except (InvalidInstallationID, OriginMismatch, EndpointConflict, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except CapacityExhausted as exc:
            raise HTTPException(status_code=503, detail="no independent device capacity") from exc
        except RegistryError as exc:
            # Do not return a URL if route persistence or Nginx reload failed.
            raise HTTPException(
                status_code=503,
                detail="provisioning is temporarily unavailable",
            ) from exc
        except Exception as exc:
            # Keep unexpected SQLite/filesystem/helper failures fail-closed and
            # avoid exposing deployment details through FastAPI responses.
            raise HTTPException(
                status_code=503,
                detail="provisioning is temporarily unavailable",
            ) from exc
        return ProvisionResponse(
            url=registry.public_url(allocation),
            remote_port=allocation.remote_port,
        )

    return app


def _unconfigured_app() -> FastAPI:
    """Fail closed when an operator forgot the deployment environment."""

    unconfigured = FastAPI(
        title="KT MCP provisioning (not configured)",
        docs_url=None,
        redoc_url=None,
    )

    @unconfigured.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "not-configured"}

    @unconfigured.post("/provision", include_in_schema=False)
    def provision_unconfigured() -> Any:
        raise HTTPException(status_code=503, detail="provisioning service is not configured")

    return unconfigured


try:
    app = create_app()
except (ValueError, RegistryError):
    # Importing the module remains safe for tests and tooling, but a missing
    # deployment environment or reload hook never silently allocates a URL.
    app = _unconfigured_app()


__all__ = [
    "AppSecurity",
    "ProvisionRequest",
    "ProvisionResponse",
    "app",
    "create_app",
]
