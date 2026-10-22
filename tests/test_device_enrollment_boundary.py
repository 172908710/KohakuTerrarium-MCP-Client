"""Boundary tests and real bundled OpenSSH parsing; no external connections."""
from pathlib import Path
import os
import subprocess
import sys

import pytest

from kt_mcp_client import enrollment as e
from kt_mcp_client.config import DeviceProfile
from kt_mcp_client.ssh_tunnel import build_reverse_command
from server.authorized_keys import main as keys_main
from server.enrollment import EnrollmentService, EnrollmentSettings, issue_key
from server.enrollment_cli import main as activation_main
from server.registry import AllocationRegistry, RegistrySettings

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://customer.example.invalid"


def make_keys(tmp_path):
    private, public = e.ensure_device_key(tmp_path / "新客户 with spaces" / "profile.json")
    _, host_public = e.ensure_device_key(tmp_path / "synthetic-host" / "profile.json")
    return private, public, host_public


def metadata(public, host_public):
    return {"enrollment_version": 2, "ssh": {"host": "ssh.example.invalid", "port": 2222,
        "user": "kt-mcp-devices", "host_key": host_public,
        "client_key_fingerprint": e.fingerprint(public), "remote_bind": "127.0.0.1"}}


@pytest.mark.parametrize("field,bad", [
    ("user", "root"), ("user", "Administrator"), ("user", "-oProxyCommand=bad"),
    ("host", "-oProxyCommand=bad"), ("host", "ssh.example.invalid\nextra"),
    ("port", True), ("port", 0), ("port", 65536), ("port", "22"),
    ("remote_bind", "0.0.0.0"), ("client_key_fingerprint", "wrong-device"),
    ("host_key", "ssh-ed25519 AAAA")])
def test_malicious_response_metadata_rejected(tmp_path, field, bad):
    _, public, host = make_keys(tmp_path)
    value = metadata(public, host)
    value["ssh"][field] = bad
    with pytest.raises(ValueError):
        e._ssh_metadata(value, public)


def test_private_file_is_protected_and_stable(tmp_path):
    private, public, _ = make_keys(tmp_path)
    before = private.read_bytes()
    again, same = e.ensure_device_key(private.parent.parent / "profile.json")
    assert again == private and same == public and again.read_bytes() == before
    if os.name == "nt":
        import win32api, win32con, win32security
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            current_sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
        sd = win32security.GetNamedSecurityInfo(str(private), win32security.SE_FILE_OBJECT,
                                               win32security.DACL_SECURITY_INFORMATION)
        acl = sd.GetSecurityDescriptorDacl()
        assert acl.GetAceCount() == 1
        assert acl.GetAce(0)[2] == current_sid
    else:
        assert private.stat().st_mode & 0o777 == 0o600


def test_existing_corrupt_key_is_not_regenerated(tmp_path):
    private, _, _ = make_keys(tmp_path)
    private.write_bytes(b"not a private key")
    with pytest.raises(ValueError):
        e.ensure_device_key(private.parent.parent / "profile.json")
    assert private.read_bytes() == b"not a private key"


def test_spaced_unicode_paths_and_noninteractive_ssh_options(tmp_path):
    private, public, host = make_keys(tmp_path)
    known = private.parent / "known_hosts"
    e._write_pinned(known, "[ssh.example.invalid]:2222 " + host + "\n")
    profile = DeviceProfile(ssh_host="ssh.example.invalid", ssh_port=2222,
        ssh_user="kt-mcp-devices", ssh_identity_file=str(private), ssh_known_hosts=str(known),
        remote_forward_port=18766, local_port=8865)
    ssh = ROOT / "tools/openssh/ssh.exe"
    cmd = build_reverse_command(profile, ssh_executable=str(ssh))
    assert str(private) in cmd
    for option in ["BatchMode=yes", "IdentitiesOnly=yes", "IdentityAgent=none",
                   "PasswordAuthentication=no", "KbdInteractiveAuthentication=no",
                   "StrictHostKeyChecking=yes"]:
        assert option in cmd
    if os.name != "nt" or not ssh.is_file():
        return
    env = {k:v for k,v in os.environ.items() if not k.startswith(("KT_", "PYTHON"))}
    # -G only parses config and exits. It never contacts the synthetic host.
    parsed = subprocess.run([cmd[0], "-G", *cmd[1:]], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=10, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert parsed.returncode == 0, parsed.stderr
    expected_path = str(known).replace("\\", "/").lower()
    assert expected_path in parsed.stdout.replace("\\", "/").lower()
    # Prove cryptography's private-key format is accepted by the bundled SSH tools.
    checked = subprocess.run([str(ssh.with_name("ssh-keygen.exe")), "-y", "-P", "", "-f", str(private)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert checked.returncode == 0, checked.stderr
    assert e.public_key(checked.stdout.strip()) == public


def test_operator_cli_and_authorized_keys_entrypoint(tmp_path, capsys):
    import json
    registry = AllocationRegistry(RegistrySettings(tmp_path / "db.sqlite3", tmp_path / "routes.map", ORIGIN), reload=lambda: None)
    _, public, host = make_keys(tmp_path)
    service = EnrollmentService(registry, EnrollmentSettings("ssh.example.invalid", "kt-mcp-devices", host, 2222))
    db = str(registry.settings.db_path)
    assert activation_main(["--db", db, "issue", "--label", "customer-one"]) == 0
    issued = json.loads(capsys.readouterr().out)
    assert issued["verification_key"].startswith("ktd_")
    service.provision(issued["verification_key"], "a" * 32, ORIGIN, ORIGIN + "/mcp/" + "Q" * 43, public)
    args = ["--db", db, "--user", "kt-mcp-devices", "--type", "ssh-ed25519", "--key", public.split()[1]]
    assert keys_main(args) == 0
    assert 'permitlisten="127.0.0.1:18766"' in capsys.readouterr().out
    assert activation_main(["--db", db, "list"]) == 0
    assert issued["verification_key"] not in capsys.readouterr().out
    assert activation_main(["--db", db, "revoke", "--activation-id", issued["activation_id"]]) == 0
    capsys.readouterr()
    assert keys_main(args) == 0
    assert capsys.readouterr().out == ""
