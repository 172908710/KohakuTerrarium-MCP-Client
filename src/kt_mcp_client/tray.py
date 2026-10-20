"""Small Windows notification-area controller for the launcher.

The portable runtime already ships ``pywin32`` (it is needed by the KT
runtime), so the tray icon uses the native Shell_NotifyIcon API instead of
adding another third-party dependency.  All Win32 calls are isolated here;
non-Windows builds simply report that no tray is available.
"""

from __future__ import annotations

import os
from typing import Callable


class TrayController:
    """Own a notification-area icon and dispatch its two menu actions.

    ``pump`` is called from Tk's event loop.  This keeps tray callbacks on the
    same thread as Tk, which is important because restoring or destroying a
    Tk window from a worker thread is unsafe.
    """

    _SHOW_ID = 1001
    _EXIT_ID = 1002

    def __init__(
        self,
        root: object,
        *,
        on_show: Callable[[], None],
        on_exit: Callable[[], None],
        tooltip: str = "KohakuTerrarium MCP",
    ) -> None:
        self.root = root
        self.on_show = on_show
        self.on_exit = on_exit
        self.tooltip = tooltip
        self.available = False
        self._stopped = False
        self._pump_id: object | None = None
        self._win32 = None
        self._win32api = None
        self._win32con = None
        self._winerror = None
        self._hwnd: int | None = None
        self._hicon: int | None = None
        self._nid: tuple | None = None
        self._class_name = "KohakuTerrariumMCPTray"

    def start(self) -> bool:
        """Create the icon and begin polling tray messages from Tk."""

        if os.name != "nt" or self.available:
            return self.available
        try:
            import win32api
            import win32con
            import win32gui
            import winerror
        except ImportError:
            # Unit tests and non-Windows source runs do not necessarily carry
            # pywin32.  The GUI remains usable; close falls back to normal Tk.
            return False

        self._win32 = win32gui
        self._win32api = win32api
        self._win32con = win32con
        self._winerror = winerror
        try:
            self._create_window()
            self._add_icon()
        except Exception:
            self.stop()
            return False
        self.available = True
        self._stopped = False
        self._schedule_pump()
        return True

    def _create_window(self) -> None:
        assert self._win32 is not None
        assert self._win32api is not None
        assert self._win32con is not None
        assert self._winerror is not None
        win32gui = self._win32
        win32api = self._win32api
        win32con = self._win32con

        # Include the process id so a second launcher instance never collides
        # with a class left behind by a previous process during shutdown.
        self._class_name = f"KohakuTerrariumMCPTray-{os.getpid()}"
        restart_message = win32gui.RegisterWindowMessage("TaskbarCreated")
        message_map = {
            restart_message: self._on_taskbar_restart,
            win32con.WM_DESTROY: self._on_destroy,
            win32con.WM_COMMAND: self._on_command,
            win32con.WM_USER + 20: self._on_taskbar_notify,
        }
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = self._class_name
        wc.style = win32con.CS_HREDRAW | win32con.CS_VREDRAW
        wc.hCursor = win32api.LoadCursor(0, win32con.IDC_ARROW)
        wc.hbrBackground = win32con.COLOR_WINDOW
        wc.lpfnWndProc = message_map
        try:
            win32gui.RegisterClass(wc)
        except win32gui.error as exc:
            if getattr(exc, "winerror", None) != self._winerror.ERROR_CLASS_ALREADY_EXISTS:
                raise
        self._hwnd = win32gui.CreateWindow(
            self._class_name,
            "KohakuTerrarium MCP",
            win32con.WS_OVERLAPPED | win32con.WS_SYSMENU,
            0,
            0,
            win32con.CW_USEDEFAULT,
            win32con.CW_USEDEFAULT,
            0,
            0,
            wc.hInstance,
            None,
        )
        win32gui.UpdateWindow(self._hwnd)

    def _add_icon(self) -> None:
        assert self._win32 is not None
        assert self._win32con is not None
        assert self._hwnd is not None
        win32gui = self._win32
        win32con = self._win32con
        self._hicon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        self._nid = (
            self._hwnd,
            0,
            flags,
            win32con.WM_USER + 20,
            self._hicon,
            self.tooltip,
        )
        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, self._nid)

    def _schedule_pump(self) -> None:
        if not self.available or self._stopped:
            return
        try:
            self._pump_id = self.root.after(100, self.pump)  # type: ignore[attr-defined]
        except Exception:
            self._pump_id = None

    def pump(self) -> None:
        """Process queued Shell_NotifyIcon messages without blocking Tk."""

        self._pump_id = None
        if not self.available or self._stopped or self._win32 is None:
            return
        try:
            self._win32.PumpWaitingMessages()
        except Exception:
            # Explorer can restart while the icon is being recreated.  The
            # next TaskbarCreated message will re-add it; a transient error
            # should not take down the MCP process.
            pass
        self._schedule_pump()

    def _on_taskbar_restart(self, *_args: object) -> None:
        if self._stopped:
            return
        try:
            self._add_icon()
        except Exception:
            pass

    def _on_destroy(self, *_args: object) -> int:
        if self._win32 is not None and self._nid is not None:
            try:
                self._win32.Shell_NotifyIcon(self._win32.NIM_DELETE, self._nid[:2])
            except Exception:
                pass
        return 0

    def _on_taskbar_notify(self, _hwnd: int, _msg: int, _wparam: int, lparam: int) -> int:
        assert self._win32con is not None
        if lparam in (self._win32con.WM_LBUTTONUP, self._win32con.WM_LBUTTONDBLCLK):
            self.on_show()
        elif lparam == self._win32con.WM_RBUTTONUP:
            self._show_menu()
        return 1

    def _show_menu(self) -> None:
        assert self._win32 is not None
        assert self._win32con is not None
        assert self._hwnd is not None
        assert self._win32api is not None
        menu = self._win32.CreatePopupMenu()
        try:
            self._win32.AppendMenu(menu, self._win32con.MF_STRING, self._SHOW_ID, "显示窗口")
            self._win32.AppendMenu(
                menu,
                self._win32con.MF_STRING,
                self._EXIT_ID,
                "退出并停止本地 MCP",
            )
            pos = self._win32.GetCursorPos()
            self._win32.SetForegroundWindow(self._hwnd)
            self._win32.TrackPopupMenu(
                menu,
                self._win32con.TPM_LEFTALIGN | self._win32con.TPM_RIGHTBUTTON,
                pos[0],
                pos[1],
                0,
                self._hwnd,
                None,
            )
            self._win32.PostMessage(self._hwnd, self._win32con.WM_NULL, 0, 0)
        finally:
            destroy_menu = getattr(self._win32, "DestroyMenu", None)
            if callable(destroy_menu):
                destroy_menu(menu)

    def _on_command(self, _hwnd: int, _msg: int, wparam: int, _lparam: int) -> int:
        assert self._win32api is not None
        command = self._win32api.LOWORD(wparam)
        if command == self._SHOW_ID:
            self.on_show()
        elif command == self._EXIT_ID:
            self.on_exit()
        return 0

    def stop(self) -> None:
        """Remove the icon and stop polling; idempotent."""

        if self._stopped and not self.available:
            return
        self._stopped = True
        pump_id = self._pump_id
        self._pump_id = None
        if pump_id is not None:
            try:
                self.root.after_cancel(pump_id)  # type: ignore[attr-defined]
            except Exception:
                pass
        if self._win32 is not None and self._hwnd is not None:
            try:
                self._win32.DestroyWindow(self._hwnd)
            except Exception:
                pass
        self._hwnd = None
        self._nid = None
        self.available = False


__all__ = ["TrayController"]
