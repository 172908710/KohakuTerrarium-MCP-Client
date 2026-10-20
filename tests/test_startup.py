from __future__ import annotations

from kt_mcp_client import startup


class _Key:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _FakeWinreg:
    HKEY_CURRENT_USER = object()
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def OpenKey(self, *_args):
        if not self.values:
            raise FileNotFoundError
        return _Key(self.values)

    def CreateKey(self, *_args):
        return _Key(self.values)

    def QueryValueEx(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, _reserved, _kind, value):
        key.values[name] = value

    def DeleteValue(self, key, name):
        if name not in key.values:
            raise FileNotFoundError
        del key.values[name]


def test_startup_command_uses_one_file_launcher_without_secrets(monkeypatch, tmp_path):
    launcher = tmp_path / "KohakuTerrarium-MCP-Client.exe"
    launcher.write_bytes(b"test")
    monkeypatch.setenv("KT_MCP_LAUNCHER_EXE", str(launcher))
    command = startup.startup_command(tmp_path / "data" / "profile.json")
    assert str(launcher) in command
    assert "--startup" in command
    assert "profile.json" in command
    assert "token" not in command.casefold()


def test_startup_registry_toggle_is_per_user_and_idempotent(monkeypatch, tmp_path):
    fake = _FakeWinreg()
    monkeypatch.setattr(startup, "winreg", fake)
    monkeypatch.setattr(startup.os, "name", "nt")
    monkeypatch.setattr(startup, "startup_command", lambda path: f"client --startup {path}")

    assert startup.is_startup_enabled(value_name="test-client") is False
    assert startup.set_startup_enabled(True, tmp_path / "profile.json", value_name="test-client")
    assert startup.is_startup_enabled(value_name="test-client") is True
    assert startup.set_startup_enabled(False, value_name="test-client") is False
    assert startup.is_startup_enabled(value_name="test-client") is False
    assert startup.set_startup_enabled(False, value_name="test-client") is False
