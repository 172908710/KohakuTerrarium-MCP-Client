"""Windows GUI single-instance coordination.

The lock is an OS-owned named mutex, not a PID or port marker.  A named
auto-reset event lets a secondary launch wake the primary without starting a
second GUI.  Non-Windows source runs use a no-op backend.
"""
from __future__ import annotations

import ctypes
import hashlib
import ntpath
import os
from pathlib import Path
import threading
from typing import Any

from .config import config_file


class InstanceError(RuntimeError):
    """Raised when the operating-system single-instance primitive fails."""


WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF


def _error(operation: str, code: int | None = None) -> InstanceError:
    suffix = f" (Windows error {code})" if code not in (None, 0) else ""
    return InstanceError(f"Windows single-instance {operation} failed{suffix}")


class _NoopBackend:
    """Cross-platform fallback used by source runs outside Windows."""

    def current_user_identity(self) -> str:
        return "non-windows"


class _WindowsBackend:
    def __init__(self) -> None:
        try:
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        except (AttributeError, OSError) as exc:
            raise InstanceError("Windows single-instance API is unavailable") from exc
        self._kernel32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
        self._kernel32.CreateEventW.restype = ctypes.c_void_p
        self._kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        self._kernel32.CreateMutexW.restype = ctypes.c_void_p
        self._kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        self._kernel32.SetEvent.argtypes = [ctypes.c_void_p]
        self._kernel32.SetEvent.restype = ctypes.c_int
        self._kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        self._kernel32.ReleaseMutex.restype = ctypes.c_int
        self._kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._kernel32.CloseHandle.restype = ctypes.c_int
        self._kernel32.GetCurrentProcess.argtypes = []
        self._kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p
        self._advapi32.OpenProcessToken.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)
        ]
        self._advapi32.OpenProcessToken.restype = ctypes.c_int
        self._advapi32.GetTokenInformation.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        self._advapi32.GetTokenInformation.restype = ctypes.c_int
        self._advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)
        ]
        self._advapi32.ConvertSidToStringSidW.restype = ctypes.c_int
        self._advapi32.GetUserNameW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
        self._advapi32.GetUserNameW.restype = ctypes.c_int

    def _last_error(self) -> int:
        return int(ctypes.get_last_error() or 0)

    def current_user_identity(self) -> str:
        # A SID remains stable when a user renames their account, and avoids
        # collisions between same-named accounts on a shared machine.  Keep a
        # username fallback for restricted Windows environments where token
        # APIs are unavailable (the identity still stays user-scoped).
        token = ctypes.c_void_p()
        token_query = 0x0008  # TOKEN_QUERY
        token_user = 1  # TokenUser
        try:
            opened = self._advapi32.OpenProcessToken(
                self._kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
            )
            if opened:
                try:
                    size = ctypes.c_uint32(0)
                    self._advapi32.GetTokenInformation(
                        token, token_user, None, 0, ctypes.byref(size)
                    )
                    if size.value:
                        buffer = ctypes.create_string_buffer(size.value)
                        if self._advapi32.GetTokenInformation(
                            token, token_user, buffer, size.value, ctypes.byref(size)
                        ):
                            # TOKEN_USER starts with a SID_AND_ATTRIBUTES pair;
                            # the first pointer is the SID address.
                            sid_pointer = ctypes.cast(
                                buffer, ctypes.POINTER(ctypes.c_void_p)
                            )[0]
                            sid_text = ctypes.c_wchar_p()
                            if sid_pointer and self._advapi32.ConvertSidToStringSidW(
                                sid_pointer, ctypes.byref(sid_text)
                            ):
                                value = sid_text.value.strip() if sid_text.value else ""
                                self._kernel32.LocalFree(sid_text)
                                if value:
                                    return value.casefold()
                finally:
                    self._kernel32.CloseHandle(token)
        except (AttributeError, OSError, TypeError, ValueError):
            # Fall through to GetUserNameW below.  This path is intentionally
            # quiet: a displayable startup error should not expose API details.
            pass
        size = ctypes.c_uint32(256)
        while True:
            buffer = ctypes.create_unicode_buffer(size.value)
            if self._advapi32.GetUserNameW(buffer, ctypes.byref(size)):
                value = buffer.value.strip()
                if value:
                    return value.casefold()
                raise _error("GetUserNameW returned an empty identity")
            code = self._last_error()
            if code == 122:  # ERROR_INSUFFICIENT_BUFFER
                size = ctypes.c_uint32(max(size.value * 2, 512))
                continue
            raise _error("GetUserNameW", code)

    def create_event(self, name: str) -> int:
        handle = self._kernel32.CreateEventW(None, 0, 0, name)
        if not handle:
            raise _error("CreateEventW", self._last_error())
        return int(handle)

    def create_mutex(self, name: str) -> int:
        handle = self._kernel32.CreateMutexW(None, 0, name)
        if not handle:
            raise _error("CreateMutexW", self._last_error())
        return int(handle)

    def wait(self, handle: int, timeout_ms: int) -> int:
        return int(self._kernel32.WaitForSingleObject(handle, timeout_ms))

    def set_event(self, handle: int) -> None:
        if not self._kernel32.SetEvent(handle):
            raise _error("SetEvent", self._last_error())

    def release_mutex(self, handle: int) -> None:
        if not self._kernel32.ReleaseMutex(handle):
            raise _error("ReleaseMutex", self._last_error())

    def close_handle(self, handle: int) -> None:
        if not self._kernel32.CloseHandle(handle):
            raise _error("CloseHandle", self._last_error())


