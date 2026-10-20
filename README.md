# KohakuTerrarium MCP Client

Independent Windows MCP client, portable runtime, provisioning service, and
SSH reverse-tunnel tooling. The project keeps the production MCP Bridge and
its `18765` route separate from the independent device route (`18766`).

## Current Status

The independent provisioning service is deployed on the configured VPS and
the registered device has passed public MCP `initialize`, `tools/list`, and a
read-only `tree` call. The production endpoint passed the same checks. The
deployed server sources match `server/app.py`, `server/registry.py`,
`server/service.py`, and `server/requirements.txt` in this repository.

Deployment details and rollback boundaries are documented in
[`server/README.md`](server/README.md) and [`server/DEPLOYED.md`](server/DEPLOYED.md).

## Windows Client

Use the complete portable ZIP and double-click `Start-KT-MCP-Client.vbs`, or
build the single-file launcher with `scripts/build_single_exe.py`. The GUI
keeps the endpoint flow compact: local KT setup, provisioning, SSH tunnel, and
actual public MCP verification before showing an address. Each device must
enter its authorized HTTPS origin and matching verification key; the key is
stored only in the local DPAPI-protected profile.

On a clean computer, first-run initialization discovers that computer's
available drive roots and stores them in its own profile. Existing profiles
are not overwritten. The GUI does not display the local directory list.

See [`README_中文.md`](README_中文.md) for the Chinese guide and
[`README_便携版.md`](README_便携版.md) for the portable distribution guide.

## Source Safety

Runtime binaries, `data/`, credentials, SSH keys, registry databases, route
maps, and rollback backups are intentionally excluded from Git. The live VPS
environment file and provisioning admin token are never stored here.
