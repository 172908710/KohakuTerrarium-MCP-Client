"""Configuration and strict isolation for the independent KT MCP client."""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import hmac
import json
import ntpath
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

CONFIG_ENV = "KT_MCP_CLIENT_CONFIG_DIR"
CONFIG_FILENAME = "profile.json"
DEFAULT_LOCAL_PORT = 8865
DEFAULT_REMOTE_FORWARD_PORT = 18766
PRODUCTION_LOCAL_PORT = 8765
PRODUCTION_REMOTE_FORWARD_PORT = 18765
DEFAULT_MCP_PATH = "/mcp/"
SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
WORKSPACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
PRODUCTION_BRIDGE_ROOT = "C:/Users/Administrator/AppData/Local/KT-MCP-Bridge"
PRODUCTION_BRIDGE_RUNTIME_ROOT = PRODUCTION_BRIDGE_ROOT + "/env"


def _workspace_item(value: Any) -> tuple[str, str] | None:
    """Coerce one persisted workspace record to ``(workspace_id, path)``.

    The native KT registry uses ``workspace_id``/``path``.  Accepting the
    legacy ``id``/``workspace_path`` spellings and two-item sequences keeps
    hand-authored profiles and older development fixtures readable without
    changing the on-disk canonical form.
    """

    if isinstance(value, Mapping):
        workspace_id = value.get("workspace_id", value.get("id", ""))
        path = value.get("path", value.get("workspace_path", ""))
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        workspace_id, path = value
    else:
        return None
    return str(workspace_id or "").strip(), str(path or "").strip()


class ConfigError(ValueError):
    """Raised when a profile or path cannot be used safely."""


@dataclass
class DeviceProfile:
    display_name: str = "KT MCP 设备"
    workspace_path: str = ""
    workspace_id: str = "default"
    kt_python: str = ""
    kt_home: str = ""
    public_origin: str = ""
    local_port: int = DEFAULT_LOCAL_PORT
    remote_forward_host: str = "127.0.0.1"
    remote_forward_port: int = DEFAULT_REMOTE_FORWARD_PORT
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_identity_file: str = ""
    ssh_known_hosts: str = ""
    ssh_extra_options: list[str] = field(default_factory=list)
    auto_reconnect: bool = True
    mcp_path: str = DEFAULT_MCP_PATH
    secret: str = field(default="", repr=False)
    # The provisioning token is stored DPAPI-protected in profile.json.  It is
    # never included in public_dict() or passed to child processes.
    provision_token: str = field(default="", repr=False, compare=False)
    endpoint_url: str = field(default="", repr=False, compare=False)
    endpoint_verified: bool = field(default=False, repr=False, compare=False)
    provenance: bool = field(default=False, repr=False, compare=False)
    proof: str = field(default="", repr=False, compare=False)
    tools_config: str = ""
    # Native KT supports a registry of named directories.  Keep the original
    # single workspace_path/workspace_id fields for callers and old profiles;
    # the first entry is mirrored into those fields when a list is supplied.
    # Appending this field preserves the positional constructor order used by
    # older callers.
    workspaces: list[dict[str, str]] = field(default_factory=list)
    _endpoint_source_verified: bool = field(default=False, init=False, repr=False, compare=False)
    _verified_origin: str = field(default="", init=False, repr=False, compare=False)
    _verified_kt_home: str = field(default="", init=False, repr=False, compare=False)
    _verified_local_port: int | None = field(default=None, init=False, repr=False, compare=False)
    _verified_secret: str = field(default="", init=False, repr=False, compare=False)
    _active_runtime_proof: str | None = field(default=None, init=False, repr=False, compare=False)
    _last_provisioned_remote_port: int | None = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if isinstance(self.workspaces, tuple):
            self.workspaces = list(self.workspaces)
        if not isinstance(self.workspaces, list) or not self.workspaces:
            return
        first = _workspace_item(self.workspaces[0])
        if first is None:
            return
        workspace_id, workspace_path = first
        if not self.workspace_path and workspace_path:
            self.workspace_path = workspace_path
        if (not self.workspace_id or self.workspace_id == "default") and workspace_id:
            self.workspace_id = workspace_id

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DeviceProfile":
        names = {
            "display_name", "workspace_path", "workspace_id", "workspaces", "kt_python", "kt_home", "public_origin",
            "local_port", "remote_forward_host", "remote_forward_port", "ssh_host", "ssh_port",
            "ssh_user", "ssh_identity_file", "ssh_known_hosts", "ssh_extra_options", "auto_reconnect",
            "mcp_path", "tools_config",
        }
        data = {key: value[key] for key in names if key in value}
        options = data.get("ssh_extra_options", [])
        if options is None:
            options = []
        if not isinstance(options, (list, tuple)):
            raise ConfigError("ssh_extra_options 必须是列表")
        data["ssh_extra_options"] = [str(item) for item in options]
        return cls(**data)

    def public_dict(self) -> dict[str, Any]:
        return public_dict(self)

    def mcp_url(self) -> str:
        return mcp_url(self)

    def validate(self) -> list[str]:
        return validate(self)


