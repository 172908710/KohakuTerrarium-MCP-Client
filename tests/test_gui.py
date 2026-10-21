from __future__ import annotations

import time
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

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
        self.owner_thread = threading.get_ident()
        self.destroyed = False
        self.clipboard = ""
        self.clipboard_writes = 0

    def clipboard_get(self):
        assert threading.get_ident() == self.owner_thread
        return self.clipboard

    def clipboard_clear(self):
        assert threading.get_ident() == self.owner_thread
        self.clipboard = ""
        self.clipboard_writes += 1

    def clipboard_append(self, text):
        assert threading.get_ident() == self.owner_thread
        self.clipboard += text

    def destroy(self):
        assert threading.get_ident() == self.owner_thread
        self.destroyed = True

    def after_cancel(self, callback):
        assert threading.get_ident() == self.owner_thread

    def after(self, delay: int, callback) -> None:
        assert threading.get_ident() == self.owner_thread
        self.callbacks.append(callback)

    def drain(self) -> None:
        callbacks, self.callbacks = self.callbacks, []
        for callback in callbacks:
            callback()


@pytest.fixture
def form_gui(monkeypatch):
    class FormRoot(QueuedRoot):
        def __init__(self):
            super().__init__()
            self.hidden = False

        def title(self, *_args): pass
        def resizable(self, *_args): pass
        def minsize(self, *_args): pass
        def geometry(self, *_args): pass
        def protocol(self, *_args): pass

        def withdraw(self):
            self.hidden = True

    root = FormRoot()
    monkeypatch.setattr(gui.tk, "Tk", lambda: root)
    monkeypatch.setattr(gui.tk, "StringVar", FakeVar)
    monkeypatch.setattr(gui.tk, "BooleanVar", FakeVar)
    monkeypatch.setattr(gui, "TrayController", lambda *a, **k: object())
    monkeypatch.setattr(gui.ClientGUI, "_read_startup_state", staticmethod(lambda: False))
    monkeypatch.setattr(gui.ClientGUI, "_build", lambda self: None)
    return root


@pytest.mark.parametrize("startup_mode", [False, True])
def test_form_starts_empty_even_with_saved_settings(form_gui, monkeypatch, startup_mode):
    loads = []

    def saved_profile(path):
        loads.append(path)
        return SimpleNamespace(
            public_origin="https://device.example.invalid",
            provision_token="synthetic-saved-key",
        )

    monkeypatch.setattr(gui, "load", saved_profile)
    value = gui.ClientGUI(startup_mode=startup_mode)

    assert value.origin_var.get() == ""
    assert value.provision_key_var.get() == ""
    assert loads == []


@pytest.mark.parametrize("saved_origin,expected_resume", [
    ("https://device.example.invalid", True),
    (gui.DEFAULT_ORIGIN, False),
    ("", False),
    ("invalid-origin", False),
])
def test_silent_startup_uses_saved_origin_not_empty_form(
    form_gui, monkeypatch, saved_origin, expected_resume
):
    instances = []
    resumed = []
    monkeypatch.setattr(gui, "load", lambda path: SimpleNamespace(
        public_origin=saved_origin, provision_token="synthetic-saved-key"
    ))
    monkeypatch.setattr(gui.ClientGUI, "_ensure_tray", lambda self: True)
    monkeypatch.setattr(gui.ClientGUI, "run", lambda self: instances.append(self))
    monkeypatch.setattr(gui.ClientGUI, "resume_saved_services", lambda self: resumed.append(True))

    gui.run_gui(startup_mode=True)

    assert form_gui.hidden
    assert instances[0].origin_var.get() == ""
    assert instances[0].provision_key_var.get() == ""
    assert len(form_gui.callbacks) == int(expected_resume)
    form_gui.drain()
    assert resumed == ([True] if expected_resume else [])


def test_normal_launch_does_not_read_saved_settings(form_gui, monkeypatch):
    monkeypatch.setattr(gui, "load", lambda path: pytest.fail("Normal launch must not load credentials"))
    monkeypatch.setattr(gui.ClientGUI, "_ensure_tray", lambda self: True)
    monkeypatch.setattr(gui.ClientGUI, "run", lambda self: None)

    gui.run_gui()

    assert not form_gui.hidden
    assert not form_gui.callbacks


