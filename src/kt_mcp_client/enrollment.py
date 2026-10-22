"""Automatic per-device SSH enrollment. Only the PUBLIC key leaves this machine.

Both the existing shared verification key and per-device ktd_ keys can enroll.
Each computer still owns its own private key. The server must support version 2; this module never downgrades an enrollment
request or copies a production SSH credential to make it succeed.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import struct
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request

from .config import DeviceProfile, config_file, save

ACTIVATION_KEY = re.compile(r"^ktd_[A-Za-z0-9_-]{43}$")
SSH_FIELDS = ("ssh_host", "ssh_user", "ssh_identity_file", "ssh_known_hosts")


def public_key(value: object) -> str:
    """Accept exactly an unadorned Ed25519 public key, not authorized_keys options."""
    if not isinstance(value, str) or len(value) > 256 or "\n" in value or "\r" in value:
        raise ValueError("invalid Ed25519 public key")
    fields = value.split()
    if len(fields) != 2 or fields[0] != "ssh-ed25519":
        raise ValueError("invalid Ed25519 public key")
    try:
        blob = base64.b64decode(fields[1], validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid Ed25519 public key") from exc
    expected_prefix = struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32)
    if len(blob) != len(expected_prefix) + 32 or not blob.startswith(expected_prefix):
        raise ValueError("invalid Ed25519 public key")
    return "ssh-ed25519 " + base64.b64encode(blob).decode("ascii")


def fingerprint(value: str) -> str:
    blob = base64.b64decode(public_key(value).split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


def _protect_path(path: Path, *, directory: bool = False) -> None:
    if os.name != "nt":
        path.chmod(0o700 if directory else 0o600)
        return
    # pywin32 is already bundled with KT. Fail closed rather than create an
    # unprotected key when the Windows ACL APIs are unavailable.
    import win32api
    import win32con
    import win32security
    import ntsecuritycon
    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        token.Close()
    acl = win32security.ACL()
    flags = (win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE) if directory else 0
    acl.AddAccessAllowedAceEx(win32security.ACL_REVISION, flags, ntsecuritycon.FILE_ALL_ACCESS, sid)
    win32security.SetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, acl, None,
    )


def _key_directory(config_path: str | os.PathLike[str] | None) -> Path:
    root = config_file(config_path).parent.resolve()
    directory = root / "device-ssh"
    if directory.resolve() != directory or directory.is_symlink():
        raise ValueError("device SSH directory must not be redirected")
    directory.mkdir(parents=True, exist_ok=True)
    _protect_path(directory, directory=True)
    return directory


def ensure_device_key(config_path: str | os.PathLike[str] | None) -> tuple[Path, str]:
    """Reuse the same private key on retries, even after a lost HTTP response."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    directory = _key_directory(config_path)
    private = directory / "id_ed25519"
    if private.is_symlink() or private.resolve() != private:
        raise ValueError("device private key must not be redirected")
    if not private.exists():
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                serialization.NoEncryption())
        # Exclusive creation: a concurrent launch cannot overwrite a device's identity.
        try:
            fd = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
    _protect_path(private)
    if private.stat().st_size > 16384:
        raise ValueError("invalid device key file")
    key = serialization.load_ssh_private_key(private.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("device key must be Ed25519")
    public = public_key(key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode("ascii"))
    pubfile = private.with_suffix(".pub")
    _write_pinned(pubfile, public + "\n")
    return private, public


def _write_pinned(path: Path, text: str) -> None:
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("device SSH file must not be redirected")
    if path.exists():
        if path.read_text(encoding="ascii") != text:
            raise ValueError("SSH identity or server host key changed; explicit administrator review required")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    _protect_path(path)


def _ssh_metadata(payload: Mapping[str, Any], expected_public: str) -> dict[str, Any]:
    ssh = payload.get("ssh")
    required = {"host", "port", "user", "host_key", "client_key_fingerprint", "remote_bind"}
    if payload.get("enrollment_version") != 2 or not isinstance(ssh, dict) or set(ssh) != required:
        raise ValueError("server did not return the version 2 SSH enrollment contract")
    host, user, port = ssh["host"], ssh["user"], ssh["port"]
    if not isinstance(host, str) or len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("invalid SSH host")
    if not isinstance(user, str) or not re.fullmatch(r"kt-mcp-device[a-z0-9_-]{0,19}", user):
        raise ValueError("server must use a dedicated kt-mcp-device account, never root")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("invalid SSH port")
    if ssh["client_key_fingerprint"] != fingerprint(expected_public) or ssh["remote_bind"] != "127.0.0.1":
        raise ValueError("SSH authorization does not match this device or loopback binding")
    return {**ssh, "host_key": public_key(ssh["host_key"])}


def needs_automatic_enrollment(profile: DeviceProfile, config_path=None) -> bool:
    """Fresh devices auto-enroll; never replace a complete legacy SSH setup.

    A device previously auto-enrolled with the shared key must keep using v2 on
    retries, even though its SSH fields are now populated. File identity here
    selects the protocol; it is not an authentication or endpoint proof.
    """
    if any(not str(getattr(profile, name, "") or "").strip() for name in SSH_FIELDS):
        return True
    expected = config_file(config_path).parent / "device-ssh" / "id_ed25519"
    return Path(profile.ssh_identity_file).expanduser().resolve() == expected.resolve()


def enroll_and_save(origin: str, endpoint_url: str, *, profile: DeviceProfile,
                    config_path=None, opener=None, timeout: float = 15.0) -> str:
    # Deferred import avoids changing the legacy provisioning API's public surface.
    from . import provisioning as p
    origin = p.normalize_origin(origin)
    endpoint = p._validate_current_profile(origin, endpoint_url, profile)
    token = p._registration_token(profile)
    if token.startswith("ktd_") and not ACTIVATION_KEY.fullmatch(token):
        raise p.ProvisioningError("客户验证 key 格式无效；请粘贴完整的 key。")
    url = p.provisioning_url(origin)
    try:
        private, public = ensure_device_key(config_path)
    except Exception as exc:
        raise p.ProvisioningError("无法安全创建或读取本机设备密钥；未发送注册请求。") from exc
    body = json.dumps({"origin": origin, "endpoint_url": endpoint,
                       "installation_id": p.get_installation_id(config_path),
                       "enrollment_version": 2, "ssh_public_key": public}, separators=(",", ":")).encode("utf-8")
    request = Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json", "X-KT-Provision-Token": token})
    response = None
    try:
        response = (opener or p._no_redirect_opener)(request, timeout=timeout)
        status = p._response_status(response)
        if status != 200:
            raise HTTPError(url, status or 502, "enrollment failed", {}, None)
        payload = p._response_payload(p._read_response(response))
        response = None
    except HTTPError as exc:
        messages = {
            400: "服务器未接受自动设备接入协议；请确认服务端已升级。",
            401: "验证 key 无效或设备授权已停用；使用旧 key 时请确认服务端已升级共享 key 自动接入。",
            403: "该设备未获授权。",
            409: "设备身份冲突：ktd_ key 可能已绑定另一台设备，或本机原有公钥不匹配；请保留 data 并核对。",
            422: "服务器仍是旧版注册接口；请先升级服务端自动接入功能。",
            404: "服务器注册接口未部署。",
            503: "服务器自动设备接入未启用或暂不可用；请联系服务端管理员。",
        }
        raise p.ProvisioningError(messages.get(exc.code, f"自动接入请求失败（HTTP {exc.code}）。")) from exc
    except p.ProvisioningError:
        raise
    except (URLError, OSError, ValueError, TypeError) as exc:
        raise p.ProvisioningUnavailable("自动接入通信失败；本机密钥已保留，重试不会更换设备身份。") from exc
    finally:
        if response is not None:
            p._close_response(response)
    try:
        if payload.get("url") != endpoint or ("endpoint_url" in payload and payload["endpoint_url"] != endpoint):
            raise ValueError("server endpoint differs from local KT")
        remote_port = p._parse_remote_port(payload)
        ssh = _ssh_metadata(payload, public)
        known = private.parent / "known_hosts"
        host = ssh["host"] if ssh["port"] == 22 else f"[{ssh['host']}]:{ssh['port']}"
        _write_pinned(known, host + " " + ssh["host_key"] + "\n")
    except (ValueError, OSError) as exc:
        raise p.ProvisioningError("服务器自动接入响应无效、设备不匹配或主机密钥发生变化；未启动隧道。") from exc
    # The token and endpoint remain DPAPI protected by config.save(). The
    # private key is a locally created ACL-protected file, never an API field.
    old = {name: getattr(profile, name) for name in (*SSH_FIELDS, "ssh_port", "remote_forward_host", "remote_forward_port")}
    try:
        profile.ssh_host, profile.ssh_port, profile.ssh_user = ssh["host"], ssh["port"], ssh["user"]
        profile.ssh_identity_file, profile.ssh_known_hosts = str(private), str(known)
        profile.remote_forward_host, profile.remote_forward_port = "127.0.0.1", remote_port
        save(profile, config_path)
    except Exception:
        for name, value in old.items():
            setattr(profile, name, value)
        raise
    return endpoint