Profile = DeviceProfile


def default_config_dir() -> Path:
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "KohakuTerrarium" / "MCPClientNext"
        return Path.home() / "AppData" / "Local" / "KohakuTerrarium" / "MCPClientNext"
    return Path.home() / ".local" / "share" / "KohakuTerrarium" / "MCPClientNext"


def _resolved(value: str | os.PathLike[str]) -> Path:
    try:
        return Path(value).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ConfigError(f"路径无效：{value}") from exc


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _windows_normalized(value: str | os.PathLike[str]) -> str:
    try:
        text = os.fspath(value).replace("/", "\\")
        return ntpath.normcase(ntpath.normpath(ntpath.abspath(text)))
    except (TypeError, ValueError):
        return ""


def _contains_bridge_home(path: str | os.PathLike[str]) -> bool:
    candidate = _windows_normalized(path)
    forbidden = _windows_normalized(PRODUCTION_BRIDGE_ROOT)
    if candidate and (candidate == forbidden or candidate.startswith(forbidden + "\\")):
        return True
    try:
        parts = _resolved(path).parts
    except ConfigError:
        return True
    return any(part.casefold() in {"kt-mcp-bridge", "kt mcp bridge"} for part in parts)


def _current_bridge_python() -> str:
    for name in ("KT_MCP_BRIDGE_PYTHON", "KT_MCP_BRIDGE_ENV_PYTHON", "KT_BRIDGE_PYTHON"):
        value = os.environ.get(name, "").strip()
        if value:
            return _windows_normalized(value)
    return _windows_normalized(sys.executable)


def _is_production_python(path: str | os.PathLike[str]) -> bool:
    candidate = _windows_normalized(path)
    forbidden_roots = (_windows_normalized(PRODUCTION_BRIDGE_ROOT), _windows_normalized(PRODUCTION_BRIDGE_RUNTIME_ROOT))
    return bool(candidate and any(candidate == root or candidate.startswith(root + "\\") for root in forbidden_roots if root))


def canonicalize_config_dir(path: str | os.PathLike[str] | None = None) -> Path:
    root = _resolved(default_config_dir() if path is None else path)
    if _contains_bridge_home(root):
        raise ConfigError("配置根不得位于当前 KT-MCP-Bridge 目录")
    return root


def config_file(path: str | os.PathLike[str] | None = None) -> Path:
    if path is None:
        return canonicalize_config_dir() / CONFIG_FILENAME
    candidate = Path(path).expanduser()
    if candidate.suffix.casefold() == ".json":
        root = canonicalize_config_dir(candidate.parent)
        return (root / candidate.name).resolve(strict=False)
    return canonicalize_config_dir(candidate) / CONFIG_FILENAME


def canonicalize_runtime_root(path: str | os.PathLike[str], config_root: str | os.PathLike[str] | None = None) -> Path:
    runtime = _resolved(path)
    root = canonicalize_config_dir(config_root) if config_root is not None else canonicalize_config_dir(runtime.parent)
    if _contains_bridge_home(runtime):
        raise ConfigError("runtime root 不得位于当前 KT-MCP-Bridge 目录")
    if runtime == root or not _is_relative_to(runtime, root):
        raise ConfigError("runtime root 必须位于独立配置根内且不能等于配置根")
    return runtime


def canonicalize_kt_home(path: str | os.PathLike[str], config_root: str | os.PathLike[str] | None = None) -> Path:
    home = _resolved(path)
    if _contains_bridge_home(home):
        raise ConfigError("kt_home 不得使用当前 KT-MCP-Bridge 目录")
    if config_root is not None and home == canonicalize_config_dir(config_root):
        raise ConfigError("配置根和 kt_home 必须不同")
    return home


