from __future__ import annotations

from pathlib import Path

import pytest

from kt_mcp_client import instance


class FakeBackend:
    """Deterministic named-object backend; no real process or credentials."""

    def __init__(self, waits: list[int] | None = None) -> None:
        self.waits = list(waits or [])
        self.calls: list[tuple] = []
        self.next_handle = 10
        self.raise_on: str | None = None

    def current_user_identity(self) -> str:
        self.calls.append(("identity",))
        return "S-1-5-21-synthetic-user"

    def _handle(self, kind: str, name: str) -> int:
        if self.raise_on == kind:
            raise instance.InstanceError(f"synthetic {kind} failure")
        handle = self.next_handle
        self.next_handle += 1
        self.calls.append((kind, name, handle))
        return handle

    def create_event(self, name: str) -> int:
        return self._handle("event", name)

    def create_mutex(self, name: str) -> int:
        return self._handle("mutex", name)

    def wait(self, handle: int, timeout_ms: int) -> int:
        self.calls.append(("wait", handle, timeout_ms))
        return self.waits.pop(0) if self.waits else instance.WAIT_TIMEOUT

    def set_event(self, handle: int) -> None:
        if self.raise_on == "set_event":
            raise instance.InstanceError("synthetic SetEvent failure")
        self.calls.append(("set_event", handle))

    def release_mutex(self, handle: int) -> None:
        if self.raise_on == "release_mutex":
            raise instance.InstanceError("synthetic ReleaseMutex failure")
        self.calls.append(("release_mutex", handle))

    def close_handle(self, handle: int) -> None:
        if self.raise_on == "close_handle":
            raise instance.InstanceError("synthetic CloseHandle failure")
        self.calls.append(("close", handle))


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return tmp_path / "profile.json"


def test_primary_creates_event_before_mutex_and_consumes_activation(config_path):
    backend = FakeBackend([instance.WAIT_OBJECT_0, instance.WAIT_OBJECT_0])
    owner = instance.GUIInstance(config_path, backend=backend)

    assert owner.acquire(activate_existing=True) is True
    kinds = [call[0] for call in backend.calls]
    assert kinds[:4] == ["identity", "event", "mutex", "wait"]
    assert owner.consume_activation() is True

    owner.close()
    assert ("release_mutex", 11) in backend.calls
    assert backend.calls[-2:] == [("close", 11), ("close", 10)]


def test_secondary_normal_launch_signals_existing_owner(config_path):
    backend = FakeBackend([instance.WAIT_TIMEOUT])
    secondary = instance.GUIInstance(config_path, backend=backend)

    assert secondary.acquire(activate_existing=True) is False
    assert any(call[0] == "set_event" for call in backend.calls)
    assert secondary.consume_activation() is False
    assert not any(call[0] == "release_mutex" for call in backend.calls)
    secondary.close()


def test_secondary_silent_start_does_not_signal_existing_owner(config_path):
    backend = FakeBackend([instance.WAIT_TIMEOUT])
    secondary = instance.GUIInstance(config_path, backend=backend)

    assert secondary.acquire(activate_existing=False) is False
    assert not any(call[0] == "set_event" for call in backend.calls)
    secondary.close()


def test_abandoned_mutex_is_recovered_as_primary(config_path):
    backend = FakeBackend([instance.WAIT_ABANDONED])
    owner = instance.GUIInstance(config_path, backend=backend)

    assert owner.acquire(activate_existing=False) is True
    assert owner.consume_activation() is False
    owner.close()


def test_acquire_error_cleans_event_and_mutex(config_path):
    backend = FakeBackend()
    backend.raise_on = "mutex"
    owner = instance.GUIInstance(config_path, backend=backend)

    with pytest.raises(instance.InstanceError):
        owner.acquire(activate_existing=True)
    assert [call[0] for call in backend.calls] == ["identity", "event", "close"]
    owner.close()


def test_close_is_idempotent_even_when_close_handle_reports_error(config_path):
    backend = FakeBackend([instance.WAIT_OBJECT_0])
    owner = instance.GUIInstance(config_path, backend=backend)
    assert owner.acquire(activate_existing=False)
    backend.raise_on = "close_handle"

    with pytest.raises(instance.InstanceError):
        owner.close()
    # The failed native close is not retried after the object has been marked
    # closed; a second call remains safe and does not leak a new mutex owner.
    owner.close()


def test_names_are_user_and_config_scoped(config_path):
    first = instance.GUIInstance(config_path, backend=FakeBackend())
    second = instance.GUIInstance(str(config_path).upper(), backend=FakeBackend())
    other = instance.GUIInstance(config_path.parent / "other.json", backend=FakeBackend())

    assert first.mutex_name == second.mutex_name
    assert first.event_name == second.event_name
    assert first.mutex_name.startswith("Local\\KT-MCP-GUI-")
    assert first.event_name.startswith("Local\\KT-MCP-GUI-")
    assert first.mutex_name != other.mutex_name


def test_non_windows_backend_is_noop(monkeypatch, config_path):
    monkeypatch.setattr(instance, "_NoopBackend", instance._NoopBackend)
    owner = instance.GUIInstance(config_path, backend=instance._NoopBackend())

    assert owner.acquire(activate_existing=True) is True
    assert owner.consume_activation() is False
    owner.close()
    owner.close()
