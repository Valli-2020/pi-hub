#!/usr/bin/env python3
"""Pi Hub CLI - drive the Pi Hub API from the terminal (v7).

Examples:
  pi_hub_cli.py status                 # host + service status table
  pi_hub_cli.py wake ollama            # WOL packet
  pi_hub_cli.py reboot keller          # SSH reboot
  pi_hub_cli.py shutdown winpc         # SSH shutdown
  pi_hub_cli.py ct                     # list Proxmox containers (all instances)
  pi_hub_cli.py ct 105 start           # start container 105 (default instance)
  pi_hub_cli.py ct pve:102 start       # start container 102 on instance 'pve'
  pi_hub_cli.py login                  # store a session token (0600)
  pi_hub_cli.py users                  # list users (admin)
  pi_hub_cli.py useradd bob viewer     # create user (admin)
  pi_hub_cli.py userdel bob            # delete user (admin)
  pi_hub_cli.py passwd                 # change own password

Point at a different host with HUB_API=http://host:8898.
Token: HUB_TOKEN env var overrides ~/.config/pi-hub/token.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from getpass import getpass
from urllib.parse import quote

BASE = os.environ.get("HUB_API", "http://127.0.0.1:8898")
TOKEN_FILE = os.path.expanduser("~/.config/pi-hub/token")


def _token() -> str:
    """Return the bearer token: HUB_TOKEN env → token file → ''."""
    t = os.environ.get("HUB_TOKEN", "")
    if t:
        return t
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def _save_token(token: str) -> None:
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)


def _req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    headers = {"Content-Type": "application/json"}
    tok = _token()
    if tok:
        headers["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("error", e.reason)
        except Exception:
            msg = e.reason
        return {"__http_error__": e.code, "error": msg}


def _get(path):
    return _req("GET", path)


def _post(path, body=None):
    return _req("POST", path, body)


def _delete(path):
    return _req("DELETE", path)


def _fail(resp):
    return isinstance(resp, dict) and "__http_error__" in resp


def _print(resp):
    if _fail(resp):
        hint = " (401 → run 'login' first)" if resp["__http_error__"] == 401 else ""
        print(f"FAIL: {resp['error']}{hint}")
        return
    if isinstance(resp, dict):
        ok = resp.get("success")
        if ok is True:
            print("OK:", resp.get("message", ""))
        elif ok is False:
            print("FAIL:", resp.get("error", ""))
        else:
            print(json.dumps(resp, indent=2))
    else:
        print(json.dumps(resp, indent=2))


def cmd_status():
    print("== Hosts ==")
    try:
        st = _get("/api/hosts/status")
    except Exception as e:
        print("  (api unreachable:", e, ")")
        return
    if _fail(st):
        _print(st)
        return
    for hid, info in st.items():
        if isinstance(info, dict):
            state = "online" if info.get("online") else "offline"
            ds = info.get("dualboot_state")
            extra = f"  [{ds}]" if ds else ""
            print(f"  {hid:<10} {state}{extra}")
        else:
            print(f"  {hid:<10} {info}")
    print("== Services ==")
    try:
        sv = _get("/api/status")
    except Exception:
        pass
    else:
        if _fail(sv):
            _print(sv)
            return
        for name, state in sv.items():
            print(f"  {name:<14} {state}")


def cmd_ct(args):
    if len(args) >= 2:
        vmid, action = args[0], args[1]
        # v7: `ct <instance>:<vmid> <action>` → instanced route;
        # plain `ct <vmid> <action>` → default instance (legacy alias)
        if ":" in vmid:
            inst, vmid = vmid.split(":", 1)
            _print(_post(f"/api/proxmox/{quote(inst)}/container/{quote(vmid)}/{quote(action)}"))
        else:
            _print(_post(f"/api/proxmox/container/{quote(vmid)}/{quote(action)}"))
        return
    d = _get("/api/proxmox/containers")
    if _fail(d):
        _print(d)
        return
    if not d.get("success"):
        print("FAIL:", d.get("error", ""))
        return
    for c in d.get("containers", []):
        print(f"  {c.get('instance',''):<8} {c['vmid']:<4} {c['name']:<12} {c['status']}")


def cmd_login():
    user = input("Username: ").strip()
    pw = getpass("Password: ")
    resp = _post("/api/auth/login", {"username": user, "password": pw})
    if _fail(resp) or "token" not in resp:
        print("FAIL: login rejected" + (f" — {resp.get('error', '')}" if _fail(resp) else ""))
        sys.exit(1)
    _save_token(resp["token"])
    print(f"OK: logged in as {user} (role {resp['user']['role']}); token saved to {TOKEN_FILE}")


def cmd_logout():
    _post("/api/auth/logout")
    try:
        os.remove(TOKEN_FILE)
    except OSError:
        pass
    print("OK: logged out")


def cmd_users():
    _print(_get("/api/auth/users"))


def _parse_caps(args):
    """Parse 'wake=ollama,winpc shutdown=ollama containers=102' → caps dict."""
    caps = {}
    for part in args:
        if "=" not in part:
            continue
        action, _, targets = part.partition("=")
        caps[action.strip()] = [t.strip() for t in targets.split(",") if t.strip()]
    return caps


def cmd_useradd(args):
    if len(args) < 1:
        print("usage: useradd <name> [admin|viewer] [action=target,target ...]")
        print("  e.g. useradd max viewer wake=ollama,winpc containers=102")
        return
    name = args[0]
    role = args[1] if len(args) > 1 and args[1] in ("admin", "viewer") else "viewer"
    caps = _parse_caps(args[2:])
    pw = getpass("Password for %s: " % name)
    _print(_post("/api/auth/users", {"username": name, "password": pw, "role": role,
                                     "caps": caps or None}))


def cmd_caps(args):
    if not args:
        print("usage: caps <name> [action=target,target ...]   (no caps args = show)")
        return
    name = args[0]
    if len(args) == 1:
        d = _get("/api/auth/users")
        if _fail(d):
            _print(d)
            return
        for u in d:
            if u.get("username") == name:
                print(json.dumps(u.get("caps", {}), indent=2))
                return
        print(f"FAIL: user {name} not found")
        return
    caps = _parse_caps(args[1:])
    _print(_post(f"/api/auth/users/{quote(name)}/caps", {"caps": caps}))


def cmd_userdel(args):
    if len(args) < 1:
        print("usage: userdel <name>")
        return
    _print(_delete(f"/api/auth/users/{quote(args[0])}"))


def cmd_passwd(args):
    target = args[0] if args else ""
    pw = getpass("New password: ")
    path = f"/api/auth/users/{quote(target)}/password" if target else ""
    if not target:
        # self-service: need own username from /api/auth/me
        me = _get("/api/auth/me")
        if _fail(me) or "username" not in me:
            _print(me)
            return
        target = me["username"]
        path = f"/api/auth/users/{quote(target)}/password"
    _print(_post(path, {"password": pw}))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "status":
        cmd_status()
    elif cmd == "login":
        cmd_login()
    elif cmd == "logout":
        cmd_logout()
    elif cmd == "users":
        cmd_users()
    elif cmd == "useradd":
        cmd_useradd(args)
    elif cmd == "caps":
        cmd_caps(args)
    elif cmd == "userdel":
        cmd_userdel(args)
    elif cmd == "passwd":
        cmd_passwd(args)
    elif cmd == "wake":
        _print(_post(f"/api/hosts/{quote(args[0])}/wake")) if args else print("usage: wake <id>")
    elif cmd == "reboot":
        _print(_post(f"/api/hosts/{quote(args[0])}/reboot")) if args else print("usage: reboot <id>")
    elif cmd == "shutdown":
        _print(_post(f"/api/hosts/{quote(args[0])}/shutdown")) if args else print("usage: shutdown <id>")
    elif cmd == "ct":
        cmd_ct(args)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
