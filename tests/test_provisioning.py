"""Provisioning client regression tests."""

import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.request import Request

import httpx
import pytest

from kt_mcp_client import provisioning
from tests.helpers import set_test_endpoint
from kt_mcp_client.config import DeviceProfile, load, save
from kt_mcp_client.provisioning import (
    DEFAULT_ORIGIN,
    KT_MCP_PROVISION_TOKEN,
    MAX_RESPONSE_BYTES,
    PROVISIONING_NOT_CONFIGURED_MESSAGE,
    PublicVerification,
    PublicVerificationCancelled,
    ProvisioningError,
    ProvisioningUnavailable,
    discover_default_origin,
    discover_production_origin,
    get_installation_id,
    installation_id_path,
    provision_and_save,
    request_independent_endpoint,
    verify_public_endpoint,
    wait_for_public_endpoint,
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
    # These regressions exercise an already configured legacy device. Fresh
    # shared-key device coverage belongs to test_shared_key_enrollment.py.
    tmp_path.mkdir(parents=True, exist_ok=True)
    identity, known = tmp_path / "legacy-test-key", tmp_path / "legacy-known-hosts"
    identity.write_text("synthetic legacy key", encoding="ascii")
    known.write_text("synthetic legacy pinned host", encoding="ascii")
    profile = DeviceProfile(
        ssh_host="legacy.example.invalid", ssh_user="kt-mcp-device-01",
        ssh_identity_file=str(identity), ssh_known_hosts=str(known),
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


class FakeClock:
    """A monotonic clock whose waits consume no wall time."""

    def __init__(self) -> None:
        self.now = 0.0
        self.waits: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        assert 0 < seconds <= 0.1
        self.waits.append(seconds)
        self.now += seconds


@pytest.fixture
def readiness_clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    clock = FakeClock()
    # Replace only provisioning's reference, not the shared stdlib module
    # used by pytest, httpx, or other test workers.
    monkeypatch.setattr(provisioning, "time", clock)
    return clock


@pytest.fixture
def public_profile() -> DeviceProfile:
    return DeviceProfile(
        public_origin="https://device.example.invalid", workspace_id="synthetic-workspace"
    )


PUBLIC_EVIDENCE = PublicVerification(
    "2025-03-26", tuple(sorted(provisioning.EXPECTED_PARITY_TOOLS)), "tree"
)


def public_http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", ENDPOINT)
    return httpx.HTTPStatusError(
        f"HTTP error at {ENDPOINT}", request=request,
        response=httpx.Response(status, request=request, text=f"sensitive body {TOKEN}"),
    )


def install_public_transport(monkeypatch: pytest.MonkeyPatch, handler):
    """Use real httpx response/cleanup semantics, but never open a socket."""

    original_client = httpx.Client
    clients: list[httpx.Client] = []

    def make_client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["http2"] is False
        assert 0 < kwargs["timeout"] <= 15.0
        client = original_client(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(provisioning, "httpx", SimpleNamespace(
        **{name: getattr(httpx, name) for name in dir(httpx) if not name.startswith("_")},
    ))
    monkeypatch.setattr(provisioning.httpx, "Client", make_client)
    return clients


def successful_public_response(request: httpx.Request, *, sse: bool = False) -> httpx.Response:
    body = json.loads(request.content)
    method = body["method"]
    headers: dict[str, str] = {}
    if method == "notifications/initialized":
        return httpx.Response(202)
    if method == "initialize":
        result = {"protocolVersion": "2025-03-26"}
        headers["mcp-session-id"] = "synthetic-session"
    elif method == "tools/list":
        result = {"tools": [{"name": name} for name in PUBLIC_EVIDENCE.tools]}
    else:
        assert method == "tools/call"
        result = {"content": [{"type": "text", "text": "synthetic tree"}], "isError": False}
    payload = {"jsonrpc": "2.0", "id": body["id"], "result": result}
    if sse:
        headers["content-type"] = "text/event-stream"
        return httpx.Response(200, headers=headers, text=f"event: message\ndata: {json.dumps(payload)}\n\n")
    return httpx.Response(200, headers=headers, json=payload)


@pytest.mark.parametrize("sse", [False, True])
def test_public_verification_requires_complete_mcp_exchange(
    monkeypatch, readiness_clock, public_profile, sse,
):
    requests: list[httpx.Request] = []
    timeouts: list[float] = []

    def handler(request):
        requests.append(request)
        timeouts.append(request.extensions["timeout"]["read"])
        readiness_clock.now += 0.5
        return successful_public_response(request, sse=sse)

    clients = install_public_transport(monkeypatch, handler)
    assert verify_public_endpoint(ENDPOINT, public_profile, timeout=4.0) == PUBLIC_EVIDENCE
    assert [json.loads(request.content)["method"] for request in requests] == [
        "initialize", "notifications/initialized", "tools/list", "tools/call"
    ]
    assert timeouts == pytest.approx([4.0, 3.5, 3.0, 2.5])
    for request in requests[1:]:
        assert request.headers["Mcp-Session-Id"] == "synthetic-session"
        assert request.headers["MCP-Protocol-Version"] == "2025-03-26"
        assert str(request.url) == ENDPOINT
    assert json.loads(requests[-1].content)["params"] == {
        "name": "tree", "arguments": {"workspace_id": "synthetic-workspace", "path": "."},
    }
    assert len(clients) == 1 and clients[0].is_closed


@pytest.mark.parametrize("status", [401, 403, 404, 408, 500, 502, 503, 504])
def test_public_http_failure_reports_safe_status_and_keeps_cause(
    monkeypatch, readiness_clock, public_profile, status,
):
    clients = install_public_transport(
        monkeypatch, lambda request: httpx.Response(status, text=f"secret: {ENDPOINT}")
    )
    with pytest.raises(ProvisioningUnavailable, match=f"HTTP {status}") as error:
        verify_public_endpoint(ENDPOINT, public_profile)
    assert isinstance(error.value.__cause__, httpx.HTTPStatusError)
    assert ENDPOINT not in str(error.value)
    assert TOKEN not in str(error.value)
    assert clients[0].is_closed


@pytest.mark.parametrize("error_type", [
    httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
    httpx.RemoteProtocolError, ValueError, TypeError, OSError,
])
def test_public_failure_reports_safe_error_type_and_keeps_cause(
    monkeypatch, readiness_clock, public_profile, error_type,
):
    failure = error_type(f"failure at {ENDPOINT}")

    def handler(request):
        raise failure

    clients = install_public_transport(monkeypatch, handler)
    with pytest.raises(ProvisioningUnavailable, match=error_type.__name__) as error:
        verify_public_endpoint(ENDPOINT, public_profile)
    assert error.value.__cause__ is failure
    assert TOKEN not in str(error.value)
    assert ENDPOINT not in str(error.value)
    assert clients[0].is_closed


def test_public_verification_deadline_is_shared_by_all_requests(
    monkeypatch, readiness_clock, public_profile,
):
    methods: list[str] = []
    timeouts: list[float] = []

    def handler(request):
        methods.append(json.loads(request.content)["method"])
        remaining = request.extensions["timeout"]["read"]
        timeouts.append(remaining)
        readiness_clock.now += min(0.75, remaining)
        return successful_public_response(request)

    clients = install_public_transport(monkeypatch, handler)
    with pytest.raises(ProvisioningUnavailable, match="ReadTimeout") as error:
        verify_public_endpoint(ENDPOINT, public_profile, timeout=2.0)
    assert methods == ["initialize", "notifications/initialized", "tools/list"]
    assert timeouts == pytest.approx([2.0, 1.25, 0.5])
    assert readiness_clock.now == 2.0
    assert isinstance(error.value.__cause__, httpx.ReadTimeout)
    assert clients[0].is_closed


def test_public_verification_rejects_late_final_success(
    monkeypatch, readiness_clock, public_profile,
):
    methods: list[str] = []

    def handler(request):
        methods.append(json.loads(request.content)["method"])
        readiness_clock.now += request.extensions["timeout"]["read"] if len(methods) == 4 else 0.1
        return successful_public_response(request)

    clients = install_public_transport(monkeypatch, handler)
    with pytest.raises(ProvisioningUnavailable, match="ReadTimeout"):
        verify_public_endpoint(ENDPOINT, public_profile, timeout=1.0)
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert readiness_clock.now == pytest.approx(1.0)
    assert clients[0].is_closed


@pytest.mark.parametrize("case", ["auth", "payload"])
def test_late_terminal_failure_is_not_reclassified_as_retryable_timeout(
    monkeypatch, readiness_clock, public_profile, case,
):
    attempts: list[float] = []

    def handler(request):
        attempts.append(request.extensions["timeout"]["read"])
        # Expire the 8-second attempt, but leave total readiness time. The
        # terminal response must not be masked by a transport-like deadline.
        readiness_clock.now += request.extensions["timeout"]["read"] + 0.1
        if case == "auth":
            return httpx.Response(403, text=ENDPOINT)
        return httpx.Response(200, headers={"content-type": "application/json"}, text=ENDPOINT)

    clients = install_public_transport(monkeypatch, handler)
    with pytest.raises(ProvisioningError, match="HTTP 403" if case == "auth" else "无效 JSON"):
        wait_for_public_endpoint(
            ENDPOINT, public_profile,
            on_retry=lambda *args: pytest.fail("late terminal response must not retry"),
        )
    assert attempts == [8.0]
    assert readiness_clock.waits == []
    assert len(clients) == 1 and clients[0].is_closed


@pytest.mark.parametrize("case,expected_calls", [
    ("invalid_json", 1), ("invalid_sse", 1), ("non_object", 1),
    ("initialize_error", 1), ("initialize_no_result", 1),
    ("initialize_no_protocol", 1), ("initialize_bad_protocol", 1),
    ("list_error", 3), ("list_no_tools", 3), ("list_missing_tools", 3),
    ("list_malformed_tool", 3), ("call_error", 4), ("call_is_error", 4),
    ("call_no_result", 4), ("call_empty_result", 4), ("call_bad_error_flag", 4),
])
def test_public_payload_and_tool_failures_never_retry(
    monkeypatch, readiness_clock, public_profile, case, expected_calls,
):
    requests: list[httpx.Request] = []
    retries: list[object] = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        method = body["method"]
        response = successful_public_response(request)
        if method == "initialize":
            if case == "invalid_json":
                return httpx.Response(200, headers={"content-type": "application/json"}, text=ENDPOINT)
            if case == "invalid_sse":
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=f"data: {ENDPOINT}\n")
            if case == "non_object":
                return httpx.Response(200, json=[ENDPOINT])
            if case == "initialize_error":
                return httpx.Response(200, json={"error": {"message": ENDPOINT}, "result": {"protocolVersion": "2025-03-26"}})
            if case == "initialize_no_result":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1})
            if case == "initialize_no_protocol":
                return httpx.Response(200, json={"result": {}})
            if case == "initialize_bad_protocol":
                return httpx.Response(200, json={"result": {"protocolVersion": {"secret": ENDPOINT}}})
        elif method == "tools/list":
            if case == "list_error":
                return httpx.Response(200, json={"error": {"message": ENDPOINT}})
            if case == "list_no_tools":
                return httpx.Response(200, json={"result": {}})
            if case == "list_missing_tools":
                return httpx.Response(200, json={"result": {"tools": [{"name": "tree"}]}})
            if case == "list_malformed_tool":
                payload = response.json()
                payload["result"]["tools"].append({"name": 123})
                return httpx.Response(200, json=payload)
        elif method == "tools/call":
            if case == "call_error":
                return httpx.Response(200, json={"error": {"message": ENDPOINT}})
            if case == "call_is_error":
                return httpx.Response(200, json={"result": {"isError": True, "content": [{"text": ENDPOINT}]}})
            if case == "call_no_result":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 3})
            if case == "call_empty_result":
                return httpx.Response(200, json={"result": {}})
            if case == "call_bad_error_flag":
                return httpx.Response(200, json={"result": {"content": [], "isError": ENDPOINT}})
        return response

    clients = install_public_transport(monkeypatch, handler)
    with pytest.raises(ProvisioningError) as error:
        wait_for_public_endpoint(ENDPOINT, public_profile, on_retry=lambda *args: retries.append(args))
    assert not isinstance(error.value, ProvisioningUnavailable)
    assert ENDPOINT not in str(error.value)
    assert TOKEN not in str(error.value)
    assert retries == [] and readiness_clock.waits == []
    assert len(requests) == expected_calls
    assert len(clients) == 1 and clients[0].is_closed


