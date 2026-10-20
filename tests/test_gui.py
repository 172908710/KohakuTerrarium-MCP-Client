from __future__ import annotations

import time
import queue
import threading
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def registration_environment(monkeypatch):
    monkeypatch.setenv("KT_MCP_PROVISION_TOKEN", "synthetic-test-token")
    monkeypatch.setenv("KT_MCP_PROVISION_URL", "https://provision.example.invalid/api")
    monkeypatch.setenv("KT_MCP_PROVISION_ORIGIN_MAP", '{"https://device.example.invalid": ["provision.example.invalid"]}')

from kt_mcp_client import gui
from kt_mcp_client.provisioning import ProvisioningUnavailable


class FakeVar:
    def __init__(self, value: str = "") -> None:
        self.value = value
        self.owner_thread = threading.get_ident()

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        assert threading.get_ident() == self.owner_thread, "Tk variable touched from worker"
        self.value = value


class FakeButton:
    def __init__(self) -> None:
        self.states: list[str] = []

    def configure(self, *, state: str) -> None:
        self.states.append(state)


class FakeProgress:
    def __init__(self):
        self.running = False

    def start(self, interval):
        self.running = True

    def stop(self):
        self.running = False


class FakeRoot:
    def after(self, delay: int, callback) -> None:
        # Real Tk runs this callback on the main thread. The fake invokes it
        # immediately; tests still join the worker before making assertions.
        callback()


class QueuedRoot:
    """Headless root whose ``after`` callbacks remain pending until drained."""

    def __init__(self) -> None:
        self.callbacks = []

    def after(self, delay: int, callback) -> None:
        self.callbacks.append(callback)

    def drain(self) -> None:
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback()


def _gui_without_tk() -> gui.ClientGUI:
    value = object.__new__(gui.ClientGUI)
    value.root = QueuedRoot()
    value.config_path = None
    value.origin_var = FakeVar("https://device.example.invalid")
    value.provision_key_var = FakeVar()
    value.output_var = FakeVar()
    value.status_var = FakeVar("状态：等待请求")
    value.request_button = FakeButton()
    value.origin_entry = FakeButton()
    value.provision_key_entry = FakeButton()
    value.progress_bar = FakeProgress()
    value.elapsed_var = FakeVar()
    value._events = queue.SimpleQueue()
    value._started_at = value._stage_at = 0.0
    value._stage = ""
    value._poll_id = None
    value._closing = False
    value._worker = None
    return value


def _wait_for_worker(value: gui.ClientGUI) -> None:
    for _ in range(200):
        worker = value._worker
        if worker is None:
            return
        worker.join(timeout=0.01)
        value.root.drain()
        time.sleep(0.001)
    raise AssertionError("GUI worker did not finish")


def test_button_uses_worker_and_runtime_url_before_provision(monkeypatch, tmp_path):
    value = _gui_without_tk()
    value.config_path = str(tmp_path)
    endpoint = "https://device.example.invalid/mcp/" + ("Q" * 43)
    calls: list[tuple[str, object]] = []
    runtime_calls: list[str] = []

    # The test bypasses filesystem profile validation but preserves the required
    # ordering: setup -> start -> url -> provisioning.
    class FakeRuntime:
        def __init__(self, *args, **kwargs):
            pass

        def setup(self):
            runtime_calls.append("setup")

        def start(self, *, wait_for_public=True):
            assert wait_for_public is False
            runtime_calls.append("start")
            return type("Status", (), {"cli_status": "ready", "local_ready": True, "detail": ""})()

        def url(self):
            runtime_calls.append("url")
            return endpoint

    monkeypatch.setattr(gui, "config_file", lambda path: Path(path) / "profile.json")
    identity = tmp_path / "id"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("key", encoding="utf-8")
    known_hosts.write_text("vps.example.invalid ssh-ed25519 AAAA", encoding="utf-8")
    monkeypatch.setattr(gui, "load", lambda path: type("Profile", (), {
        "kt_python": "python", "workspace_path": "workspace", "kt_home": "home",
        "public_origin": "https://device.example.invalid",
        "workspace_id": "default", "remote_forward_host": "127.0.0.1",
        "remote_forward_port": 18766, "local_port": 8865,
        "ssh_host": "vps.example.invalid", "ssh_user": "kt",
        "ssh_identity_file": str(identity), "ssh_known_hosts": str(known_hosts),
        "ssh_port": 2222, "ssh_extra_options": [], "secret": "",
    })())
    monkeypatch.setattr(gui, "KTRuntime", FakeRuntime)

    def provision(origin: str, endpoint_url: str, config_path: str | Path | None, *, profile):
        calls.append((origin, endpoint_url, config_path, profile))
        return endpoint

    monkeypatch.setattr(gui, "provision_and_save", provision)
    monkeypatch.setattr(gui, "validate_ssh_profile", lambda profile: None)
    class FakeTunnelProcess:
        running = True
        identity_ok = True
    class FakeTunnelStatus:
        configured = True
        process = FakeTunnelProcess()
        detail = "ready"
    class FakeTunnel:
        def __init__(self, *args, **kwargs): pass
        def start(self): return FakeTunnelStatus()
    monkeypatch.setattr(gui, "SSHTunnel", FakeTunnel)
    monkeypatch.setattr(gui, "verify_public_endpoint", lambda endpoint, profile: None)
    Path(value.config_path, "profile.json").write_text("{}", encoding="utf-8")
    value.request_endpoint()
    _wait_for_worker(value)

    assert runtime_calls == ["setup", "start", "url"]
    assert calls == [("https://device.example.invalid", endpoint, str(tmp_path), calls[0][3])]
    assert value.output_var.get() == endpoint
    assert "公网 MCP 已完成" in value.status_var.get()
    assert value.request_button.states == ["disabled", "normal"]


