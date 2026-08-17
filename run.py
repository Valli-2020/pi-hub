#!/usr/bin/env python3
"""Pi Hub Dashboard — WOL, shutdown, GRUB dual-boot switching, Proxmox container control.

Entry point: parses CLI arguments and starts the HTTP server.
Python 3 stdlib only. Configure via config.json (see config.example.json).

v7: on a fresh install (auth enabled, no users.json) the web UI shows a
setup screen that creates the first admin — the CLI --create-user path is
the fallback. Proxmox is multi-instance (config `proxmox` is a list).

Security: binds to 127.0.0.1 by default. Binding to 0.0.0.0 requires an
explicit --bind flag and prints a warning — enable `auth` in config.json.
"""
import argparse
import os
import sys
from typing import List, Optional

from pi_hub import __version__, server


def main(argv: Optional[List[str]] = None) -> None:
    """Parse CLI arguments and run the dashboard server."""
    parser = argparse.ArgumentParser(
        description="Pi Hub Dashboard — lightweight network control dashboard")
    parser.add_argument("--bind", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8898,
                        help="listen port (default: 8898)")
    parser.add_argument("--dev", action="store_true",
                        help="re-read static files per request (dev mode)")
    parser.add_argument("--create-user", metavar="NAME", default=None,
                        help="create a user in users.json and exit (bootstrap/recovery)")
    parser.add_argument("--role", default=None, choices=["admin", "viewer"],
                        help="role for --create-user (first user defaults to admin)")
    parser.add_argument("--force", action="store_true",
                        help="allow --create-user even when users.json exists")
    parser.add_argument("--version", action="version",
                        version=f"Pi Hub {__version__}",
                        help="print the version and exit")
    args = parser.parse_args(argv)

    # S3: binding to 0.0.0.0 requires explicit --bind and warns
    if args.bind == "0.0.0.0":
        print("WARNING: binding to 0.0.0.0 — dashboard is reachable on all "
              "interfaces and has no auth layer. Only use this on trusted "
              "networks.", file=sys.stderr)

    if args.create_user:
        from pi_hub import auth as auth_mod
        if os.path.exists(auth_mod.USERS_PATH) and not args.force:
            print("users.json already exists — create users via the web UI "
                  "(Settings/Users) or cli.py (or pass --force to "
                  "create/update just this user)", file=sys.stderr)
            sys.exit(1)
        import getpass
        role = args.role or ("admin" if not auth_mod.load_users() else "viewer")
        pw1 = getpass.getpass("Password for %s: " % args.create_user)
        pw2 = getpass.getpass("Repeat password: ")
        if pw1 != pw2:
            print("FAIL: passwords do not match", file=sys.stderr)
            sys.exit(1)
        res = auth_mod.add_user(args.create_user, pw1, role)
        if not res["ok"]:
            print("FAIL:", res["error"], file=sys.stderr)
            sys.exit(1)
        print("User '%s' created (%s). users.json chmod 0600." % (args.create_user, role))
        sys.exit(0)

    server.run(args.bind, args.port)


if __name__ == "__main__":
    main()