@pytest.mark.parametrize("content", [
    [None], [ENDPOINT], [{}], [{"type": "text"}],
    [{"type": "text", "text": None}], [{"type": "text", "text": 123}],
    [{"type": "unknown", "text": ENDPOINT}],
    [{"type": "text", "text": "valid"}, None],
])
def test_public_malformed_readonly_content_is_terminal(
    monkeypatch, readiness_clock, public_profile, content,
):
    methods: list[str] = []

    def handler(request):
        methods.append(json.loads(request.content)["method"])
        if methods[-1] == "tools/call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 3, "result": {
                "content": content, "isError": False,
            }})
        return successful_public_response(request)

    clients = install_public_transport(monkeypatch, handler)
    with pytest.raises(ProvisioningError, match="无效文本内容") as error:
        wait_for_public_endpoint(
            ENDPOINT, public_profile,
            on_retry=lambda *args: pytest.fail("malformed content must never retry"),
        )
    assert not isinstance(error.value, ProvisioningUnavailable)
    assert ENDPOINT not in str(error.value) and TOKEN not in str(error.value)
    assert methods == ["initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert readiness_clock.waits == []
    assert len(clients) == 1 and clients[0].is_closed


@pytest.mark.parametrize("status", [408, 502, 503, 504])
def test_wait_retries_transient_gateway_until_full_verification(
    monkeypatch, readiness_clock, public_profile, status,
):
    methods: list[str] = []
    retries: list[tuple[int, str]] = []

    def handler(request):
        method = json.loads(request.content)["method"]
        methods.append(method)
        if len(methods) == 1:
            return httpx.Response(status, text=ENDPOINT)
        return successful_public_response(request)

    clients = install_public_transport(monkeypatch, handler)
    monkeypatch.setattr(provisioning, "provision_and_save", lambda *args, **kwargs: pytest.fail("registration must never repeat"))
    assert wait_for_public_endpoint(
        ENDPOINT, public_profile, on_retry=lambda *args: retries.append(args)
    ) == PUBLIC_EVIDENCE
    assert methods == ["initialize", "initialize", "notifications/initialized", "tools/list", "tools/call"]
    assert retries == [(1, f"HTTP {status}")]
    assert readiness_clock.now == pytest.approx(1.0)
    assert len(clients) == 2 and all(client.is_closed for client in clients)


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout])
def test_wait_retries_transport_error_and_resolves_default_at_call_time(
    monkeypatch, readiness_clock, public_profile, wrapped, error_type,
):
    attempts: list[tuple[str, DeviceProfile, float]] = []
    retries: list[tuple[int, str]] = []
    failure = error_type(f"failed at {ENDPOINT}")

    def verify(endpoint, profile, *, timeout):
        attempts.append((endpoint, profile, timeout))
        if len(attempts) < 3:
            if wrapped:
                raise ProvisioningUnavailable(f"unsafe wrapper {ENDPOINT}") from failure
            raise failure
        return PUBLIC_EVIDENCE

    monkeypatch.setattr(provisioning, "verify_public_endpoint", verify)
    assert wait_for_public_endpoint(
        ENDPOINT, public_profile, on_retry=lambda *args: retries.append(args)
    ) is PUBLIC_EVIDENCE
    assert [(endpoint, profile) for endpoint, profile, timeout in attempts] == [(ENDPOINT, public_profile)] * 3
    assert [timeout for endpoint, profile, timeout in attempts] == [8.0, 8.0, 8.0]
    assert retries == [(1, error_type.__name__), (2, error_type.__name__)]
    assert readiness_clock.now == pytest.approx(3.0)
    assert all(TOKEN not in reason and ENDPOINT not in reason for attempt, reason in retries)


