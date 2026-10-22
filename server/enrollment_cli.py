"""Local operator commands; never install these as unauthenticated HTTP routes.

Issue prints a customer's activation key once. Keep its output private. Revoke
blocks future registration/authentication, but does not kill existing tunnels.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sqlite3
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.enrollment import issue_key, revoke_key, revoke_shared_device


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Issue/list/revoke customer verification keys")
    parser.add_argument("--db", type=Path, default=Path("/var/lib/kt-mcp-provision/registry.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue")
    issue.add_argument("--label", required=True)
    issue.add_argument("--valid-hours", type=int, default=168)
    commands.add_parser("list")
    revoke = commands.add_parser("revoke")
    revoke.add_argument("--activation-id", required=True)
    commands.add_parser("shared-devices")
    revoke_device = commands.add_parser("revoke-device")
    revoke_device.add_argument("--installation-id", required=True)
    args = parser.parse_args(argv)
    if not args.db.is_file():
        parser.error("the existing provisioning registry is required")
    try:
        if args.command == "issue":
            key, identifier = issue_key(args.db, args.label, valid_hours=args.valid_hours)
            print(json.dumps({"activation_id": identifier, "label": args.label,
                              "verification_key": key, "valid_hours_before_first_use": args.valid_hours}))
        elif args.command == "revoke":
            revoked = revoke_key(args.db, args.activation_id)
            print(json.dumps({"revoked": revoked, "existing_sessions_terminated": False}))
            return 0 if revoked else 1
        elif args.command == "revoke-device":
            revoked = revoke_shared_device(args.db, args.installation_id)
            print(json.dumps({"revoked": revoked, "existing_sessions_terminated": False}))
            return 0 if revoked else 1
        else:
            uri = args.db.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=5) as connection:
                connection.row_factory = sqlite3.Row
                if args.command == "shared-devices":
                    rows = connection.execute("""SELECT a.installation_id,a.created_at,a.revoked,d.remote_port,d.enabled
                        FROM shared_key_device_bindings a JOIN devices d ON d.installation_id=a.installation_id
                        ORDER BY d.remote_port""").fetchall()
                else:
                    rows = connection.execute("SELECT token_hash AS activation_id,label,created_at,expires_at,revoked,installation_id FROM device_activation_keys ORDER BY created_at DESC").fetchall()
            print(json.dumps([dict(row) for row in rows], ensure_ascii=True))
    except Exception:
        print("Activation management failed; check database/schema/permissions. No credentials were printed.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
