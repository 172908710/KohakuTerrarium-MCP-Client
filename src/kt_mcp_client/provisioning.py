from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
import urllib.request
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

import httpx

from .config import (
    ConfigError,
    DeviceProfile,
    endpoint_proof_is_current,
    mcp_url,
    config_file,
    parse_endpoint_url,
    save,
    PRODUCTION_REMOTE_FORWARD_PORT,
)


KT_MCP_PROVISION_URL = "KT_MCP_PROVISION_URL"
KT_MCP_PROVISION_TOKEN = "KT_MCP_PROVISION_TOKEN"
# Optional explicit origin -> provisioning-host allowlist for deployments whose
# registration API is served on a different HTTPS host.
KT_MCP_PROVISION_ORIGIN_MAP = "KT_MCP_PROVISION_ORIGIN_MAP"
# Public deployments must be entered per device; never embed a real origin in
# source or a distributable package.
DEFAULT_ORIGIN = "https://example.invalid/"
PRODUCTION_CONNECTION_PATH = Path(
    "C:/Users/Administrator/AppData/Local/KT-MCP-Bridge/"
    "kt-home/mcp-serve/endpoint/connection.json"
)
INSTALLATION_ID_FILENAME = "installation_id"
PROVISION_TIMEOUT = 15.0
# The independent profile mirrors the live formal MCP surface. Keep this
# explicit so a missing Studio hook cannot be mistaken for successful public
# enrollment merely because ``initialize`` returned 200.
EXPECTED_PARITY_TOOLS = frozenset({
    "read", "write", "edit", "multi_edit", "glob", "grep", "tree", "bash", "python",
    "job_status", "job_wait", "job_cancel", "job_promote", "workspaces",
    "sessions_list", "session_read", "session_history_detail", "session_send",
    "session_message_read", "goals_list", "goal_get", "goal_update", "goal_transition",
})
# Keep the client-side response boundary small enough that a compromised or
# misconfigured provisioning service cannot make the client buffer arbitrary
# data before JSON validation.
MAX_RESPONSE_BYTES = 64 * 1024

# This is intentionally also used for connection failures and a missing/invalid
# override. The client must not make up an endpoint when the server-side API is
# absent.
PROVISIONING_NOT_CONFIGURED_MESSAGE = (
    "VPS/服务器端 provisioning API 未配置，无法生成独立 MCP 地址。"
)

_INSTALLATION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class ProvisioningError(RuntimeError):
    """Raised when an independent MCP address cannot be provisioned."""


class ProvisioningUnavailable(ProvisioningError):
    """Raised when the server-side provisioning API is not available."""


@dataclass(frozen=True)
class ProvisionedEndpoint:
    """Validated endpoint metadata returned by the provisioning service."""

    url: str = field(repr=False)
    remote_port: int | None = None


@dataclass(frozen=True)
class PublicVerification:
    """Evidence collected from the public MCP route after tunnel startup."""

    protocol_version: str
    tools: tuple[str, ...]
    readonly_tool: str


# A named type makes the small HTTP boundary easy to replace in tests without
# importing or starting a GUI.
URLOpener = Callable[..., Any]


def normalize_origin(value: str) -> str:
    """Validate and canonicalize an HTTPS origin without a path."""

    text = str(value or "").strip()
    if not text or any(character.isspace() for character in text):
        raise ProvisioningError("请输入有效的 HTTPS origin")
    try:
        parsed = urlsplit(text)
        # Accessing port and hostname catches malformed bracketed hosts and
        # empty-host forms before retaining the conservative origin rules.
        _ = parsed.port
        hostname = parsed.hostname
    except (TypeError, ValueError) as exc:
        raise ProvisioningError("请输入有效的 HTTPS origin") from exc
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
        raise ProvisioningError("origin 必须是无 path/query/fragment 的 HTTPS URL")
    # Host names are case-insensitive; upstream KT writes the canonical origin
    # with a lower-case netloc in connection.json.
    return f"https://{parsed.netloc.lower()}"


def canonicalize_endpoint_url(
    value: str,
    expected_origin: str | None = None,
) -> str:
    """Validate and canonicalize a complete endpoint emitted by KT.

    The endpoint URL, including its bearer secret, is the local KT runtime's
    identity.  A provisioning service may register it, but it may not replace
    it with another secret.
    """

    try:
        returned_origin, path, secret = parse_endpoint_url(str(value or ""))
    except ConfigError as exc:
        raise ProvisioningError(
            "独立 KT runtime 未返回有效的完整 endpoint URL"
        ) from exc
    if expected_origin is not None:
        canonical_origin = normalize_origin(expected_origin)
        if returned_origin != canonical_origin:
            raise ProvisioningError(
                "endpoint_url 的 origin 与输入 origin 不一致"
            )
    return f"{returned_origin}{path}{secret}"


