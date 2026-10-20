"""SSH reverse tunnel construction and lifecycle."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .config import DeviceProfile
from .process import ManagedProcess, ProcessError, ProcessStatus


class SSHTunnelError(ValueError):
    """Raised when SSH settings are incomplete or unsafe."""


_ALLOWED_EXTRA_OPTIONS = {
    "AddressFamily", "BindInterface", "CanonicalizeHostname", "Compression",
    "ConnectTimeout", "IPQoS", "LogLevel", "NoHostAuthenticationForLocalhost",
    "ServerAliveCountMax", "ServerAliveInterval", "TCPKeepAlive", "UpdateHostkeys",
}
_FORBIDDEN_OPTIONS = {
    "BatchMode", "ExitOnForwardFailure", "HostKeyAlias", "IdentityFile",
    "LocalCommand", "LocalForward", "PermitLocalCommand", "ProxyCommand",
    "ProxyJump", "RemoteCommand", "RemoteForward", "RequestTTY",
    "StrictHostKeyChecking", "UserKnownHostsFile", "ClearAllForwardings",
}
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_./:@%+\[\],=-]+$")
_SAFE_HOST = re.compile(r"^[A-Za-z0-9_.:\-\[\]]+$")
_SAFE_USER = re.compile(r"^[A-Za-z0-9_.-]+$")


def _required_text(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise SSHTunnelError(f"{label} 未配置")
    if any(char.isspace() for char in text) or "\x00" in text:
        raise SSHTunnelError(f"{label} 包含非法字符")
    return text


def _validate_host(value: object, label: str) -> str:
    text = _required_text(value, label)
    if text.startswith("-") or not _SAFE_HOST.fullmatch(text):
        raise SSHTunnelError(f"{label} 无效")
    return text


def _validate_port(value: object, label: str) -> str:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise SSHTunnelError(f"{label} 必须是端口") from exc
    if not 1 <= port <= 65535:
        raise SSHTunnelError(f"{label} 必须是 1-65535 的端口")
    return str(port)


def _validate_file(value: object, label: str) -> str:
    text = _required_text(value, label)
    path = Path(text).expanduser()
    if not path.is_file():
        raise SSHTunnelError(f"{label} 不存在或不是文件")
    return str(path)


def normalise_extra_options(options: Sequence[str] | None) -> list[str]:
    if options is None:
        return []
    if not isinstance(options, (list, tuple)):
        raise SSHTunnelError("ssh_extra_options 必须是列表")
    values = [str(item) for item in options]
    if any("\x00" in value for value in values):
        raise SSHTunnelError("ssh_extra_options 包含 NUL")
    specs: list[str] = []
    index = 0
    while index < len(values):
        token = values[index]
        if token == "-o":
            index += 1
            if index >= len(values):
                raise SSHTunnelError("ssh_extra_options 的 -o 缺少参数")
            token = values[index]
        elif token.startswith("-"):
            raise SSHTunnelError("ssh_extra_options 只允许 -o key=value")
        if any(char.isspace() for char in token) or token.count("=") != 1:
            raise SSHTunnelError("ssh_extra_options 必须使用无空白 key=value")
        key, value = token.split("=", 1)
        canonical_key = next((candidate for candidate in _ALLOWED_EXTRA_OPTIONS if candidate.casefold() == key.casefold()), None)
        if canonical_key is None or key.casefold() in {item.casefold() for item in _FORBIDDEN_OPTIONS}:
            raise SSHTunnelError(f"SSH extra option 不在白名单：{key}")
        if not value or not _SAFE_VALUE.fullmatch(value):
            raise SSHTunnelError(f"SSH extra option 值无效：{key}")
        specs.append(f"{canonical_key}={value}")
        index += 1
    return specs


def validate_ssh_profile(profile: DeviceProfile) -> None:
    if str(profile.remote_forward_host).strip() != "127.0.0.1":
        raise SSHTunnelError("remote_forward_host 必须固定为 127.0.0.1")
    _validate_host(profile.ssh_host, "ssh_host")
    _validate_port(profile.ssh_port, "ssh_port")
    user = _required_text(profile.ssh_user, "ssh_user")
    if not _SAFE_USER.fullmatch(user):
        raise SSHTunnelError("ssh_user 无效")
    _validate_file(profile.ssh_identity_file, "ssh_identity_file")
    _validate_file(profile.ssh_known_hosts, "ssh_known_hosts")
    _validate_host(profile.remote_forward_host, "remote_forward_host")
    remote_port = _validate_port(profile.remote_forward_port, "remote_forward_port")
    local_port = _validate_port(profile.local_port, "local_port")
    if int(remote_port) == 18765 or int(local_port) == 8765:
        raise SSHTunnelError("拒绝使用当前生产端口")
    normalise_extra_options(profile.ssh_extra_options)


def build_reverse_command(profile: DeviceProfile, *, ssh_executable: str = "ssh") -> list[str]:
    """Build argv for an SSH reverse tunnel without a shell or secret."""

    validate_ssh_profile(profile)
    executable = _required_text(ssh_executable, "ssh executable")
    if any(char.isspace() for char in executable):
        raise SSHTunnelError("ssh executable 路径包含空白，请使用单独 argv")
    identity = str(Path(profile.ssh_identity_file).expanduser())
    known_hosts = str(Path(profile.ssh_known_hosts).expanduser())
    remote = (
        f"{_validate_host(profile.remote_forward_host, 'remote_forward_host')}"
        f":{_validate_port(profile.remote_forward_port, 'remote_forward_port')}"
        f":127.0.0.1:{_validate_port(profile.local_port, 'local_port')}"
    )
    command = [
        executable, "-N", "-T", "-R", remote, "-p", _validate_port(profile.ssh_port, "ssh_port"),
        "-i", identity,
        # ``-F none`` prevents user/system config from adding forwards. Do not
        # use ClearAllForwardings here: OpenSSH for Windows also clears the
        # explicit command-line -R on some builds.
        "-F", "none",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={known_hosts}",
    ]
    for option in normalise_extra_options(profile.ssh_extra_options):
        command.extend(["-o", option])
    command.append(f"{_required_text(profile.ssh_user, 'ssh_user')}@{_validate_host(profile.ssh_host, 'ssh_host')}")
    if profile.secret and profile.secret in "\x00".join(command):
        raise SSHTunnelError("拒绝将 secret 放入 SSH 命令行")
    return command


@dataclass(frozen=True)
class TunnelStatus:
    process: ProcessStatus
    configured: bool
    detail: str = ""


class SSHTunnel:
    """Manage the SSH process; supervisor owns reconnect policy."""

    def __init__(self, profile: DeviceProfile, runtime_dir: str | os.PathLike[str], *, process: ManagedProcess | None = None, ssh_executable: str = "ssh") -> None:
        self.profile = profile
        self.runtime_dir = Path(runtime_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.ssh_executable = ssh_executable
        self.process = process or ManagedProcess(
            "ssh-tunnel", self.runtime_dir / "ssh-tunnel.log",
            record_path=self.runtime_dir / "ssh-tunnel.pid.json", secrets=(profile.secret,),
        )

    def command(self) -> list[str]:
        return build_reverse_command(self.profile, ssh_executable=self.ssh_executable)

    def start(self) -> TunnelStatus:
        command = self.command()
        # Persist all security-relevant identity tokens, including user@host,
        # remote bind, local port, and -N/-T.  A short token alone is unsafe.
        status = self.process.start(command, identity_tokens=tuple(command[1:]))
        if not status.running or not status.identity_ok:
            detail = "SSH 隧道进程启动后立即退出或身份校验失败；公网路由仍需单独配置"
            return TunnelStatus(status, True, detail)
        return TunnelStatus(status, True, "SSH 进程运行；仅表示 SSH 进程运行，VPS 路由仍需单独配置")

    def stop(self) -> TunnelStatus:
        try:
            self.process.stop()
        except ProcessError as exc:
            return TunnelStatus(self.process.status(), True, str(exc))
        return TunnelStatus(self.process.status(), True)

    def status(self) -> TunnelStatus:
        try:
            configured = bool(self.profile.ssh_host and self.profile.ssh_user and self.profile.ssh_identity_file and self.profile.ssh_known_hosts)
            if configured:
                validate_ssh_profile(self.profile)
            process_status = self.process.status()
            detail = ""
            if process_status.running:
                detail = "SSH 进程运行；仅表示 SSH 进程运行，VPS 路由仍需单独配置"
            elif process_status.pid:
                detail = "SSH 隧道未运行；公网不可用"
            return TunnelStatus(process_status, configured, detail)
        except SSHTunnelError as exc:
            return TunnelStatus(self.process.status(), False, str(exc))


__all__ = ["SSHTunnel", "SSHTunnelError", "TunnelStatus", "build_reverse_command", "normalise_extra_options", "validate_ssh_profile"]
