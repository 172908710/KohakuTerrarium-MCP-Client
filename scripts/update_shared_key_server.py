#!/usr/bin/env python3
"""Upgrade an already deployed v2 gateway without replacing keys/ports/firewalls.

No credentials are embedded. --apply requires root on the VPS and a checked
server/ source bundle beside this script. Without --apply, only preflight runs.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

APP = Path('/opt/kt-mcp-provision')
DB = Path('/var/lib/kt-mcp-provision/registry.sqlite3')
SERVICE = 'kt-mcp-provision.service'
SSH_SERVICE = 'kt-mcp-device-sshd.service'
SERVER_FILES = ('__init__.py', 'app.py', 'registry.py', 'service.py', 'enrollment.py', 'authorized_keys.py', 'enrollment_cli.py')
BASE = Path(__file__).resolve().parent


def run(args, timeout=30):
    result = subprocess.run([str(item) for item in args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{Path(str(args[0])).name} failed (exit {result.returncode}); inspect locally; command output withheld')
    return result.stdout.strip()


def prop(name, field):
    return run(['systemctl', 'show', name, '--property=' + field, '--value'])


def service_env():
    pid = int(prop(SERVICE, 'MainPID'))
    if pid <= 0:
        raise RuntimeError('Provisioning service has no running process')
    pairs = (item.partition(b'=') for item in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0'))
    return {key.decode(): value.decode() for key, sep, value in pairs if sep}


def port_live(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=2):
            return True
    except OSError:
        return False


def api(path, *, body=None, token=None):
    headers = {'Content-Type': 'application/json'} if body is not None else {}
    if token is not None:
        headers['X-KT-Provision-Token'] = token
    request = urllib.request.Request('http://127.0.0.1:18080' + path,
        data=json.dumps(body).encode() if body is not None else None, headers=headers)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        try:
            return exc.code
        finally:
            exc.close()


def protected_hashes():
    result = {}
    for root in [Path('/etc/ssh'), Path('/etc/nginx'), Path('/etc/kt-mcp-provision'),
                 Path('/etc/systemd/system/kt-mcp-provision.service'),
                 Path('/etc/systemd/system/kt-mcp-provision.service.d'),
                 Path('/etc/systemd/system') / SSH_SERVICE]:
        files = [root] if root.is_file() else root.rglob('*') if root.exists() else []
        for file in files:
            if file.is_file():
                result[str(file)] = hashlib.sha256(file.read_bytes()).hexdigest()
    return result


def verify_sources():
    manifest = json.loads((BASE / 'source-sha256.json').read_text(encoding='utf-8'))
    for name in SERVER_FILES:
        relative = 'server/' + name
        source = BASE / relative
        if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != manifest.get(relative):
            raise RuntimeError('Source bundle checksum mismatch: ' + name)
        ast.parse(source.read_text(encoding='utf-8'), filename=name)
    for directory in [APP, APP / 'server']:
        info = directory.stat()
        if directory.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('Installed source parent must be root-owned and not group/world writable')
    for name in SERVER_FILES:
        if (APP / 'server' / name).is_symlink():
            raise RuntimeError('Refusing symlink destination')


def preflight():
    if os.name != 'posix' or os.geteuid() != 0:
        raise RuntimeError('Run on the Linux VPS as root')
    verify_sources()
    if prop(SERVICE, 'ActiveState') != 'active' or prop(SSH_SERVICE, 'ActiveState') != 'active':
        raise RuntimeError('The existing v2 provisioning and customer SSH services must already be running')
    if prop(SERVICE, 'WorkingDirectory') != str(APP) or prop(SERVICE, 'User') != 'kt-mcp-provision':
        raise RuntimeError('Unexpected provisioning service identity/path')
    env = service_env()
    if (env.get('KT_MCP_DEVICE_ENROLLMENT_ENABLED') != '1'
        or not env.get('KT_MCP_PROVISION_ADMIN_TOKEN')
        or env.get('KT_MCP_PROVISION_PORT', '18080') != '18080'
        or Path(env.get('KT_MCP_REGISTRY_DB', str(DB))) != DB or not DB.is_file()):
        raise RuntimeError('Unexpected v2 deployment configuration; no changes made')
    ssh_port = int(env['KT_MCP_DEVICE_SSH_PORT'])
    if not port_live(ssh_port) or api('/healthz') != 200:
        raise RuntimeError('Existing service health check failed')
    run(['/usr/sbin/nginx', '-t'])
    return env, ssh_port


def atomic_install(destination, source, *, mode=0o644, uid=0, gid=0):
    temporary = destination.with_name(destination.name + '.shared-update-' + uuid.uuid4().hex)
    try:
        with temporary.open('xb') as handle:
            handle.write(source.read_bytes()); handle.flush(); os.fsync(handle.fileno())
        temporary.chmod(mode); os.chown(temporary, uid, gid)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def wait_healthy():
    for _ in range(20):
        try:
            if prop(SERVICE, 'ActiveState') == 'active' and api('/healthz') == 200:
                return
        except (OSError, urllib.error.URLError, RuntimeError):
            pass
        time.sleep(0.5)
    raise RuntimeError('Updated provisioning service did not become healthy')


def restore(backup, records):
    run(['systemctl', 'stop', SERVICE])
    for record in records:
        destination = APP / 'server' / record['name']
        if destination.is_symlink():
            raise RuntimeError('Unsafe rollback destination')
        if record['existed']:
            atomic_install(destination, backup / record['name'], mode=record['mode'], uid=record['uid'], gid=record['gid'])
        else:
            destination.unlink(missing_ok=True)
    run(['systemctl', 'start', SERVICE]); wait_healthy()


def apply():
    env, ssh_port = preflight()
    protected = protected_hashes()
    production_live = port_live(18765)
    backup = Path('/root') / ('kt-shared-key-backup-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    backup.mkdir(mode=0o700)
    records = []
    for name in SERVER_FILES:
        file = APP / 'server' / name
        record = {'name': name, 'existed': file.exists()}
        if file.exists():
            info = file.stat()
            record.update(mode=info.st_mode & 0o777, uid=info.st_uid, gid=info.st_gid)
            shutil.copyfile(file, backup / name); (backup / name).chmod(0o600)
        records.append(record)
    (backup / 'files.json').write_text(json.dumps(records, indent=2)); (backup / 'files.json').chmod(0o600)
    print('BACKUP=' + str(backup), flush=True)
    try:
        run(['systemctl', 'stop', SERVICE])
        with sqlite3.connect(DB) as source, sqlite3.connect(backup / 'registry.sqlite3') as target:
            source.backup(target)
        (backup / 'registry.sqlite3').chmod(0o600)
        for name in SERVER_FILES:
            atomic_install(APP / 'server' / name, BASE / 'server' / name)
        # Verify the actual installed code using a temporary registry; no real
        # customer registration, key binding or route reload occurs in this test.
        check = """