def canonicalize_workspace(path: str | os.PathLike[str]) -> Path:
    workspace = _resolved(path)
    if _contains_bridge_home(workspace):
        raise ConfigError("workspace 不得位于当前 KT-MCP-Bridge 目录")
    return workspace


def workspace_entries(profile: DeviceProfile) -> list[tuple[str, str]]:
    """Return the configured native KT workspace registry entries.

    Profiles written before multi-workspace support only have
    ``workspace_id``/``workspace_path``; synthesize one entry for those
    profiles.  New profiles persist a list of objects using the native
    ``workspace_id``/``path`` names.
    """

    raw = profile.workspaces
    if raw:
        if not isinstance(raw, list):
            raise ConfigError("workspaces 必须是列表")
        entries: list[tuple[str, str]] = []
        for item in raw:
            parsed = _workspace_item(item)
            if parsed is None:
                raise ConfigError("workspaces 中的记录必须包含 workspace_id 和 path")
            entries.append(parsed)
        return entries
    if profile.workspace_path or profile.workspace_id not in {"", "default"}:
        return [(str(profile.workspace_id or ""), str(profile.workspace_path or ""))]
    return []


def canonicalize_kt_python(path: str | os.PathLike[str]) -> Path:
    python_path = _resolved(path)
    if _is_production_python(python_path):
        raise ConfigError("kt_python 不得使用生产 KT-MCP-Bridge runtime")
    return python_path


class _DATA_BLOB(ctypes.Structure):  # pragma: no cover - Windows-only
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _dpapi_protect(raw: bytes) -> bytes:  # pragma: no cover - Windows-only
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    source_buffer = ctypes.create_string_buffer(raw)
    source = _DATA_BLOB(len(raw), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    target = _DATA_BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def _dpapi_unprotect(raw: bytes) -> bytes:  # pragma: no cover - Windows-only
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    source_buffer = ctypes.create_string_buffer(raw)
    source = _DATA_BLOB(len(raw), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    target = _DATA_BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target)):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def _protect_secret(secret: str) -> str:
    if not secret:
        return ""
    raw = secret.encode("utf-8")
    if os.name != "nt":
        return "base64-fallback:" + base64.b64encode(raw).decode("ascii")
    try:
        return "dpapi:" + base64.b64encode(_dpapi_protect(raw)).decode("ascii")
    except Exception as exc:  # pragma: no cover
        raise ConfigError("Windows DPAPI 保护 secret 失败；未写入配置") from exc


def _unprotect_secret(value: str) -> str:
    if not value:
        return ""
    try:
        encoded = value.split(":", 1)[1]
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
        if value.startswith("base64-fallback:"):
            return raw.decode("utf-8")
        if value.startswith("dpapi:"):
            if os.name != "nt":
                raise ConfigError("配置使用 Windows DPAPI，当前系统无法解密")
            return _dpapi_unprotect(raw).decode("utf-8")
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError("配置中的 secret 无法解码") from exc
    raise ConfigError("配置中的 secret 不是受支持的保护格式")