def discover_production_origin(
    path: str | os.PathLike[str] | None = None,
) -> str | None:
    """Read only ``public_origin`` from the production connection metadata.

    The production file is an origin hint, not an endpoint source. In
    particular, no production endpoint URL or secret is read from the parsed
    payload or copied into the independent profile.
    """

    connection_path = PRODUCTION_CONNECTION_PATH if path is None else Path(path)
    try:
        with connection_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("public_origin")
    if not isinstance(value, str):
        return None
    try:
        return normalize_origin(value)
    except ProvisioningError:
        return None


def discover_default_origin(
    path: str | os.PathLike[str] | None = None,
) -> str:
    """Return the production origin when valid, otherwise the safe default."""

    return discover_production_origin(path) or DEFAULT_ORIGIN


# A concise alias is useful to callers that only need the GUI's default.
discover_origin = discover_default_origin


def _mapped_override_hosts(origin: str) -> set[str]:
    """Return explicitly configured provisioning netlocs for one origin.

    The mapping is JSON, for example:
    {"https://device.example": ["provision.example/api"]}.  It is deliberately
    exact and fail-closed; no wildcard or suffix matching is accepted.
    """
    raw = os.environ.get(KT_MCP_PROVISION_ORIGIN_MAP, "").strip()
    if not raw:
        return set()
    try:
        mapping = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProvisioningUnavailable(
            f"{KT_MCP_PROVISION_ORIGIN_MAP} 必须是 JSON origin 到 HTTPS host 的映射。"
        ) from exc
    if not isinstance(mapping, Mapping):
        raise ProvisioningUnavailable(
            f"{KT_MCP_PROVISION_ORIGIN_MAP} 必须是 JSON 对象。"
        )
    values = mapping.get(origin)
    if values is None:
        return set()
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        raise ProvisioningUnavailable(
            f"{KT_MCP_PROVISION_ORIGIN_MAP} 的映射值必须是 HTTPS host 或列表。"
        )
    hosts: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text or any(char.isspace() for char in text):
            raise ProvisioningUnavailable(
                f"{KT_MCP_PROVISION_ORIGIN_MAP} 包含无效 host。"
            )
        candidate = text if "://" in text else f"https://{text}"
        try:
            parsed = urlsplit(candidate)
            _ = parsed.port
            hostname = parsed.hostname
        except (TypeError, ValueError) as exc:
            raise ProvisioningUnavailable(
                f"{KT_MCP_PROVISION_ORIGIN_MAP} 包含无效 HTTPS host。"
            ) from exc
        if (
            parsed.scheme.casefold() != "https"
            or not parsed.netloc
            or not hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or "\\" in parsed.netloc
            or parsed.netloc.endswith(":")
        ):
            raise ProvisioningUnavailable(
                f"{KT_MCP_PROVISION_ORIGIN_MAP} 只能包含无 path/query 的 HTTPS host。"
            )
        hosts.add(parsed.netloc.lower())
    return hosts


def provisioning_url(origin: str) -> str:
    """Resolve the provisioning API without trusting a user-entered origin.

    An explicit HTTPS override is the only configurable registration target.
    Without one, registration is pinned to DEFAULT_ORIGIN; this prevents the
    bearer token from being sent to an arbitrary origin typed into the GUI.
    """
    canonical_origin = normalize_origin(origin)
    override = os.environ.get(KT_MCP_PROVISION_URL, "").strip()
    if override:
        try:
            parsed = urlsplit(override)
            _ = parsed.port
            hostname = parsed.hostname
        except (TypeError, ValueError) as exc:
            raise ProvisioningUnavailable(
                "服务器注册地址无效：请检查 KT_MCP_PROVISION_URL，必须为 HTTPS 地址。"
            ) from exc
        if (
            parsed.scheme.casefold() != "https"
            or not parsed.netloc
            or not hostname
            or "\\" in parsed.netloc
            or parsed.netloc.endswith(":")
            or parsed.username
            or parsed.password
            or any(character.isspace() for character in override)
            or parsed.query
            or parsed.fragment
            or parsed.path in {"", "/"}
        ):
            raise ProvisioningUnavailable(
                "服务器注册地址无效：请检查 KT_MCP_PROVISION_URL，必须为 HTTPS API 地址。"
            )
        override_host = parsed.netloc.lower()
        origin_host = urlsplit(canonical_origin).netloc.lower()
        if override_host != origin_host and override_host not in _mapped_override_hosts(canonical_origin):
            raise ProvisioningUnavailable(
                "KT_MCP_PROVISION_URL 的 host 与 origin 不一致；请配置显式安全映射。"
            )
        return override

    fallback = normalize_origin(DEFAULT_ORIGIN)
    if canonical_origin != fallback:
        raise ProvisioningUnavailable(
            "未配置 KT_MCP_PROVISION_URL；为避免向用户输入的 origin 发送 token，"
            "仅允许使用默认安全 origin，或显式配置 HTTPS override。"
        )
    return f"{fallback}/provision"