def test_missing_runtime_configuration_is_clear_and_does_not_provision(monkeypatch, tmp_path):
    value = _gui_without_tk()
    value.config_path = str(tmp_path)
    Path(value.config_path, "profile.json").write_text("{}", encoding="utf-8")
    provisioned = False

    monkeypatch.setattr(gui, "config_file", lambda path: Path(path) / "profile.json")
    monkeypatch.setattr(gui, "load", lambda path: type("Profile", (), {
        "kt_python": "", "workspace_path": "", "kt_home": "",
    })())

    def provision(*args, **kwargs):
        nonlocal provisioned
        provisioned = True
        return "should-not-be-used"

    monkeypatch.setattr(gui, "provision_and_save", provision)
    monkeypatch.setattr(gui, "validate_ssh_profile", lambda profile: None)
    class FakeTunnelProcess:
        running = True
        identity_ok = True
    class FakeTunnelStatus:
        configured = True
        process = FakeTunnelProcess()
        detail = "ready"
    class FakeTunnel:
        def __init__(self, *args, **kwargs): pass
        def start(self): return FakeTunnelStatus()
    monkeypatch.setattr(gui, "SSHTunnel", FakeTunnel)
    monkeypatch.setattr(gui, "verify_public_endpoint", lambda endpoint, profile: None)
    value.request_endpoint()
    _wait_for_worker(value)

    assert not provisioned
    assert "runtime 配置缺失" in value.status_var.get()
    assert "kt_python" in value.status_var.get()
    assert value.output_var.get() == ""
    assert value.request_button.states == ["disabled", "normal"]