def _string(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _valid_port(value: Any) -> bool:
    try:
        return 1 <= int(value) <= 65535
    except (TypeError, ValueError):
        return False


def canonicalize_origin(origin: str) -> str:
    """Return the canonical HTTPS origin used by KT's endpoint records."""

    text = _string(origin)
    if not text or any(char.isspace() for char in text):
        raise ConfigError("origin 必须是无 path/query/fragment 的 https URL")
    try:
        parsed = urlsplit(text)
        # Accessing these properties validates malformed bracketed hosts and
        # out-of-range ports before the origin is retained.
        _ = parsed.port
        hostname = parsed.hostname
    except (TypeError, ValueError) as exc:
        raise ConfigError("origin 必须是无 path/query/fragment 的 https URL") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or not hostname
        or "\\" in parsed.netloc
        or parsed.netloc.endswith(":")
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("origin 必须是无 path/query/fragment 的 https URL")
    # Host names are case-insensitive, and upstream KT canonicalizes netlocs
    # with lower-case host text when writing connection.json.
    return f"https://{parsed.netloc.lower()}"


def _validate_origin(origin: str) -> bool:
    try:
        canonicalize_origin(origin)
    except ConfigError:
        return False
    return True


def _validate_host(host: str) -> bool:
    return bool(host and not any(char.isspace() for char in host) and not host.startswith("-") and re.fullmatch(r"[A-Za-z0-9_.:\-\[\]]+", host))


def validate_runtime(profile: DeviceProfile, config_root: str | os.PathLike[str] | None = None) -> list[str]:
    errors: list[str] = []
    try:
        if not _string(profile.kt_python):
            errors.append("kt_python 未配置")
        elif not canonicalize_kt_python(profile.kt_python).is_file():
            errors.append("kt_python 不存在或不是文件")
        if not _string(profile.kt_home):
            errors.append("kt_home 未配置")
        else:
            canonicalize_kt_home(profile.kt_home, config_root)
    except ConfigError as exc:
        errors.append(str(exc))
    return errors


def validate(profile: DeviceProfile, config_root: str | os.PathLike[str] | None = None) -> list[str]:
    errors: list[str] = []
    root: Path | None = None
    if config_root is not None:
        try:
            root = canonicalize_config_dir(config_root)
        except ConfigError as exc:
            errors.append(str(exc))
    if not _string(profile.display_name):
        errors.append("display_name 不能为空")
    try:
        configured_workspaces = workspace_entries(profile)
    except ConfigError as exc:
        configured_workspaces = []
        errors.append(str(exc))
    if not configured_workspaces:
        errors.append("workspace_path 未配置")
    seen_ids: set[str] = set()
    seen_paths: set[Path] = set()
    for index, (workspace_id, workspace_path) in enumerate(configured_workspaces):
        label = f"workspaces[{index}]"
        if not WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
            errors.append(
                f"{label}.workspace_id 必须匹配 [A-Za-z0-9][A-Za-z0-9_.-]{{0,63}}"
            )
        elif workspace_id in seen_ids:
            errors.append(f"{label}.workspace_id 重复：{workspace_id}")
        else:
            seen_ids.add(workspace_id)
        if not _string(workspace_path):
            errors.append(f"{label}.path 未配置")
            continue
        try:
            canonical_path = canonicalize_workspace(workspace_path)
            if not canonical_path.is_dir():
                errors.append(f"{label}.path 不存在或不是目录")
            elif canonical_path in seen_paths:
                errors.append(f"{label}.path 重复：{workspace_path}")
            else:
                seen_paths.add(canonical_path)
        except ConfigError as exc:
            errors.append(str(exc))
    if not _string(profile.kt_python):
        errors.append("kt_python 未配置")
    else:
        try:
            if not canonicalize_kt_python(profile.kt_python).is_file():
                errors.append("kt_python 不存在或不是文件")
        except ConfigError as exc:
            errors.append(str(exc))
    if not _string(profile.kt_home):
        errors.append("kt_home 未配置")
    else:
        try:
            canonicalize_kt_home(profile.kt_home, root)
        except ConfigError as exc:
            errors.append(str(exc))
    if not _validate_origin(_string(profile.public_origin)):
        errors.append("public_origin 必须是无 path/query/fragment 的 https URL")
    if not _valid_port(profile.local_port):
        errors.append("local_port 必须是 1-65535 的端口")
    elif int(profile.local_port) == PRODUCTION_LOCAL_PORT:
        errors.append("local_port 不得使用当前生产端口 8765")
    if _string(profile.remote_forward_host) != "127.0.0.1":
        errors.append("remote_forward_host 必须固定为 127.0.0.1")
    if not _valid_port(profile.remote_forward_port):
        errors.append("remote_forward_port 必须是 1-65535 的端口")
    elif int(profile.remote_forward_port) == PRODUCTION_REMOTE_FORWARD_PORT:
        errors.append("remote_forward_port 不得使用当前生产端口 18765")
    if not _valid_port(profile.ssh_port):
        errors.append("ssh_port 必须是 1-65535 的端口")
    if _string(profile.mcp_path) != DEFAULT_MCP_PATH:
        errors.append("mcp_path 固定为 /mcp/")
    if profile.secret and not SECRET_PATTERN.fullmatch(_string(profile.secret)):
        errors.append("secret 必须是 KT URL-safe token（43-128 位）")
    ssh_values = {
        name: _string(getattr(profile, name))
        for name in ("ssh_host", "ssh_user", "ssh_identity_file", "ssh_known_hosts")
    }
    if any(ssh_values.values()):
        missing = [name for name, value in ssh_values.items() if not value]
        if missing:
            errors.append("SSH 配置不完整：缺少 " + ", ".join(missing))
        elif not _validate_host(ssh_values["ssh_host"]):
            errors.append("SSH 配置：ssh_host 无效")
        if ssh_values["ssh_user"] and any(char.isspace() for char in ssh_values["ssh_user"]):
            errors.append("SSH 配置：ssh_user 无效")
        for name, label in (("ssh_identity_file", "私钥文件"), ("ssh_known_hosts", "known_hosts 文件")):
            if ssh_values[name]:
                try:
                    if not _resolved(ssh_values[name]).is_file():
                        errors.append(f"SSH 配置：{label}不存在或不是文件")
                except ConfigError as exc:
                    errors.append(str(exc))
    if not isinstance(profile.ssh_extra_options, (list, tuple)):
        errors.append("ssh_extra_options 必须是列表")
    if profile.tools_config:
        try:
            tools = _resolved(profile.tools_config)
            if _contains_bridge_home(tools) or (root is not None and not _is_relative_to(tools, root)):
                errors.append("tools_config 必须位于独立配置根内")
        except ConfigError as exc:
            errors.append(str(exc))
    return errors


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def public_dict(profile: DeviceProfile) -> dict[str, Any]:
    entries = workspace_entries(profile)
    return {
        "display_name": profile.display_name,
        "workspace_path": profile.workspace_path,
        "workspace_id": profile.workspace_id,
        "workspaces": [
            {"workspace_id": workspace_id, "path": workspace_path}
            for workspace_id, workspace_path in entries
        ],
        "kt_python": profile.kt_python,
        "kt_home": profile.kt_home,
        "public_origin": profile.public_origin,
        "local_port": _safe_int(profile.local_port, DEFAULT_LOCAL_PORT),
        "remote_forward_host": profile.remote_forward_host,
        "remote_forward_port": _safe_int(profile.remote_forward_port, DEFAULT_REMOTE_FORWARD_PORT),
        "ssh_host": profile.ssh_host,
        "ssh_port": _safe_int(profile.ssh_port, 22),
        "ssh_user": profile.ssh_user,
        "ssh_identity_file": profile.ssh_identity_file,
        "ssh_known_hosts": profile.ssh_known_hosts,
        "ssh_extra_options": list(profile.ssh_extra_options) if isinstance(profile.ssh_extra_options, (list, tuple)) else [],
        "auto_reconnect": bool(profile.auto_reconnect),
        "mcp_path": profile.mcp_path,
        "tools_config": profile.tools_config,
        "secret_configured": bool(profile.secret and SECRET_PATTERN.fullmatch(profile.secret)),
    }


def parse_endpoint_url(value: str) -> tuple[str, str, str]:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
        _ = parsed.port
        hostname = parsed.hostname
    except (TypeError, ValueError) as exc:
        raise ConfigError("KT endpoint URL 必须是无 query/fragment 的 https URL") from exc
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.netloc
        or not hostname
        or "\\" in parsed.netloc
        or parsed.netloc.endswith(":")
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("KT endpoint URL 必须是无 query/fragment 的 https URL")
    try:
        origin = canonicalize_origin(f"https://{parsed.netloc}")
    except ConfigError as exc:
        raise ConfigError("KT endpoint URL 的 origin 无效") from exc
    if not parsed.path.startswith(DEFAULT_MCP_PATH) or parsed.path.count("/") != 2:
        raise ConfigError("KT endpoint URL path 必须固定为 /mcp/<secret>")
    secret = parsed.path[len(DEFAULT_MCP_PATH):]
    if not SECRET_PATTERN.fullmatch(secret):
        raise ConfigError("KT endpoint URL secret 不是合法 URL-safe token")
    return origin, DEFAULT_MCP_PATH, secret


def _endpoint_home_key(value: str | os.PathLike[str]) -> str:
    return _windows_normalized(value) if _string(value) else ""


def _endpoint_identity(profile: DeviceProfile) -> tuple[str, str, int | None]:
    try:
        local_port = int(profile.local_port)
    except (TypeError, ValueError):
        local_port = None
    return (canonicalize_origin(profile.public_origin), _endpoint_home_key(profile.kt_home), local_port)



def endpoint_proof_is_current(profile: DeviceProfile, proof: str | None = None) -> bool:
    stored_proof = getattr(profile, "proof", "")
    if (
        not getattr(profile, "provenance", False)
        or not isinstance(stored_proof, str)
        or not stored_proof
        or not profile.endpoint_verified
        or not profile.endpoint_url
    ):
        return False
    expected_proof = getattr(profile, "_active_runtime_proof", None)
    if not isinstance(expected_proof, str) or not expected_proof:
        return False
    if proof is None:
        supplied_matches = True
    elif not isinstance(proof, str):
        supplied_matches = False
    else:
        supplied_matches = hmac.compare_digest(stored_proof, proof)
    return (
        supplied_matches
        and hmac.compare_digest(stored_proof, expected_proof)
        and _verified_endpoint_is_current(profile)
    )


def _remember_verified_endpoint(profile: DeviceProfile) -> None:
    origin, home, local_port = _endpoint_identity(profile)
    profile._endpoint_source_verified = True
    profile._verified_origin = origin
    profile._verified_kt_home = home
    profile._verified_local_port = local_port
    profile._verified_secret = _string(profile.secret)
    profile.endpoint_verified = True


def invalidate_endpoint(profile: DeviceProfile) -> DeviceProfile:
    profile.endpoint_url = ""
    profile.endpoint_verified = False
    profile.provenance = False
    profile.proof = ""
    profile._endpoint_source_verified = False
    profile._verified_origin = ""
    profile._verified_kt_home = ""
    profile._verified_local_port = None
    profile._verified_secret = ""
    profile._last_provisioned_remote_port = None
    # Keep the runtime registration so this KTRuntime can refresh the URL
    # after an identity-changing edit.  Profiles loaded from disk have no
    # active registration and remain untrusted until a new runtime is built.
    return profile


def _verified_endpoint_is_current(profile: DeviceProfile) -> bool:
    if not profile.endpoint_verified or not SECRET_PATTERN.fullmatch(_string(profile.secret)):
        return False
    if not _validate_origin(_string(profile.public_origin)) or _string(profile.mcp_path) != DEFAULT_MCP_PATH:
        return False
    current = _endpoint_identity(profile)
    if profile._endpoint_source_verified:
        expected = (profile._verified_origin, profile._verified_kt_home, profile._verified_local_port)
        if current != expected or _string(profile.secret) != profile._verified_secret:
            return False
    if profile.endpoint_url:
        try:
            origin, path, secret = parse_endpoint_url(profile.endpoint_url)
        except ConfigError:
            return False
        if origin != current[0] or path != DEFAULT_MCP_PATH or secret != _string(profile.secret):
            return False
    elif not profile._endpoint_source_verified:
        return False
    return True


def set_actual_endpoint(profile: DeviceProfile, value: str, *, proof: str) -> str:
    # Refresh attempts are fail-closed: a malformed, mismatched, or stale
    # endpoint must not leave an older verified URL usable in memory.
    try:
        origin, path, secret = parse_endpoint_url(value)
        expected_origin = canonicalize_origin(profile.public_origin)
        expected_proof = getattr(profile, "_active_runtime_proof", None)
        if origin != expected_origin:
            raise ConfigError("KT endpoint URL 的 origin 与 profile 不一致")
        if (
            not isinstance(proof, str)
            or not proof
            or not isinstance(expected_proof, str)
            or not expected_proof
            or not hmac.compare_digest(proof, expected_proof)
        ):
            raise ConfigError("必须提供当前 KTRuntime session proof")
    except ConfigError:
        invalidate_endpoint(profile)
        raise
    profile.public_origin, profile.mcp_path, profile.secret = origin, path, secret
    profile.endpoint_url = f"{origin}{path}{secret}"
    profile.endpoint_verified = True
    profile.provenance = True
    profile.proof = expected_proof
    _remember_verified_endpoint(profile)
    return profile.endpoint_url


def mcp_url(profile: DeviceProfile) -> str:
    if not endpoint_proof_is_current(profile):
        raise ConfigError("当前进程没有由 KT runtime url() 验证的 endpoint")
    origin, path, secret = parse_endpoint_url(profile.endpoint_url)
    if origin != canonicalize_origin(profile.public_origin) or path != DEFAULT_MCP_PATH or secret != _string(profile.secret) or _string(profile.mcp_path) != DEFAULT_MCP_PATH:
        raise ConfigError("profile endpoint 与独立 KT 实际 endpoint 不一致")
    return f"{origin}{path}{secret}"


def redact_sensitive(text: object) -> str:
    return re.sub(r"https://[^\s\"'<>]+/mcp/[A-Za-z0-9_-]{43,128}", "<redacted-url>", str(text), flags=re.IGNORECASE)


def _profile_from_payload(payload: Mapping[str, Any]) -> tuple[dict[str, Any], str, str, bool]:
    if "profile" in payload:
        raw = payload.get("profile")
        if not isinstance(raw, Mapping):
            raise ConfigError("配置 profile 必须是对象")
        data = dict(raw)
        protected = payload.get("secret_protected", "")
        provision_protected = payload.get("provision_token_protected", "")
    else:
        data = dict(payload)
        protected = data.pop("secret_protected", "")
        provision_protected = data.pop("provision_token_protected", "")
    if "secret" in data and data.get("secret"):
        raise ConfigError("配置包含未保护的 secret；请重新保存配置")
    data.pop("secret", None)
    if not isinstance(protected, str):
        raise ConfigError("secret_protected 格式无效")
    # Older development profiles used an explicit null marker while the
    # field was not yet persisted; treat that as an empty token for migration.
    if provision_protected is None:
        provision_protected = ""
    if not isinstance(provision_protected, str):
        raise ConfigError("provision_token_protected 格式无效")
    return data, protected, provision_protected, bool(payload.get("endpoint_verified", False))


def load(path: str | os.PathLike[str] | None = None) -> DeviceProfile:
    filename = config_file(path)
    if not filename.exists():
        return DeviceProfile(kt_home=str(filename.parent / "kt-home"), tools_config=str(filename.parent / "tools.yaml"))
    try:
        with filename.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError("无法读取 MCP Client 配置") from exc
    if not isinstance(payload, Mapping):
        raise ConfigError("配置根必须是对象")
    data, protected, provision_protected, _verified = _profile_from_payload(payload)
    profile = DeviceProfile.from_mapping(data)
    profile.secret = _unprotect_secret(protected)
    profile.provision_token = _unprotect_secret(provision_protected)
    if profile.kt_home == "":
        profile.kt_home = str(filename.parent / "kt-home")
    if profile.tools_config == "":
        profile.tools_config = str(filename.parent / "tools.yaml")
    invalidate_endpoint(profile)
    return profile


def save(profile: DeviceProfile, path: str | os.PathLike[str] | None = None) -> Path:
    filename = config_file(path)
    root = filename.parent
    if profile.kt_home:
        canonicalize_kt_home(profile.kt_home, root)
    else:
        profile.kt_home = str(root / "kt-home")
    entries = workspace_entries(profile)
    if not entries:
        # Provisioning-only profiles used by older callers may intentionally
        # omit a workspace; runtime validation still rejects them before start.
        profile.workspaces = []
        normalized_entries: list[dict[str, str]] = []
    else:
        normalized_entries = []
    seen_ids: set[str] = set()
    seen_paths: set[Path] = set()
    for workspace_id, workspace_path in entries:
        if not WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
            raise ConfigError("workspace_id 必须匹配 [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
        if not workspace_path:
            raise ConfigError("workspace_path 未配置")
        canonical_path = canonicalize_workspace(workspace_path)
        if workspace_id in seen_ids:
            raise ConfigError(f"workspace_id 重复：{workspace_id}")
        if canonical_path in seen_paths:
            raise ConfigError(f"workspace path 重复：{workspace_path}")
        seen_ids.add(workspace_id)
        seen_paths.add(canonical_path)
        normalized_entries.append({"workspace_id": workspace_id, "path": str(canonical_path)})
    # Keep legacy fields synchronized with the first native registry entry so
    # old callers and status output continue to describe the default path.
    if normalized_entries:
        profile.workspace_id = normalized_entries[0]["workspace_id"]
        profile.workspace_path = normalized_entries[0]["path"]
    profile.workspaces = normalized_entries
    if profile.kt_python:
        canonicalize_kt_python(profile.kt_python)
    if profile.secret and not SECRET_PATTERN.fullmatch(profile.secret):
        raise ConfigError("secret 必须是 KT URL-safe token（43-128 位）")
    if profile.endpoint_url and not endpoint_proof_is_current(profile):
        invalidate_endpoint(profile)
    elif profile.endpoint_verified and not _verified_endpoint_is_current(profile):
        invalidate_endpoint(profile)
    tools = _resolved(profile.tools_config or root / "tools.yaml")
    if _contains_bridge_home(tools) or not _is_relative_to(tools, root) or tools.name != "tools.yaml":
        raise ConfigError("tools_config 必须是独立配置根内的 tools.yaml")
    profile.tools_config = str(tools)
    payload = {
        "version": 3,
        "profile": public_dict(profile),
        "secret_protected": _protect_secret(profile.secret),
        "provision_token_protected": _protect_secret(profile.provision_token),
        "endpoint_verified": bool(profile.endpoint_verified),
    }
    root.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".profile-", suffix=".json", dir=str(root))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, filename)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return filename