def _registration_token(profile: DeviceProfile | None = None) -> str:
    # Prefer the DPAPI-protected token loaded into the profile so the portable
    # GUI works after a fresh launch. Keep the environment variable as a
    # one-shot override for scripts and backwards compatibility.
    token = str(getattr(profile, "provision_token", "") or "").strip()
    if not token:
        token = os.environ.get(KT_MCP_PROVISION_TOKEN, "").strip()
    if not token:
        raise ProvisioningUnavailable(
            "缺少服务器注册凭据 KT_MCP_PROVISION_TOKEN；未发送注册请求。"
            "请配置与服务器匹配的有效凭据后重试。"
        )
    if len(token) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise ProvisioningError("服务器注册凭据格式无效；未发送注册请求。")
    return token


def check_provisioning_configuration(
    origin: str, *, profile: DeviceProfile | None = None
) -> None:
    """Fail before starting KT when enrollment cannot possibly succeed.

    Local checks only: no endpoint/installation ID creation, network requests,
    credentials returned, or claims that the remote service is reachable.
    """
    provisioning_url(origin)
    _registration_token(profile)


def installation_id_path(config_path: str | os.PathLike[str] | None = None) -> Path:
    """Return the non-secret installation ID path in the independent root."""

    return config_file(config_path).parent / INSTALLATION_ID_FILENAME


def _write_installation_id(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as handle:
            handle.write(value)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def get_installation_id(config_path: str | os.PathLike[str] | None = None) -> str:
    """Load or create a stable, non-secret installation identifier."""

    path = installation_id_path(config_path)
    try:
        existing = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        existing = ""
    except (OSError, UnicodeError) as exc:
        raise ProvisioningError("无法读取独立 installation_id") from exc
    if _INSTALLATION_ID_PATTERN.fullmatch(existing):
        return existing

    value = uuid.uuid4().hex
    try:
        _write_installation_id(path, value)
    except OSError as exc:
        raise ProvisioningError("无法保存独立 installation_id") from exc
    return value


def _no_redirect_opener(request: Request, *, timeout: float) -> Any:
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    return opener.open(request, timeout=timeout)


def _response_status(response: Any) -> int | None:
    try:
        value = getattr(response, "status", None)
        if value is None:
            getcode = getattr(response, "getcode", None)
            value = getcode() if callable(getcode) else None
        return int(value) if value is not None else None
    except (TypeError, ValueError, AttributeError):
        return None


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            # Closing is best effort and must not mask the boundary error.
            pass


def _response_content_length(response: Any) -> int | None:
    """Return a validated Content-Length when the response exposes one."""

    value: Any = None
    getheader = getattr(response, "getheader", None)
    if callable(getheader):
        try:
            value = getheader("Content-Length")
        except Exception:
            # A response wrapper may expose a broken optional header accessor;
            # the bounded read below is still authoritative.
            value = None
    if value is None:
        headers = getattr(response, "headers", None)
        getter = getattr(headers, "get", None)
        if callable(getter):
            try:
                value = getter("Content-Length")
            except Exception:
                value = None
    if value is None:
        return None
    try:
        text = str(value).strip()
        if not text or not text.isdigit():
            raise ValueError
        length = int(text)
    except (TypeError, ValueError) as exc:
        raise ProvisioningError("provisioning API 响应长度无效") from exc
    if length > MAX_RESPONSE_BYTES:
        raise ProvisioningError("provisioning API 响应过大")
    return length


def _read_response(response: Any) -> bytes:
    try:
        _response_content_length(response)
        try:
            # urllib responses support a bounded read. A few small test and
            # embedding doubles only provide read() with no size parameter.
            try:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            except TypeError:
                raw = response.read()
        except ProvisioningError:
            raise
        except Exception as exc:
            raise ProvisioningError("provisioning API 响应读取失败") from exc
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, bytes):
            raise ProvisioningError("provisioning API 返回了无效响应")
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProvisioningError("provisioning API 响应过大")
        return raw
    finally:
        _close_response(response)


