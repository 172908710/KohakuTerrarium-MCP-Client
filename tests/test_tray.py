from types import SimpleNamespace

from kt_mcp_client.tray import TrayController


def make_tray():
    calls = []
    root = SimpleNamespace(after=lambda *args: None)
    tray = TrayController(root, on_show=lambda: calls.append("show"), on_exit=lambda: calls.append("exit"))
    tray.available = True
    tray._win32api = SimpleNamespace(LOWORD=lambda value: value & 0xffff)
    tray._win32con = SimpleNamespace(WM_LBUTTONUP=1, WM_LBUTTONDBLCLK=2, WM_RBUTTONUP=3)
    tray._win32 = SimpleNamespace(PumpWaitingMessages=lambda: None)
    return tray, calls


def test_native_commands_only_queue_actions_until_tk_poll():
    tray, calls = make_tray()
    tray._on_command(0, 0, tray._SHOW_ID, 0)
    tray._on_command(0, 0, tray._EXIT_ID, 0)
    assert calls == []
    tray.pump()
    assert calls == ["show", "exit"]


def test_native_left_click_does_not_reenter_tk():
    tray, calls = make_tray()
    tray._on_taskbar_notify(0, 0, 0, tray._win32con.WM_LBUTTONUP)
    assert calls == []
    tray.pump()
    assert calls == ["show"]


def test_stopped_tray_does_not_dispatch_pending_actions():
    tray, calls = make_tray()
    tray._on_command(0, 0, tray._EXIT_ID, 0)
    tray._stopped = True
    tray.pump()
    assert calls == []