LAST_CONNECTION_FILENAME = "last-successful-connection.json"


def load_successful_connection(path: str | os.PathLike[str] | None = None) -> tuple[str, str]:
    """Restore form fields, never an endpoint proof or a ready/running flag.

    Older clients kept only the profile; use that as a one-time compatible
    fallback until a fully verified connection writes its own success record.
    A failed later registration can update the runtime profile without erasing
    this last known-good pair.
    """
    filename = config_file(path).parent / LAST_CONNECTION_FILENAME
    try:
        with filename.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        previous = load(path)
        if not previous.public_origin or not previous.provision_token:
            return "", ""
        origin = canonicalize_origin(previous.public_origin)
        # Migrate once before a failed later registration can mutate profile.
        # This restores legacy input only; it never grants endpoint readiness.
        remember_successful_connection(previous, path)
        return origin, previous.provision_token
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError("无法读取上次成功连接的设置，请重新输入") from exc
    if not isinstance(payload, Mapping) or payload.get("version") != 1:
        raise ConfigError("上次成功连接的设置格式无效，请重新输入")
    protected = payload.get("provision_token_protected")
    if not isinstance(protected, str):
        raise ConfigError("上次成功连接的验证 key 格式无效，请重新输入")
    return canonicalize_origin(payload.get("origin", "")), _unprotect_secret(protected)