def test_button_does_not_fake_url_when_api_is_unavailable(monkeypatch, tmp_path):
    value = _gui_without_tk()
    value.config_path = str(tmp_path)
    value.output_var.set("old-url")
    Path(value.config_path, "profile.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(gui, "config_file", lambda path: Path(path) / "profile.json")
    identity = tmp_path / "id"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("key", encoding="utf-8")
    known_hosts.write_text("vps.example.invalid ssh-ed25519 AAAA", encoding="utf-8")
    monkeypatch.setattr(gui, "load", lambda path: type("Profile", (), {
        "kt_python": "python", "workspace_path": "workspace", "kt_home": "home",
        "public_origin": "https://device.example.invalid",
        "workspace_id": "default", "remote_forward_host": "127.0.0.1",
        "remote_forward_port": 18766, "local_port": 8865,
        "ssh_host": "vps.example.invalid", "ssh_user": "kt",
        "ssh_identity_file": str(identity), "ssh_known_hosts": str(known_hosts),
        "ssh_port": 2222, "ssh_extra_options": [], "secret": "",
    })())
    monkeypatch.setattr(gui, "KTRuntime", lambda *args, **kwargs: type("Runtime", (), {
        "setup": lambda self: None,
        "start": lambda self, **kwargs: type("Status", (), {"cli_status": "ready", "local_ready": True, "detail": ""})(),
        "url": lambda self: "https://device.example.invalid/mcp/" + ("Q" * 43),
    })())

    def unavailable(*args, **kwargs):
        raise ProvisioningUnavailable("VPS/服务器端 provisioning API 未配置")

    monkeypatch.setattr(gui, "provision_and_save", unavailable)
    value.request_endpoint()
    _wait_for_worker(value)

    assert value.output_var.get() == ""
    assert "VPS/服务器端 provisioning API 未配置" in value.status_var.get()
    assert value.request_button.states == ["disabled", "normal"]


def test_button_does_not_accept_terminal_status_with_local_ready(monkeypatch, tmp_path):
    value = _gui_without_tk()
    value.config_path = str(tmp_path)
    Path(value.config_path, "profile.json").write_text("{}", encoding="utf-8")
    provisioned = False

    monkeypatch.setattr(gui, "config_file", lambda path: Path(path) / "profile.json")
    identity = tmp_path / "id"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("key", encoding="utf-8")
    known_hosts.write_text("vps.example.invalid ssh-ed25519 AAAA", encoding="utf-8")
    monkeypatch.setattr(gui, "load", lambda path: type("Profile", (), {
        "kt_python": "python", "workspace_path": "workspace", "kt_home": "home",
        "public_origin": "https://device.example.invalid",
        "workspace_id": "default", "remote_forward_host": "127.0.0.1",
        "remote_forward_port": 18766, "local_port": 8865,
        "ssh_host": "vps.example.invalid", "ssh_user": "kt",
        "ssh_identity_file": str(identity), "ssh_known_hosts": str(known_hosts),
        "ssh_port": 2222, "ssh_extra_options": [], "secret": "",
    })())
    monkeypatch.setattr(gui, "KTRuntime", lambda *args, **kwargs: type("Runtime", (), {
        "setup": lambda self: None,
        "start": lambda self, **kwargs: type(
            "Status", (), {"cli_status": "error", "local_ready": True, "detail": "terminal"}
        )(),
        "url": lambda self: pytest.fail("KT url must not run after a terminal start status"),
    })())

    def provision(*args, **kwargs):
        nonlocal provisioned
        provisioned = True

    monkeypatch.setattr(gui, "provision_and_save", provision)
    monkeypatch.setattr(gui, "validate_ssh_profile", lambda profile: None)
    class FakeTunnelProcess:
        running = True
        identity_ok = True
    class FakeTunnelStatus:
        configured = True
        process = FakeTunnelProcess()
        detail = "ready"
    class FakeTunnel:
        def __init__(self, *args, **kwargs): pass
        def start(self): return FakeTunnelStatus()
    monkeypatch.setattr(gui, "SSHTunnel", FakeTunnel)
    monkeypatch.setattr(gui, "verify_public_endpoint", lambda endpoint, profile: None)
    value.request_endpoint()
    _wait_for_worker(value)

    assert not provisioned
    assert "terminal" in value.status_var.get()
    assert value.output_var.get() == ""
    assert value.request_button.states == ["disabled", "normal"]


def test_button_ignores_second_click_until_first_callback_is_drained(monkeypatch, tmp_path):
    value = _gui_without_tk()
    root = QueuedRoot()
    value.root = root
    value.config_path = str(tmp_path)
    endpoint = "https://device.example.invalid/mcp/" + ("Q" * 43)
    Path(value.config_path, "profile.json").write_text("{}", encoding="utf-8")
    runtime_calls: list[str] = []
    provision_calls = 0

    monkeypatch.setattr(gui, "config_file", lambda path: Path(path) / "profile.json")
    identity = tmp_path / "id"
    known_hosts = tmp_path / "known_hosts"
    identity.write_text("key", encoding="utf-8")
    known_hosts.write_text("vps.example.invalid ssh-ed25519 AAAA", encoding="utf-8")
    monkeypatch.setattr(gui, "load", lambda path: type("Profile", (), {
        "kt_python": "python", "workspace_path": "workspace", "kt_home": "home",
        "public_origin": "https://device.example.invalid",
        "workspace_id": "default", "remote_forward_host": "127.0.0.1",
        "remote_forward_port": 18766, "local_port": 8865,
        "ssh_host": "vps.example.invalid", "ssh_user": "kt",
        "ssh_identity_file": str(identity), "ssh_known_hosts": str(known_hosts),
        "ssh_port": 2222, "ssh_extra_options": [], "secret": "",
    })())

    class FakeRuntime:
        def __init__(self, *args, **kwargs):
            pass

        def setup(self):
            runtime_calls.append("setup")

        def start(self, *, wait_for_public=True):
            assert wait_for_public is False
            runtime_calls.append("start")
            return type("Status", (), {"cli_status": "ready", "local_ready": True, "detail": ""})()

        def url(self):
            runtime_calls.append("url")
            return endpoint

    monkeypatch.setattr(gui, "KTRuntime", FakeRuntime)

    def provision(*args, **kwargs):
        nonlocal provision_calls
        provision_calls += 1
        return endpoint

    monkeypatch.setattr(gui, "provision_and_save", provision)
    monkeypatch.setattr(gui, "validate_ssh_profile", lambda profile: None)
    class FakeTunnelProcess:
        running = True
        identity_ok = True
    class FakeTunnelStatus:
        configured = True
        process = FakeTunnelProcess()
        detail = "ready"
    class FakeTunnel:
        def __init__(self, *args, **kwargs): pass
        def start(self): return FakeTunnelStatus()
    monkeypatch.setattr(gui, "SSHTunnel", FakeTunnel)
    monkeypatch.setattr(gui, "verify_public_endpoint", lambda endpoint, profile: None)
    value.request_endpoint()
    first_worker = value._worker
    assert first_worker is not None
    first_worker.join(timeout=1)
    assert not first_worker.is_alive()
    assert len(root.callbacks) == 1

    # The worker has completed, but its result callback is still pending.  A
    # second click must not start another runtime/provisioning request.
    value.request_endpoint()
    assert value._worker is first_worker
    assert runtime_calls == ["setup", "start", "url"]
    assert provision_calls == 1
    assert value.request_button.states == ["disabled"]

    root.drain()
    assert value._worker is None
    assert value.output_var.get() == endpoint
    assert value.request_button.states == ["disabled", "normal"]



def test_missing_token_fails_before_constructing_runtime(monkeypatch, tmp_path):
    value = _gui_without_tk()
    value.config_path = str(tmp_path)
    Path(tmp_path, "profile.json").write_text("{}", encoding="utf-8")
    monkeypatch.delenv("KT_MCP_PROVISION_TOKEN", raising=False)
    monkeypatch.setenv("KT_MCP_PROVISION_URL", "https://provision.example.invalid/api")
    monkeypatch.setattr(gui, "load", lambda path: type("Profile", (), {
        "kt_python": "python", "workspace_path": "workspace", "public_origin": "https://device.example.invalid"
    })())
    monkeypatch.setattr(gui, "KTRuntime", lambda *a, **k: pytest.fail("Must not construct runtime without token"))
    value.request_endpoint()
    _wait_for_worker(value)
    assert "KT_MCP_PROVISION_TOKEN" in value.status_var.get()
    assert not value.output_var.get()
    assert "本次耗时" in value.elapsed_var.get()
    assert not value.progress_bar.running
    assert value.request_button.states == ["disabled", "normal"]


def test_progress_remains_visible_while_worker_waits(monkeypatch):
    value = _gui_without_tk()
    waiting, release = threading.Event(), threading.Event()
    def work(origin):
        value._report_progress("[3/4] 等待本地监听")
        waiting.set()
        assert release.wait(2)
        raise ProvisioningUnavailable("服务器注册接口返回 HTTP 404")
    monkeypatch.setattr(value, "_runtime_and_provision", work)
    value.request_endpoint()
    try:
        assert waiting.wait(1)
        value.root.drain()
        assert "[3/4]" in value.status_var.get()
        assert "已用" in value.elapsed_var.get()
        assert value.progress_bar.running
    finally:
        release.set()
    _wait_for_worker(value)
    assert "HTTP 404" in value.status_var.get()
    assert not value.progress_bar.running


def test_worker_never_calls_tk_after_window_closes(monkeypatch):
    value = _gui_without_tk()
    value._closing = True
    value.root.after = lambda *a: pytest.fail("No worker thread may call root.after")
    monkeypatch.setattr(value, "_runtime_and_provision", lambda origin: "synthetic-result")
    worker = threading.Thread(target=value._worker_entry, args=("https://example.invalid",))
    worker.start(); worker.join(1)
    assert not worker.is_alive()
    assert value._events.get_nowait() == ("result", "synthetic-result", None)