import sys, tempfile, base64, struct, secrets
from pathlib import Path
sys.path.insert(0, '/opt/kt-mcp-provision')
from server.enrollment import EnrollmentService, EnrollmentSettings, authorized_key
from server.registry import AllocationRegistry, RegistrySettings
pfx=struct.pack('>I',11)+b'ssh-ed25519'+struct.pack('>I',32)
def pub(): return 'ssh-ed25519 '+base64.b64encode(pfx+secrets.token_bytes(32)).decode()
with tempfile.TemporaryDirectory(prefix='kt-shared-selftest-') as directory:
    root=Path(directory); origin='https://shared-selftest.example.invalid'
    registry=AllocationRegistry(RegistrySettings(root/'db',root/'routes',origin),reload=lambda:None)
    service=EnrollmentService(registry,EnrollmentSettings('ssh.example.invalid','kt-mcp-devices',pub(),2223))
    ports=[]
    for i in (1,2):
        public=pub(); args=('synthetic-test-key',str(i)*32,origin,origin+'/mcp/'+str(i)*43,public)
        response=service.provision_shared(*args,expected_token='synthetic-test-key')
        assert response==service.provision_shared(*args,expected_token='synthetic-test-key')
        assert authorized_key(root/'db','kt-mcp-devices','kt-mcp-devices',public)
        ports.append(response['remote_port'])
    assert len(set(ports))==2
print('INSTALLED_SHARED_KEY_SELFTEST_OK')
"""
        assert run(['/usr/bin/python3', '-I', '-c', check]) == 'INSTALLED_SHARED_KEY_SELFTEST_OK'
        run(['systemctl', 'start', SERVICE]); wait_healthy()
        current = service_env()
        fields = ['KT_MCP_PROVISION_ADMIN_TOKEN', 'KT_MCP_PUBLIC_ORIGIN', 'KT_MCP_DEVICE_ENROLLMENT_ENABLED',
                  'KT_MCP_DEVICE_SSH_HOST', 'KT_MCP_DEVICE_SSH_PORT', 'KT_MCP_DEVICE_SSH_USER', 'KT_MCP_DEVICE_SSH_HOST_KEY']
        if any(current.get(name) != env.get(name) for name in fields):
            raise RuntimeError('Existing key or connection settings changed unexpectedly')
        with sqlite3.connect(DB) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shared_key_device_bindings'").fetchone():
                raise RuntimeError('New schema was not initialized by the running service')
        # The real HTTP service accepts this shared credential and rejects the
        # invalid origin before any transaction or route can be created.
        body = {'origin': 'https://not-your-origin.invalid', 'installation_id': 'a' * 32,
                'endpoint_url': 'https://not-your-origin.invalid/mcp/' + 'Q' * 43,
                'enrollment_version': 2, 'ssh_public_key': env['KT_MCP_DEVICE_SSH_HOST_KEY']}
        if api('/provision', body=body, token=env['KT_MCP_PROVISION_ADMIN_TOKEN']) != 400:
            raise RuntimeError('Live shared-key HTTP path did not reject the invalid origin as expected')
        if protected_hashes() != protected or not port_live(ssh_port):
            raise RuntimeError('Protected settings or original customer listener changed')
        if production_live and not port_live(18765):
            raise RuntimeError('Original production forward became unavailable')
    except BaseException:
        try:
            restore(backup, records)
            print('ROLLED_BACK_CODE; additive database tables retained; original key unchanged', flush=True)
        except Exception:
            print('ROLLBACK_REQUIRES_OPERATOR: ' + str(backup), file=sys.stderr, flush=True)
        raise
    print('SHARED_KEY_SERVER_UPDATE_PASSED')
    print('EXISTING_VERIFICATION_KEY_UNCHANGED=true')
    print('CUSTOMER_SSH_PORT=' + str(ssh_port))
    print('SSH_NGINX_FIREWALL_CONFIG_UNCHANGED=true')
    print('REAL_CUSTOMER_PUBLIC_MCP_VERIFIED=false')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.apply:
            apply()
        else:
            _, port = preflight()
            print(json.dumps({'preflight_passed': True, 'changes_made': False, 'customer_ssh_port': port}))
    except Exception as exc:
        print('SHARED_KEY_UPDATE_NOT_ACCEPTED: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