def _gui_without_tk() -> gui.ClientGUI:
    value = object.__new__(gui.ClientGUI)
    value.root = QueuedRoot()
    value.config_path = None
    value.origin_var = FakeVar("https://device.example.invalid")
    value.provision_key_var = FakeVar()
    value.output_var = FakeVar()
    value.status_var = FakeVar("状态：等待请求")
    value.request_button = FakeButton()
    value.paste_button = FakeButton()
    value.copy_button = FakeButton()
    value.startup_checkbutton = FakeButton()
    value.origin_entry = FakeButton()
    value.provision_key_entry = FakeButton()
    value.progress_bar = FakeProgress()
    value.elapsed_var = FakeVar()
    value._events = queue.SimpleQueue()
    value._started_at = value._stage_at = 0.0
    value._stage = ""
    value._poll_id = None
    value._closing = False
    value._shutdown_requested = False
    value._shutdown_worker = None
    value._shutdown_poll_id = None
    value._service_lock = threading.Lock()
    value._supervisor = None
    value._runtime = None
    value._tunnel = None
    value._worker = None
    return value


class FakeTray:
    def __init__(self):
        self.stopped = False
        self.tooltip = ""
        self.owner_thread = threading.get_ident()

    def set_tooltip(self, text):
        assert threading.get_ident() == self.owner_thread
        self.tooltip = text

    def stop(self):
        assert threading.get_ident() == self.owner_thread
        self.stopped = True


@pytest.mark.parametrize("full_status", ["Synthetic failure. " * 80, "line\n" * 12, "line\n" * 120])
def test_long_status_preview_preserves_complete_details(monkeypatch, full_status):
    value = _gui_without_tk()
    value._status_display_var = FakeVar()
    styles, dialogs = [], []
    value.status_label = SimpleNamespace(configure=lambda **kwargs: styles.append(kwargs))
    monkeypatch.setattr(value, "_open_status_details", lambda text: dialogs.append(text))
    value.status_var.set(full_status)

    value._refresh_status_label()
    value._show_status_details()

    assert value.status_var.get() == full_status
    assert len(value._status_display_var.get()) < gui.STATUS_PREVIEW_LIMIT + 20
    assert "点击查看详情" in value._status_display_var.get()
    assert styles[-1] == {"cursor": "hand2", "takefocus": True}
    assert dialogs == [full_status]

    value.status_var.set("Ready")
    value._refresh_status_label()
    value._show_status_details()
    assert value._status_display_var.get() == "Ready"
    assert styles[-1] == {"cursor": "", "takefocus": False}
    assert len(dialogs) == 1


def test_status_panel_is_above_fields_and_window_fits_content(monkeypatch):
    root = None
    try:
        root = gui.tk.Tk()
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-alpha", 0)
    except gui.tk.TclError:
        if root is not None:
            root.destroy()
        pytest.skip("Tk display unavailable")
    monkeypatch.setattr(gui.ClientGUI, "_read_startup_state", staticmethod(lambda: False))
    try:
        value = gui.ClientGUI(root)
        root.deiconify()
        root.update()
        initial_height = root.winfo_height()
        assert initial_height < 260

        for status in [
            "Ready",
            "状态：公网 MCP 已完成 initialize、tools/list 与无害 tree 验证。",
            "状态：" + "synthetic error detail " * 80,
            "line\n" * 120,
            "Ready",
        ]:
            value.status_var.set(status)
            root.update()
            panel = value.status_panel
            assert panel.winfo_rootx() - root.winfo_rootx() >= root.winfo_width() / 2
            assert panel.winfo_rooty() + panel.winfo_height() <= value.origin_entry.winfo_rooty()
            for widget in (value.startup_checkbutton, value.status_label, value.elapsed_label,
                           value.origin_entry, value.paste_button, value.request_button, value.copy_button):
                assert widget.winfo_rootx() + widget.winfo_width() <= root.winfo_rootx() + root.winfo_width()
                assert widget.winfo_rooty() + widget.winfo_height() <= root.winfo_rooty() + root.winfo_height()
            bottom_gap = root.winfo_rooty() + root.winfo_height() - value.copy_button.winfo_rooty() - value.copy_button.winfo_height()
            assert 0 <= bottom_gap <= 30
            assert root.winfo_height() < 400
        assert root.winfo_height() == initial_height

        dialogs, text_widgets = [], []
        native_dialog, native_text = gui.tk.Toplevel, gui.scrolledtext.ScrolledText

        def hidden_dialog(parent):
            dialog = native_dialog(parent)
            dialog.withdraw()
            dialog.overrideredirect(True)
            dialog.attributes("-alpha", 0)
            dialogs.append(dialog)
            return dialog

        def detail_text(*args, **kwargs):
            widget = native_text(*args, **kwargs)
            text_widgets.append(widget)
            return widget

        monkeypatch.setattr(gui.tk, "Toplevel", hidden_dialog)
        monkeypatch.setattr(gui.scrolledtext, "ScrolledText", detail_text)
        full_status = "synthetic detail line\n" * 120
        value._open_status_details(full_status)
        dialogs[0].deiconify()
        root.update()
        assert text_widgets[0].get("1.0", "end-1c") == full_status
        assert str(text_widgets[0].cget("state")) == "disabled"
        text_widgets[0].yview_moveto(1)
        root.update()
        assert text_widgets[0].yview()[0] > 0
        dialogs[0].destroy()
    finally:
        root.destroy()


