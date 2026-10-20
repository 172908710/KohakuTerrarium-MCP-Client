# Server-side provisioning scaffold

This directory is a **deployment artifact**. It contains the small service that
the Windows GUI expects at `POST /provision` and a persistent per-device route
registry. The live VPS installation is operator-managed and must be kept in
sync with this code.

## Current verified deployment

The independent provisioning service is deployed on the configured VPS and
currently runs as `kt-mcp-provision.service` from `/opt/kt-mcp-provision` under
the dedicated `kt-mcp-provision` account. The API listens only on the VPS
loopback at `127.0.0.1:18080`; Nginx proxies the public `/provision` route to
that listener and includes the generated server-context route map.

The deployed Python sources have been compared against this directory and
match `app.py`, `registry.py`, `service.py`, and `requirements.txt`. The first
independent device uses remote loopback port `18766` and a restricted SSH
account; the existing production MCP continues to use `127.0.0.1:18765`.
The current device passed public MCP `initialize`, `tools/list`, and a default
read-only `tree` call. The production endpoint passed the same checks.

Live environment values, the admin token, registry database, generated
`routes.map`, SSH keys, and rollback backups are deliberately not stored in
Git. See `DEPLOYED.md` for the sanitized deployment record and rollback
boundary.

## Contract

Request:

```http
POST /provision
Content-Type: application/json

{"origin":"https://mcp.example.test","installation_id":"32-lowercase-hex-characters","endpoint_url":"https://mcp.example.test/mcp/<secret-from-local-KT>"}
```

Successful response:

```json
{"url":"https://mcp.example.test/mcp/<secret-from-local-KT>","remote_port":18766}
```

The local KT endpoint URL is the only source of the MCP secret; the response
repeats that exact endpoint URL and never substitutes a server-generated secret.
The service validates that `endpoint_url` is an HTTPS `/mcp/<secret>` URL on the
requested origin, registers that exact secret on first use, and never generates
a replacement. Repeated requests with the same `installation_id` are idempotent
only when the secret is unchanged; a reused secret or changed installation
secret is rejected. It refuses origins other than `KT_MCP_PUBLIC_ORIGIN`,
refuses the production remote port `18765`, and returns no URL until the
generated route include is written and the Nginx reload hook succeeds. The API
does not create or authorize the SSH reverse tunnel; the client and SSH policy
must use the returned `remote_port`.

The API must always be protected by `KT_MCP_PROVISION_ADMIN_TOKEN`; the
service refuses to start or serve provisioning when the token is empty. A
working `KT_MCP_RELOAD_HELPER` is also required before the registry will issue
any device URL; route-map writes without a successful reverse-proxy reload are
rolled back and fail closed.

## Production secret reservation

Set `KT_MCP_RESERVED_SECRET_HASHES` to a comma-separated list of lowercase or
uppercase SHA-256 hex digests of production endpoint secrets. The registry hashes
each candidate secret and rejects a matching candidate without storing, logging,
or returning the production secret. Configure digests only; do not put the
production URL or secret in this environment file, the database, or the route
include. Invalid digest syntax is rejected at startup.

## What this does not do

- It does not connect to, inspect, or modify the existing production Bridge.
- It does not create SSH keys or configure `sshd`.
- It does not automatically discover which Windows device owns a tunnel.
- It does not make an unconfigured `/mcp/<secret>` route work.
- It is not safe to expose on the public Internet without enrollment/authentication,
  filesystem permissions, TLS, rate limiting, backups, and an operator review.

The desktop client reads `KT_MCP_PROVISION_URL` (optional HTTPS override) and
requires the matching `KT_MCP_PROVISION_TOKEN` header value. The deployment
operator must provision that value out-of-band; it is never generated from the
endpoint secret and is never copied from the production Bridge.

## Example local test

From the project root:

```powershell
$env:PYTHONPATH = "$PWD"
$env:KT_MCP_PROVISION_ADMIN_TOKEN = "<separate-admin-token>"
$env:KT_MCP_RELOAD_HELPER = "C:\path\to\validated-nginx-reload-helper.exe"
$env:KT_MCP_PROVISION_BIND = "127.0.0.1"
$env:KT_MCP_PROVISION_PORT = "8088"
python -m server.service
```

The reload helper must exist and return success only after the reviewed proxy
configuration passes validation and reload. For production, use a dedicated
service account and protected directories, for example:

```text
/var/lib/kt-mcp-provision/registry.sqlite3
/var/lib/kt-mcp-provision/routes.map
```

Do not put those files in the Windows client profile or in the existing
`KT-MCP-Bridge` tree.

The service account must be able to read/write the route-map directory; Nginx
only needs to read the generated map. The configured reload helper must be
executable by that account (for example, place it under `/usr/local/bin`, not
an inaccessible `0750 root:root` directory) and may delegate only `nginx -t`
and `systemctl reload nginx` through a narrowly scoped sudo rule.

## Required reverse-proxy design

The registry-generated `routes.map` is a complete **Nginx server-context
include**, not an `http {}` `map` fragment. Put this exact include inside the
existing HTTPS `server { ... }` block, before or alongside the existing
production location:

```nginx
include /var/lib/kt-mcp-provision/routes.map;
```

The existing production `/mcp/` prefix location must remain in that same server
block, with its `proxy_pass` unchanged (production is `127.0.0.1:18765`). The
exact-match blocks generated for registered secrets proxy only to their
allocated `127.0.0.1:<port>` and include the existing MCP proxy headers (`Host`,
`X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto`, HTTP/1.1 upgrade/connection,
long read timeout, and disabled buffering).

For a request whose path is not one of the generated exact matches, Nginx uses
the existing production prefix location. The production Bridge must return
HTTP 404 for unknown `/mcp/<path>` routes; verify that behavior as part of the
staging and production smoke test. Do not add a broad device fallback, do not
replace `proxy_pass` in the production location, and do not turn the include
into an `http`-context `map`.

Example shape of a generated include:

```nginx
location = /mcp/<secret> {
    proxy_pass http://127.0.0.1:18766;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 86400;
    proxy_buffering off;
}
```

Validate the complete active configuration with `nginx -t` before any reload.
The route-map writer atomically publishes the include and restores it if the
reload helper fails, so the service remains fail closed.

## SSH reverse-forward requirements

Each new Windows client must use a distinct reverse forward, such as:

```text
VPS 127.0.0.1:18766  <-SSH reverse forward-  Windows 127.0.0.1:8865
```

The tunnel-only account must be restricted to the exact allocated listen port
(`PermitListen 127.0.0.1:18766`, `GatewayPorts no`, no shell, no TCP local
forward, no agent/X11 forwarding). Add one account/key or an equivalent
independently revocable credential per device. Never copy the production
private key or edit its `18765` authorization in place.

## Deployment gate

Before considering this usable, an operator must supply and verify:

1. VPS access and a reviewed backup/rollback plan;
2. a separate service account and protected database/map directories;
3. authenticated enrollment policy and rate limiting;
4. the server-context include inside the existing server block, preserving the
   production `proxy_pass` and verifying unknown paths reach production and
   return 404;
5. SSH `PermitListen` entries for each allocated port;
6. one test Windows device whose local runtime is independently configured;
7. tests proving two installation IDs receive different secrets/ports, repeated
   provisioning is idempotent, unknown routes return 404, and the production
   endpoint still reaches only the production forward.

For any new device or VPS, all seven gates must be rechecked before enrollment.
The current deployment has completed them for the registered device; a failed
or missing deployment still makes the GUI show “provisioning API 未配置” rather
than fabricate an address.