def test_wait_final_deadline_sanitizes_wrapped_transient_error(readiness_clock, public_profile):
    failure = public_http_error(503)
    failures: list[ProvisioningUnavailable] = []

    def verify(endpoint, profile, *, timeout):
        wrapper = ProvisioningUnavailable(f"unsafe wrapper {ENDPOINT}")
        failures.append(wrapper)
        raise wrapper from failure

    with pytest.raises(ProvisioningUnavailable, match="超时.*HTTP 503") as error:
        wait_for_public_endpoint(ENDPOINT, public_profile, timeout=0.25, verify=verify)
    assert len(failures) == 1
    assert error.value.__cause__ is failures[0]
    assert failures[0].__cause__ is failure
    assert ENDPOINT not in str(error.value) and TOKEN not in str(error.value)


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404, 405, 429, 500, 501])
@pytest.mark.parametrize("wrapped", [False, True])
def test_wait_does_not_retry_non_gateway_http_errors(
    readiness_clock, public_profile, status, wrapped,
):
    attempts: list[float] = []
    failure = public_http_error(status)

    def verify(endpoint, profile, *, timeout):
        attempts.append(timeout)
        if wrapped:
            raise ProvisioningUnavailable(f"HTTP {status}") from failure
        raise failure

    with pytest.raises(ProvisioningUnavailable, match=f"HTTP {status}") as error:
        wait_for_public_endpoint(
            ENDPOINT, public_profile, verify=verify,
            on_retry=lambda *args: pytest.fail("non-gateway errors must not retry"),
        )
    assert attempts == [8.0] and readiness_clock.waits == []
    assert ENDPOINT not in str(error.value) and TOKEN not in str(error.value)
    assert error.value.__cause__ is failure