def test_paste_origin_reads_only_on_click_and_does_not_fill_key():
    value = _gui_without_tk()
    value.root.clipboard = "  https://pasted.example.invalid/ \n"
    value.origin_var.set("")

    value._paste_origin()

    assert value.origin_var.get() == "https://pasted.example.invalid/"
    assert value.provision_key_var.get() == ""
    assert value.root.clipboard_writes == 0


def test_clipboard_buttons_handle_empty_or_nontext_clipboard(monkeypatch):
    value = _gui_without_tk()
    value.root.clipboard = "leave-existing-clipboard"
    value._copy_endpoint()
    assert value.root.clipboard_writes == 0

    def no_text():
        raise gui.tk.TclError("nontext clipboard")
    monkeypatch.setattr(value.root, "clipboard_get", no_text)
    value._paste_origin()
    assert "没有可粘贴" in value.status_var.get()


def test_copy_button_uses_only_verified_output_and_resets_on_failure():
    value = _gui_without_tk()
    endpoint = "https://device.example.invalid/mcp/" + "Q" * 43
    value._finish_request(endpoint, None)
    assert value.copy_button.states[-1] == "normal"
    value._copy_endpoint()
    assert value.root.clipboard == endpoint
    assert value.root.clipboard_writes == 1
    assert "已复制" in value.elapsed_var.get()

    value._finish_request(None, ProvisioningUnavailable("test failure"))
    assert value.copy_button.states[-1] == "disabled"
    value._copy_endpoint()
    assert value.root.clipboard_writes == 1


def test_exit_returns_promptly_and_waits_for_cleanup_on_ui_thread(monkeypatch):
    value = _gui_without_tk()
    value._tray_controller = FakeTray()
    started, release = threading.Event(), threading.Event()
    calls = []

    def slow_stop():
        calls.append(threading.get_ident())
        started.set()
        assert release.wait(3)

    monkeypatch.setattr(value, "_stop_local_services", slow_stop)
    before = time.monotonic()
    value._quit_app()
    assert time.monotonic() - before < 0.5
    assert started.wait(1)
    worker = value._shutdown_worker
    try:
        value._quit_app()
        assert value._shutdown_worker is worker
        value.root.drain()
        assert not value.root.destroyed
        assert not value._tray_controller.stopped
        assert value._shutdown_requested
    finally:
        release.set()
        worker.join(3)
    value.root.drain()
    assert value.root.destroyed
    assert value._tray_controller.stopped
    assert calls == [worker.ident]
    assert worker.ident != threading.get_ident()


def test_exit_joins_inflight_startup_before_final_cleanup(monkeypatch):
    value = _gui_without_tk()
    value._tray_controller = FakeTray()
    started, release = threading.Event(), threading.Event()
    calls = []

    def startup():
        started.set()
        assert release.wait(3)
        calls.append("startup-finished")

    value._worker = threading.Thread(target=startup)
    value._worker.start()
    assert started.wait(1)
    monkeypatch.setattr(value, "_stop_local_services", lambda: calls.append("stop"))
    value._quit_app()
    shutdown = value._shutdown_worker
    try:
        assert calls == []
        assert not value.root.destroyed
    finally:
        release.set()
        shutdown.join(3)
    value.root.drain()
    assert calls == ["startup-finished", "stop"]
    assert value.root.destroyed


def test_exit_failure_keeps_tray_alive_and_reports_failure(monkeypatch):
    value = _gui_without_tk()
    value._tray_controller = FakeTray()
    shown = []
    monkeypatch.setattr(value, "_show_window", lambda: shown.append(True))

    def failed_stop():
        raise RuntimeError("synthetic-stop-error")
    monkeypatch.setattr(value, "_stop_local_services", failed_stop)
    value._quit_app()
    value._shutdown_worker.join(3)
    value.root.drain()
    assert not value.root.destroyed
    assert not value._tray_controller.stopped
    assert "停止未完成" in value.status_var.get()
    assert shown == [True]
    assert value._closing is False


