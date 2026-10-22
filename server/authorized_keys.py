"""Linux sshd AuthorizedKeysCommand entry point; stdout is public-key data only.

Install this file and its server package root-owned and non-writable by the
HTTP service. Run under kt-mcp-provision (read access to the registry only).
This helper does not accept an administrator password or activation key.
"""
from __future__ import annotations
import argparse
from pathlib import Path
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server.enrollment import authorized_key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restricted device public-key lookup")
    parser.add_argument("--db", type=Path, default=Path("/var/lib/kt-mcp-provision/registry.sqlite3"))
    parser.add_argument("--configured-user", default="kt-mcp-devices")
    parser.add_argument("--user", required=True)
    parser.add_argument("--type", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--port-start", type=int, default=18766)
    parser.add_argument("--port-end", type=int, default=18865)
    args = parser.parse_args(argv)
    try:
        line = authorized_key(args.db, args.user, args.configured_user,
                              args.type + " " + args.key,
                              port_start=args.port_start, port_end=args.port_end)
    except Exception:
        # Never echo credentials, DB data or diagnostics to sshd's key parser.
        return 1
    if line:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