def _response_payload(raw: bytes) -> Mapping[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProvisioningError("provisioning API 返回的不是有效 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ProvisioningError("provisioning API JSON 响应格式无效")
    return payload


def _parse_remote_port(payload: Mapping[str, Any]) -> int:
    if "remote_port" not in payload:
        raise ProvisioningError("provisioning API 未返回 remote_port；未保存注册结果")
    value = payload.get("remote_port")
    if isinstance(value, bool):
        raise ProvisioningError("provisioning API 返回的 remote_port 无效")
    if isinstance(value, int):
        port = value
    elif isinstance(value, str) and value.strip().isdigit():
        port = int(value.strip())
    else:
        raise ProvisioningError("provisioning API 返回的 remote_port 无效")
    if not 1 <= port <= 65535 or port == PRODUCTION_REMOTE_FORWARD_PORT:
        raise ProvisioningError("provisioning API 返回的 remote_port 无效")
    return port


def _decode_mcp_response(response: httpx.Response) -> Mapping[str, Any]:
    response.raise_for_status()
    content_type = response.headers.get("content-type", "").casefold()
    if "application/json" in content_type:
        payload = response.json()
        if isinstance(payload, Mapping):
            return payload
    for line in response.text.splitlines():
        if line.startswith("data: "):
            try:
                payload = json.loads(line[6:])
            except json.JSONDecodeError as exc:
                raise ProvisioningError("公网 MCP 返回了无效 JSON") from exc
            if isinstance(payload, Mapping):
                return payload
    raise ProvisioningError("公网 MCP 没有返回 JSON-RPC 响应")


def verify_public_endpoint(
    endpoint_url: str,
    profile: DeviceProfile,
    *,
    timeout: float = PROVISION_TIMEOUT,
) -> PublicVerification:
    """Prove the registered route reaches this device's MCP runtime.

    Registration and an alive SSH process are not sufficient: this performs
    MCP initialize, tools/list, and a harmless tree call in the configured
    default workspace before a URL is reported as usable.
    """

    canonical_endpoint = canonicalize_endpoint_url(endpoint_url, profile.public_origin)
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    try:
        # Keep the portable runtime dependency-free: HTTP/1.1 is sufficient
        # for the MCP POST/SSE contract and does not require the optional h2
        # package.
        with httpx.Client(timeout=timeout, trust_env=False, http2=False) as client:
            init_response = client.post(
                canonical_endpoint,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "kt-mcp-client", "version": "1"},
                    },
                },
            )
            initialized = _decode_mcp_response(init_response)
            result = initialized.get("result")
            if not isinstance(result, Mapping):
                raise ProvisioningError("公网 MCP initialize 未返回 result")
            session_id = (
                init_response.headers.get("mcp-session-id")
                or result.get("sessionId")
                or result.get("session_id")
            )
            if isinstance(session_id, str) and session_id:
                headers["Mcp-Session-Id"] = session_id
            protocol_version = str(result.get("protocolVersion") or "")
            if not protocol_version:
                raise ProvisioningError("公网 MCP initialize 未返回 protocolVersion")
            headers["MCP-Protocol-Version"] = protocol_version
            notice = client.post(
                canonical_endpoint,
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            notice.raise_for_status()
            listed = _decode_mcp_response(client.post(
                canonical_endpoint,
                headers=headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            ))
            listed_result = listed.get("result")
            raw_tools = listed_result.get("tools") if isinstance(listed_result, Mapping) else None
            if not isinstance(raw_tools, list):
                raise ProvisioningError("公网 MCP tools/list 未返回工具列表")
            tool_names = tuple(
                str(item.get("name"))
                for item in raw_tools
                if isinstance(item, Mapping) and isinstance(item.get("name"), str)
            )
            missing_tools = sorted(EXPECTED_PARITY_TOOLS.difference(tool_names))
            if missing_tools:
                raise ProvisioningError(
                    "公网 MCP 工具集未与正式版对齐；缺少：" + ", ".join(missing_tools)
                )
            readonly_tool = "tree" if "tree" in tool_names else ("workspaces" if "workspaces" in tool_names else "")
            if not readonly_tool:
                raise ProvisioningError("公网 MCP 缺少默认只读工具")
            arguments: dict[str, Any] = {"workspace_id": profile.workspace_id}
            if readonly_tool == "tree":
                arguments["path"] = "."
            called = _decode_mcp_response(client.post(
                canonical_endpoint,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": readonly_tool, "arguments": arguments},
                },
            ))
            called_result = called.get("result")
            if not isinstance(called_result, Mapping) or called_result.get("isError") is True:
                raise ProvisioningError("公网 MCP 默认只读工具调用失败")
    except ProvisioningError:
        raise
    except (httpx.HTTPError, TimeoutError, OSError, ValueError, TypeError) as exc:
        raise ProvisioningUnavailable("公网 MCP 连通性验证失败；地址未确认可用。") from exc
    return PublicVerification(protocol_version, tool_names, readonly_tool)