class GUIInstance:
    """Own one GUI mutex and activation event for a canonical config path."""

    def __init__(self, config_path: str | os.PathLike[str] | None = None, *, backend: Any = None) -> None:
        self.config_path = self._canonical_config_path(config_path)
        self._backend = backend if backend is not None else (_WindowsBackend() if os.name == "nt" else _NoopBackend())
        identity = self._backend.current_user_identity()
        key = f"{identity}\x00{self.config_path}".encode("utf-8", "surrogatepass")
        digest = hashlib.sha256(key).hexdigest()
        self._mutex_name = f"Local\\KT-MCP-GUI-{digest}"
        self._event_name = f"Local\\KT-MCP-GUI-ACTIVATE-{digest}"
        self._event: Any = None
        self._mutex: Any = None
        self._acquired = False
        self._closed = False
        self._owner_thread = threading.get_ident()

    @staticmethod
    def _canonical_config_path(path: str | os.PathLike[str] | None) -> str:
        try:
            value = config_file(path)
            raw = os.fspath(value).replace("/", "\\")
            return ntpath.normcase(ntpath.normpath(ntpath.abspath(raw)))
        except Exception as exc:
            raise InstanceError("无法确定 GUI 配置路径") from exc

    @property
    def mutex_name(self) -> str:
        return self._mutex_name

    @property
    def event_name(self) -> str:
        return self._event_name

    def _cleanup_handles(self, *, release_mutex: bool) -> None:
        errors: list[InstanceError] = []
        if release_mutex and self._mutex is not None and self._acquired:
            try:
                self._backend.release_mutex(self._mutex)
            except InstanceError as exc:
                errors.append(exc)
            self._acquired = False
        for attr in ("_mutex", "_event"):
            handle = getattr(self, attr)
            if handle is None:
                continue
            try:
                self._backend.close_handle(handle)
            except InstanceError as exc:
                errors.append(exc)
            setattr(self, attr, None)
        if errors:
            raise errors[0]

    def acquire(self, *, activate_existing: bool) -> bool:
        """Acquire primary ownership, or signal the existing primary."""

        if self._closed:
            raise InstanceError("GUI single-instance handle is closed")
        if isinstance(self._backend, _NoopBackend):
            self._acquired = True
            return True
        if self._acquired:
            return True
        # Create the event first so an early normal launch cannot miss a
        # wakeup while the primary is still acquiring its mutex.
        try:
            self._event = self._backend.create_event(self._event_name)
            self._mutex = self._backend.create_mutex(self._mutex_name)
            result = self._backend.wait(self._mutex, 0)
            if result in (WAIT_OBJECT_0, WAIT_ABANDONED):
                self._acquired = True
                return True
            if result == WAIT_TIMEOUT:
                if activate_existing:
                    self._backend.set_event(self._event)
                self._cleanup_handles(release_mutex=False)
                return False
            raise _error("WaitForSingleObject", result)
        except Exception:
            try:
                self._cleanup_handles(release_mutex=False)
            except InstanceError:
                pass
            raise

    def consume_activation(self) -> bool:
        """Consume one activation request from the auto-reset event."""

        if self._closed or not self._acquired or self._event is None:
            return False
        result = self._backend.wait(self._event, 0)
        if result == WAIT_OBJECT_0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        if result == WAIT_ABANDONED:
            return True
        raise _error("WaitForSingleObject", result)

    def close(self) -> None:
        """Release and close all native handles; safe to call repeatedly."""

        if self._closed:
            return
        self._closed = True
        self._cleanup_handles(release_mutex=True)

    def __enter__(self) -> "GUIInstance":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


__all__ = ["GUIInstance", "InstanceError"]