@pytest.mark.parametrize("failure", [
    ProvisioningError("missing tool"), ProvisioningUnavailable("unclassified failure"),
    ValueError(ENDPOINT), TypeError(ENDPOINT), OSError(ENDPOINT), TimeoutError(ENDPOINT),
])
def test_wait_fails_closed_for_non_httpx_errors(readiness_clock, public_profile, failure):
    attempts: list[float] = []

    def verify(endpoint, profile, *, timeout):
        attempts.append(timeout)
        raise failure

    with pytest.raises(ProvisioningError) as error:
        wait_for_public_endpoint(
            ENDPOINT, public_profile, verify=verify,
            on_retry=lambda *args: pytest.fail("unclassified errors must not retry"),
        )
    assert attempts == [8.0] and readiness_clock.waits == []
    if isinstance(failure, ProvisioningError):
        assert error.value is failure
    else:
        assert error.value.__cause__ is failure
        assert ENDPOINT not in str(error.value)


def test_wait_does_not_retry_validation_error_even_with_transport_cause(readiness_clock, public_profile):
    failure = httpx.ReadTimeout(ENDPOINT)

    def verify(endpoint, profile, *, timeout):
        raise ProvisioningError("malformed result") from failure

    with pytest.raises(ProvisioningError, match="malformed result"):
        wait_for_public_endpoint(
            ENDPOINT, public_profile, verify=verify,
            on_retry=lambda *args: pytest.fail("validation error must not retry"),
        )
    assert readiness_clock.waits == []