def _validate_current_profile(
    origin: str, endpoint_url: str | os.PathLike[str] | None, profile: DeviceProfile | None
) -> str:
    """Validate the exact endpoint and live in-memory provenance proof."""

    if not isinstance(profile, DeviceProfile):
        raise ProvisioningError("必须传入本次流程的当前内存 DeviceProfile")
    active_proof = getattr(profile, "_active_runtime_proof", None)
    if endpoint_url is None or not isinstance(endpoint_url, str) or not endpoint_url:
        raise ProvisioningError("必须传入本次独立 KT url 命令返回的 endpoint_url；未发送 provisioning 请求")
    if profile.endpoint_url != endpoint_url:
        raise ProvisioningError("profile endpoint 与本次 KT endpoint 不一致；未发送 provisioning 请求")
    if (
        profile.endpoint_verified is not True
        or profile.provenance is not True
        or not profile.proof
        or not active_proof
        or not endpoint_proof_is_current(profile, active_proof)
    ):
        raise ProvisioningError("独立 KT runtime endpoint 未通过当前内存 provenance proof；未发送 provisioning 请求")
    try:
        if mcp_url(profile) != endpoint_url:
            raise ProvisioningError("profile endpoint 与本次 KT endpoint 不一致；未发送 provisioning 请求")
    except ConfigError as exc:
        raise ProvisioningError("profile endpoint 未通过当前内存 provenance proof；未发送 provisioning 请求") from exc
    canonical_origin = normalize_origin(origin)
    canonical_endpoint = canonicalize_endpoint_url(endpoint_url, canonical_origin)
    if canonical_endpoint != endpoint_url:
        raise ProvisioningError("profile endpoint 不是有效的 canonical KT endpoint；未发送 provisioning 请求")
    return canonical_endpoint


