# VPS deployment record

The independent provisioning service deployed on the VPS uses the source in
this directory. During the 2026-10-21 verification, the deployed files under
`/opt/kt-mcp-provision/server/` had the same SHA-256 digests as the checked-in
`server/app.py`, `server/registry.py`, `server/service.py`, and
`server/requirements.txt`.

The deployment shape is intentionally documented without copying live secrets
or state into this repository:

- systemd unit: `/etc/systemd/system/kt-mcp-provision.service`;
- working directory: `/opt/kt-mcp-provision`;
- service account: `kt-mcp-provision`;
- loopback provisioning listener: `127.0.0.1:18080`;
- registry and generated route map: `/var/lib/kt-mcp-provision/`;
- Nginx includes the generated route map inside the existing HTTPS server block;
- production `/mcp/` remains routed to `127.0.0.1:18765`;
- the independent device allocation starts at `127.0.0.1:18766`.

Use `kt-mcp-provision.service.example`, `nginx-provisioning.conf.example`,
and `sshd-device-example.conf` as sanitized deployment templates. The live
environment file, admin token, registry database, route map, SSH public/private
keys, and rollback backups are intentionally excluded from GitHub. They must
remain operator-managed on the VPS and must never be reconstructed from this
file.

The deployment was checked without changing the production Bridge: the
provisioning service was active, and the independent and production public MCP
endpoints both passed `initialize`, `tools/list`, and a read-only `tree` call.