def test_wait_deadline_caps_attempts_and_backoff(readiness_clock, public_profile):
    attempts: list[tuple[float, float]] = []
    retries: list[tuple[int, str]] = []
    failure = httpx.ConnectTimeout(ENDPOINT)

    def verify(endpoint, profile, *, timeout):
        attempts.append((readiness_clock.now, timeout))
        readiness_clock.now += min(0.25, timeout)
        raise failure

    with pytest.raises(ProvisioningUnavailable, match="超时.*ConnectTimeout") as error:
        wait_for_public_endpoint(
            ENDPOINT, public_profile, timeout=11.0, verify=verify,
            on_retry=lambda *args: retries.append(args),
        )
    assert [start for start, timeout in attempts] == pytest.approx([0, 1.25, 3.5, 6.75, 10.0])
    assert [timeout for start, timeout in attempts] == pytest.approx([8, 8, 7.5, 4.25, 1.0])
    assert retries == [(index, "ConnectTimeout") for index in range(1, 6)]
    assert readiness_clock.now == pytest.approx(11.0)
    assert all(0 < interval <= 0.1 for interval in readiness_clock.waits)
    assert error.value.__cause__ is failure
    assert TOKEN not in str(error.value) and ENDPOINT not in str(error.value)


def test_wait_shrinks_last_attempt_to_remaining_total_budget(readiness_clock, public_profile):
    attempts: list[float] = []
    failure = httpx.ConnectError(ENDPOINT)

    def verify(endpoint, profile, *, timeout):
        attempts.append(timeout)
        if len(attempts) == 1:
            raise failure
        readiness_clock.now += timeout
        raise httpx.ReadTimeout(ENDPOINT)

    with pytest.raises(ProvisioningUnavailable, match="超时"):
        wait_for_public_endpoint(ENDPOINT, public_profile, timeout=1.25, verify=verify)
    assert attempts == pytest.approx([1.25, 0.25])
    assert readiness_clock.now == pytest.approx(1.25)