def _request_provisioned_endpoint(
    origin: str,
    endpoint_url: str,
    *,
    profile: DeviceProfile | None,
    config_path: str | os.PathLike[str] | None = None,
    opener: URLOpener | None = None,
    timeout: float = PROVISION_TIMEOUT,
) -> ProvisionedEndpoint:
    """Register one endpoint explicitly returned by the local KT runtime."""

    canonical_origin = normalize_origin(origin)
    canonical_endpoint = _validate_current_profile(canonical_origin, endpoint_url, profile)
    # config_path is used only for the non-secret installation identifier. It
    # is never loaded as a profile or endpoint proof.
    endpoint = provisioning_url(canonical_origin)
    token = _registration_token(profile)
    installation_id = get_installation_id(config_path)
    body = json.dumps(
        {
            "origin": canonical_origin,
            "endpoint_url": canonical_endpoint,
            "installation_id": installation_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers["X-KT-Provision-Token"] = token
    request = Request(endpoint, data=body, headers=headers, method="POST")
    open_url = opener or _no_redirect_opener
    try:
        response = open_url(request, timeout=timeout)
        status = _response_status(response)
        if status == 404:
            _close_response(response)
            raise ProvisioningUnavailable("服务器注册接口返回 HTTP 404：该路由尚未部署，或 KT_MCP_PROVISION_URL 不正确。")
        if status in {401, 403}:
            _close_response(response)
            raise ProvisioningError("服务器拒绝注册凭据（HTTP 401/403）；请检查 token 与设备授权。")
        if status is not None and status >= 400:
            _close_response(response)
            raise ProvisioningError(f"服务器注册请求失败（HTTP {status}）；未保存注册结果。")
        raw = _read_response(response)
    except ProvisioningError:
        raise
    except HTTPError as exc:
        if exc.code == 404:
            raise ProvisioningUnavailable("服务器注册接口返回 HTTP 404：该路由尚未部署，或 KT_MCP_PROVISION_URL 不正确。") from exc
        if exc.code in {401, 403}:
            raise ProvisioningError("服务器拒绝注册凭据（HTTP 401/403）；请检查 token 与设备授权。") from exc
        raise ProvisioningError(f"服务器注册请求失败（HTTP {exc.code}）；未保存注册结果。") from exc
    except TimeoutError as exc:
        raise ProvisioningUnavailable("服务器注册请求超时；无法确认服务器是否已接受请求，请先核对服务端状态。") from exc
    except (URLError, ConnectionError, OSError, ValueError, TypeError) as exc:
        raise ProvisioningUnavailable("无法连接服务器注册接口；请检查网络、TLS 证书和服务器地址，注册结果尚未确认。") from exc
    except Exception as exc:
        raise ProvisioningUnavailable("服务器注册请求异常；注册结果尚未确认，请核对服务端状态。") from exc

    payload = _response_payload(raw)
    candidates: list[str] = []
    for key in ("url", "endpoint_url"):
        value = payload.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise ProvisioningError("provisioning API 返回的 MCP 地址无效")
        candidates.append(value)
    if not candidates:
        raise ProvisioningError("provisioning API 未返回 MCP 地址")
    returned_urls: list[str] = []
    for candidate in candidates:
        validated = canonicalize_endpoint_url(candidate, canonical_origin)
        if validated != candidate:
            raise ProvisioningError("provisioning 返回的 MCP URL 格式不一致；未保存配置")
        returned_urls.append(candidate)
    if any(value != canonical_endpoint for value in returned_urls) or len(set(returned_urls)) != 1:
        raise ProvisioningError("provisioning 返回的 MCP URL 与本机 KT endpoint 不一致；未保存配置")
    return ProvisionedEndpoint(
        url=canonical_endpoint,
        remote_port=_parse_remote_port(payload),
    )


def request_independent_endpoint(
    origin: str,
    endpoint_url: str,
    *,
    profile: DeviceProfile,
    config_path: str | os.PathLike[str] | None = None,
    config: str | os.PathLike[str] | None = None,
    opener: URLOpener | None = None,
    timeout: float = PROVISION_TIMEOUT,
) -> str:
    """Register the exact endpoint returned by the current local KT runtime."""
    if config_path is None:
        config_path = config
    return _request_provisioned_endpoint(
        origin, endpoint_url, profile=profile, config_path=config_path, opener=opener, timeout=timeout
    ).url


def provision_and_save(
    origin: str,
    endpoint_url: str,
    config_path: str | os.PathLike[str] | None = None,
    *,
    profile: DeviceProfile,
    config: str | os.PathLike[str] | None = None,
    opener: URLOpener | None = None,
    timeout: float = PROVISION_TIMEOUT,
) -> str:
    """Register and save the same current in-memory profile object."""

    if config_path is None:
        config_path = config
    provisioned = _request_provisioned_endpoint(
        origin, endpoint_url, profile=profile, config_path=config_path, opener=opener, timeout=timeout
    )
    profile.remote_forward_port = provisioned.remote_port
    # Never load config_path here: persisted secrets and markers cannot prove
    # that this endpoint came from the current local KT url command.
    save(profile, config_path)
    return endpoint_url


# Short aliases keep the module convenient for callers while retaining one
# implementation and one validation path.
provision_endpoint = request_independent_endpoint
request_endpoint = request_independent_endpoint


__all__ = [
    "DEFAULT_ORIGIN",
    "EXPECTED_PARITY_TOOLS",
    "INSTALLATION_ID_FILENAME",
    "KT_MCP_PROVISION_URL",
    "KT_MCP_PROVISION_TOKEN",
    "KT_MCP_PROVISION_ORIGIN_MAP",
    "MAX_RESPONSE_BYTES",
    "PRODUCTION_CONNECTION_PATH",
    "PROVISION_TIMEOUT",
    "PROVISIONING_NOT_CONFIGURED_MESSAGE",
    "ProvisionedEndpoint",
    "PublicVerification",
    "ProvisioningError",
    "ProvisioningUnavailable",
    "canonicalize_endpoint_url",
    "check_provisioning_configuration",
    "discover_default_origin",
    "discover_origin",
    "discover_production_origin",
    "get_installation_id",
    "installation_id_path",
    "normalize_origin",
    "provision_and_save",
    "provision_endpoint",
    "provisioning_url",
    "request_endpoint",
    "request_independent_endpoint",
    "verify_public_endpoint",
]