@pytest.mark.parametrize("use_supervisor", [False, True])
def test_shutdown_stops_once_without_constructing_another_supervisor(monkeypatch, use_supervisor):
    value = _gui_without_tk()
    calls = []
    process = SimpleNamespace(running=False)
    value._runtime = SimpleNamespace(stop=lambda: (
        calls.append("runtime") or SimpleNamespace(cli_status="stopped", local_ready=False, process=process)
    ))
    value._tunnel = SimpleNamespace(stop=lambda: (
        calls.append("tunnel") or SimpleNamespace(process=process, detail="")
    ))
    if use_supervisor:
        value._supervisor = SimpleNamespace(stop=lambda: (
            calls.append("supervisor") or SimpleNamespace(
                runtime={"cli_status": "stopped", "local_ready": False, "process": {"running": False}},
                tunnel={"process": {"running": False}}, detail="",
            )
        ))
    monkeypatch.setattr(gui, "load", lambda p: pytest.fail("No redundant profile reload"))
    monkeypatch.setattr(gui, "Supervisor", lambda *a, **k: pytest.fail("No second supervisor"))
    value._stop_local_services()
    assert calls == (["supervisor"] if use_supervisor else ["tunnel", "runtime"])


def test_shutdown_still_stops_runtime_when_tunnel_reports_failure():
    value = _gui_without_tk()
    calls = []
    value._tunnel = SimpleNamespace(stop=lambda: SimpleNamespace(
        process=SimpleNamespace(running=True), detail="synthetic tunnel failure"
    ))
    value._runtime = SimpleNamespace(stop=lambda: (
        calls.append("runtime") or SimpleNamespace(
            cli_status="stopped", local_ready=False, process=SimpleNamespace(running=False)
        )
    ))
    with pytest.raises(gui.SupervisorError):
        value._stop_local_services()
    assert calls == ["runtime"]


def test_shutdown_skips_runtime_stop_for_never_started_install(monkeypatch, tmp_path):
    value = _gui_without_tk()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{}", encoding="utf-8")
    calls = []
    monkeypatch.setattr(gui, "config_file", lambda path: profile_path)
    monkeypatch.setattr(gui, "load", lambda path: object())
    monkeypatch.setattr(gui, "KTRuntime", lambda *a, **k: SimpleNamespace(
        connection_path=tmp_path / "missing-endpoint" / "connection.json",
        stop=lambda: pytest.fail("No KT stop without endpoint state"),
    ))
    monkeypatch.setattr(gui, "SSHTunnel", lambda *a, **k: SimpleNamespace(stop=lambda: (
        calls.append("tunnel") or SimpleNamespace(process=SimpleNamespace(running=False), detail="")
    )))

    value._stop_local_services()

    assert calls == ["tunnel"]


@pytest.mark.parametrize("component", ["runtime", "tunnel"])
def test_shutdown_rejects_stopped_label_with_live_process(component):
    value = _gui_without_tk()
    status = SimpleNamespace(
        state="stopped",
        runtime={"cli_status": "stopped", "local_ready": False, "process": {"running": False}},
        tunnel={"process": {"running": False}},
        detail="",
    )
    getattr(status, component)["process"]["running"] = True
    value._supervisor = SimpleNamespace(stop=lambda: status)

    with pytest.raises(gui.SupervisorError):
        value._stop_local_services()


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


def test_logon_resume_starts_saved_services_without_reregistering(monkeypatch, tmp_path):
    value = _gui_without_tk()
    value.config_path = str(tmp_path)
    value._service_lock = threading.Lock()
    value._shutdown_requested = False
    value._supervisor = None
    value._runtime = None
    value._tunnel = None
    Path(tmp_path, "profile.json").write_text("{}", encoding="utf-8")
    endpoint = "https://device.example.invalid/mcp/" + ("Q" * 43)
    calls: list[str] = []

    monkeypatch.setattr(gui, "config_file", lambda path: Path(path) / "profile.json")
    monkeypatch.setattr(gui, "load", lambda path: type("Profile", (), {
        "public_origin": "https://device.example.invalid",
    })())

    class FakeRuntime:
        def url(self):
            calls.append("url")
            return endpoint

    class FakeSupervisor:
        def __init__(self, profile, runtime_root):
            self.runtime = FakeRuntime()
            self.tunnel = object()

        def start(self):
            calls.append("start")
            return type("Status", (), {"state": "running", "detail": ""})()

        def stop(self):
            calls.append("stop")

    monkeypatch.setattr(gui, "Supervisor", FakeSupervisor)
    monkeypatch.setattr(gui, "verify_public_endpoint", lambda value, profile: calls.append("verify"))

    assert value._resume_saved_services() == endpoint
    assert calls == ["start", "url", "verify"]


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