def test_wait_deadline_still_terminates_with_frozen_mock_clock(monkeypatch, public_profile):
    attempts: list[float] = []
    waits: list[float] = []
    failure = httpx.ConnectError(ENDPOINT)
    monkeypatch.setattr(provisioning, "time", SimpleNamespace(
        monotonic=lambda: 0.0, sleep=lambda interval: waits.append(interval),
    ))

    def verify(endpoint, profile, *, timeout):
        attempts.append(timeout)
        assert len(attempts) <= 20, "frozen clocks must not cause infinite retries"
        raise failure

    with pytest.raises(ProvisioningUnavailable, match="超时"):
        wait_for_public_endpoint(ENDPOINT, public_profile, timeout=45.0, verify=verify)
    assert 1 < len(attempts) <= 20
    assert sum(waits) == pytest.approx(45.0)
    assert max(waits) <= 0.1


def test_wait_never_accepts_success_after_deadline(readiness_clock, public_profile):
    def verify(endpoint, profile, *, timeout):
        assert timeout == 0.5
        readiness_clock.now += 0.6
        return PUBLIC_EVIDENCE

    with pytest.raises(ProvisioningUnavailable, match="超时"):
        wait_for_public_endpoint(ENDPOINT, public_profile, timeout=0.5, verify=verify)
    assert readiness_clock.waits == []


def test_wait_cancellation_before_attempt_has_no_side_effects(readiness_clock, public_profile):
    with pytest.raises(PublicVerificationCancelled, match="取消"):
        wait_for_public_endpoint(
            ENDPOINT, public_profile, cancelled=lambda: True,
            verify=lambda *args, **kwargs: pytest.fail("cancelled before first attempt"),
        )
    assert readiness_clock.waits == []
    assert issubclass(PublicVerificationCancelled, ProvisioningError)


def test_wait_cancellation_during_backoff_uses_event_callable(
    monkeypatch, readiness_clock, public_profile,
):
    stop = Event()
    attempts: list[float] = []
    original_sleep = readiness_clock.sleep

    def sleep(interval):
        original_sleep(interval)
        stop.set()

    monkeypatch.setattr(readiness_clock, "sleep", sleep)

    def verify(endpoint, profile, *, timeout):
        attempts.append(timeout)
        raise httpx.ConnectError(ENDPOINT)

    with pytest.raises(PublicVerificationCancelled, match="取消"):
        wait_for_public_endpoint(ENDPOINT, public_profile, cancelled=stop.is_set, verify=verify)
    assert attempts == [8.0]
    assert readiness_clock.waits == [0.1]


@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_wait_cancellation_during_attempt_never_returns_success(
    readiness_clock, public_profile, outcome,
):
    stop = Event()

    def verify(endpoint, profile, *, timeout):
        stop.set()
        if outcome == "failure":
            raise httpx.ConnectTimeout(ENDPOINT)
        return PUBLIC_EVIDENCE

    with pytest.raises(PublicVerificationCancelled, match="取消"):
        wait_for_public_endpoint(
            ENDPOINT, public_profile, verify=verify, cancelled=stop.is_set,
            on_retry=lambda *args: pytest.fail("cancelled attempt must not retry"),
        )
    assert readiness_clock.waits == []


def test_wait_cancellation_from_retry_callback_skips_wait(readiness_clock, public_profile):
    stop = Event()

    def verify(endpoint, profile, *, timeout):
        raise httpx.ConnectError(ENDPOINT)

    with pytest.raises(PublicVerificationCancelled):
        wait_for_public_endpoint(
            ENDPOINT, public_profile, verify=verify, cancelled=stop.is_set,
            on_retry=lambda attempt, reason: stop.set(),
        )
    assert readiness_clock.waits == []


@pytest.mark.parametrize("timeout", [float("inf"), float("-inf"), float("nan"), -1.0, "invalid"])
@pytest.mark.parametrize("function", [verify_public_endpoint, wait_for_public_endpoint])
def test_public_verification_rejects_invalid_budget_before_network(
    monkeypatch, readiness_clock, public_profile, timeout, function,
):
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: pytest.fail("invalid timeout must not make requests"))
    with pytest.raises(ProvisioningError, match="timeout"):
        function(ENDPOINT, public_profile, timeout=timeout)
    assert readiness_clock.waits == []


@pytest.mark.parametrize("function", [verify_public_endpoint, wait_for_public_endpoint])
def test_public_verification_zero_budget_never_attempts_network(
    monkeypatch, readiness_clock, public_profile, function,
):
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: pytest.fail("expired budget must not make requests"))
    with pytest.raises(ProvisioningUnavailable):
        function(ENDPOINT, public_profile, timeout=0)
    assert readiness_clock.waits == []