def remember_successful_connection(
    profile: DeviceProfile, path: str | os.PathLike[str] | None = None,
) -> Path:
    """Persist only after the complete public MCP verification has succeeded."""
    filename = config_file(path).parent / LAST_CONNECTION_FILENAME
    payload = {
        "version": 1,
        "origin": canonicalize_origin(profile.public_origin),
        "provision_token_protected": _protect_secret(profile.provision_token),
    }
    filename.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".last-connection-", suffix=".json", dir=str(filename.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, filename)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return filename


__all__ = [
    "CONFIG_ENV",
    "CONFIG_FILENAME",
    "DEFAULT_LOCAL_PORT",
    "DEFAULT_REMOTE_FORWARD_PORT",
    "DEFAULT_MCP_PATH",
    "PRODUCTION_LOCAL_PORT",
    "PRODUCTION_REMOTE_FORWARD_PORT",
    "PRODUCTION_BRIDGE_ROOT",
    "PRODUCTION_BRIDGE_RUNTIME_ROOT",
    "SECRET_PATTERN",
    "WORKSPACE_ID_PATTERN",
    "ConfigError",
    "DeviceProfile",
    "Profile",
    "canonicalize_config_dir",
    "canonicalize_kt_home",
    "canonicalize_kt_python",
    "canonicalize_origin",
    "canonicalize_runtime_root",
    "canonicalize_workspace",
    "config_file",
    "default_config_dir",
    "endpoint_proof_is_current",
    "invalidate_endpoint",
    "load",
    "load_successful_connection",
    "remember_successful_connection",
    "LAST_CONNECTION_FILENAME",
    "mcp_url",
    "parse_endpoint_url",
    "public_dict",
    "redact_sensitive",
    "save",
    "set_actual_endpoint",
    "validate",
    "validate_runtime",
    "workspace_entries",
]
