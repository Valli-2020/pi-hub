"""Frame HTTP surface: ticket endpoint, /plugin-frame document, and the
server-side scope of `Authorization: Frame <token>` (a compromised dashboard
bridge must not be able to reach anything but the frame's own plugin).

Uses a real server on an ephemeral port with throw-away users/sessions/config.

Run: python3 tests/test_frame_scope.py
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import textwrap
import threading
from http.server import ThreadingHTTPServer

sys.path.insert(0, ".")

from pi_hub import auth, config  # noqa: E402
from pi_hub.plugins import events, frame, manager as mgr_mod  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


TMP = tempfile.mkdtemp(prefix="pihub-scope-")
auth.USERS_PATH = os.path.join(TMP, "users.json")
auth.SESSIONS_PATH = os.path.join(TMP, "sessions.json")
config.CONFIG_PATH = os.path.join(TMP, "config.json")
with open(config.CONFIG_PATH, "w") as f:
    json.dump({"hosts": [], "services": []}, f)
config._cache = {}
config._cache_mtime = 0.0
config._cache_ts = 0.0

PW = "correct-horse-battery-1"
check("create admin", auth.add_user("alice", PW, "admin")["ok"])
check("create viewer", auth.add_user("bob", PW, "viewer")["ok"])
ADMIN_TOK = auth.issue_session("alice", "admin")
VIEW_TOK = auth.issue_session("bob", "viewer")

ROOT = os.path.join(TMP, "plugins")
os.makedirs(os.path.join(ROOT, "fp", "static", "frame"))
open(os.path.join(ROOT, "fp", "static", "frame", "main.js"), "w").write("window.__loaded = 1;\n")
open(os.path.join(ROOT, "fp", "__init__.py"), "w").write(textwrap.dedent("""
    from pi_hub.plugins.base import Plugin, FrameDef, RouteDef
    class P(Plugin):
        name = "fp"
        version = "1.0.0"
        plugin_api_version = 2
        capabilities = ["ui.frame", "hosts.read"]
        def load(self, ctx): pass
        def get_routes(self):
            return [RouteDef("GET", "/samples", lambda session, body: {"n": 1}),
                    RouteDef("POST", "/danger", lambda session, body: {"done": True}, caps=["admin"]),
                    RouteDef("PUT", "/x/{id}", lambda session, body, params: {"put": params["id"]})]
        def get_frames(self):
            return [FrameDef("main", entry=["main.js"], surfaces=[{"type": "tab", "label": "T"}], reads=["hosts.status"])]
"""))
events._reset_for_tests()
m = mgr_mod.PluginManager()
m._root = ROOT
m._state = None
m._safe = False
m.approve("fp", ["ui.frame", "hosts.read"])
m.load_plugin("fp")
mgr_mod._instance = m
check("fixture plugin loaded with its frame", "main" in m._plugins["fp"].frames)

from pi_hub import server  # noqa: E402

httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
PORT = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def req(method: str, path: str, auth_header: str | None = None, body: dict | None = None):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=8)
    h = {}
    if auth_header:
        h["Authorization"] = auth_header
    data = None
    if body is not None:
        data = json.dumps(body)
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    hd = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    try:
        js = json.loads(raw)
    except ValueError:
        js = None
    return r.status, hd, raw, js


def ticket(tok: str, plugin="fp", fr="main"):
    st, _, _, js = req("POST", "/api/plugins/frames/ticket", "Bearer " + tok, {"plugin": plugin, "frame": fr})
    return st, js


print("ticket endpoint")
check("unauthenticated → 401", req("POST", "/api/plugins/frames/ticket", None, {"plugin": "fp", "frame": "main"})[0] == 401)
st, js = ticket(ADMIN_TOK)
check("admin gets a ticket + frame token", st == 200 and js["url"].startswith("/plugin-frame/fp/main?t=") and js["frame_token"], str((st, js)))
st2, js2 = ticket(VIEW_TOK)
check("viewer gets one too (tab surface)", st2 == 200, str((st2, js2)))
check("unknown frame → 404", ticket(ADMIN_TOK, "fp", "nope")[0] == 404 and ticket(ADMIN_TOK, "nope", "main")[0] == 404)
st, _, _, js3 = req("POST", "/api/plugins/frames/ticket", "Bearer " + ADMIN_TOK, {"plugin": ["x"], "frame": {"a": 1}})
check("junk body → 404, no crash", st == 404, str(st))
check("manifest lists the frame", any(f["id"] == "main" for f in req("GET", "/api/plugins/ui", "Bearer " + ADMIN_TOK)[3]["frames"]))
check("the ticket endpoint refuses a frame token (frames cannot mint tickets)",
      req("POST", "/api/plugins/frames/ticket", "Frame " + js["frame_token"], {"plugin": "fp", "frame": "main"})[0] == 403)

print("/plugin-frame document")
st, js = ticket(ADMIN_TOK)
url = js["url"]
st, hd, raw, _ = req("GET", url)
check("document served without any Authorization header", st == 200 and b"window.__loaded = 1;" in raw, str(st))
check("content type html", hd.get("content-type", "").startswith("text/html"))
csp = hd.get("content-security-policy", "")
check("frame CSP: sandboxed, no network", "sandbox allow-scripts" in csp and "connect-src 'none'" in csp and "default-src 'none'" in csp, csp)
check("frame headers", hd.get("cache-control") == "no-store" and hd.get("x-frame-options") == "SAMEORIGIN" and hd.get("referrer-policy") == "no-referrer")
check("no token or ticket echoed into the document", js["frame_token"].encode() not in raw and url.split("t=")[1].encode() not in raw)
check("replay → 410", req("GET", url)[0] == 410)
st, js = ticket(ADMIN_TOK)
check("ticket for another plugin path → 403", req("GET", "/plugin-frame/other/main?t=" + js["url"].split("t=")[1])[0] == 403)
st, js = ticket(ADMIN_TOK)
check("ticket for another frame id → 403", req("GET", "/plugin-frame/fp/other?t=" + js["url"].split("t=")[1])[0] == 403)
check("no ticket → 403", req("GET", "/plugin-frame/fp/main")[0] == 403)
check("garbage ticket → 410", req("GET", "/plugin-frame/fp/main?t=" + "A" * 43)[0] == 410)
check("bearer token is not a ticket", req("GET", "/plugin-frame/fp/main?t=" + ADMIN_TOK)[0] in (410, 403))
check("malformed paths → 403", all(req("GET", p)[0] in (403, 404) for p in ("/plugin-frame/", "/plugin-frame/fp", "/plugin-frame/fp/main/extra?t=x", "/plugin-frame//?t=x")))
st, hd, _, _ = req("GET", "/plugin-frame/fp/main")
check("error responses keep the generic CSP", hd.get("content-security-policy") == server.GENERIC_CSP)

print("frame token scope (over HTTP)")
_, js = ticket(ADMIN_TOK)
FT = "Frame " + js["frame_token"]
check("own plugin GET route", req("GET", "/api/plugin/fp/samples", FT)[3] == {"n": 1})
check("own plugin PUT route with params", req("PUT", "/api/plugin/fp/x/7", FT, {})[3] == {"put": "7"})
check("declared core read API", req("GET", "/api/hosts/status", FT)[0] == 200)
for method, path in [("GET", "/api/status"), ("GET", "/api/proxmox/containers"), ("GET", "/api/dockge/stacks"),
                     ("GET", "/api/config/full"), ("GET", "/api/config"), ("GET", "/api/auth/me"), ("GET", "/api/auth/users"),
                     ("POST", "/api/auth/logout"), ("POST", "/api/auth/users"), ("GET", "/api/plugins/ui"),
                     ("GET", "/api/plugins/list"), ("GET", "/api/plugins/contrib?ids=x"), ("POST", "/api/config/plugins/fp/enable"),
                     ("POST", "/api/hosts/x/wake"), ("GET", "/api/update/status"), ("GET", "/api/plugin/other/samples"),
                     ("GET", "/api/plugin/fp/../../config/full"), ("PUT", "/api/config/x"), ("DELETE", "/api/auth/users/bob"),
                     ("POST", "/api/hosts/status")]:
    st = req(method, path, FT, {} if method in ("POST", "PUT") else None)[0]
    check("frame token refused: %s %s" % (method, path), st == 403 or (method == "PUT" and st == 404), str(st))
check("admin-only plugin route still needs an admin", req("POST", "/api/plugin/fp/danger", FT, {})[0] == 200)
_, jv = ticket(VIEW_TOK)
FTV = "Frame " + jv["frame_token"]
check("…a viewer's frame token cannot call it", req("POST", "/api/plugin/fp/danger", FTV, {})[0] == 403)
check("a viewer's frame token still reaches its plugin", req("GET", "/api/plugin/fp/samples", FTV)[3] == {"n": 1})

print("token hygiene")
check("garbage frame token → 401", req("GET", "/api/plugin/fp/samples", "Frame nope")[0] == 401)
check("empty frame token → 401", req("GET", "/api/plugin/fp/samples", "Frame ")[0] == 401)
check("a bearer session is not a frame token", req("GET", "/api/plugin/fp/samples", "Frame " + ADMIN_TOK)[0] == 401)
check("a frame token is not a bearer session", req("GET", "/api/auth/me", "Bearer " + js["frame_token"])[0] == 401)
check("frame token in a Bearer header cannot reach plugin routes", req("GET", "/api/plugin/fp/samples", "Bearer " + js["frame_token"])[0] == 401)
check("scheme is case-sensitive", req("GET", "/api/plugin/fp/samples", "frame " + js["frame_token"])[0] == 401)

print("revocation")
_, jr = ticket(ADMIN_TOK)
FTR = "Frame " + jr["frame_token"]
check("token works", req("GET", "/api/plugin/fp/samples", FTR)[0] == 200)
req("POST", "/api/auth/logout", "Bearer " + ADMIN_TOK, {})
check("logout revokes the user's frame tokens", req("GET", "/api/plugin/fp/samples", FTR)[0] == 401)
ADMIN_TOK = auth.issue_session("alice", "admin")
_, jr = ticket(ADMIN_TOK)
FTR = "Frame " + jr["frame_token"]
auth.revoke_user_sessions("alice")
check("revoking a user's sessions revokes their frame tokens", req("GET", "/api/plugin/fp/samples", FTR)[0] == 401)
_, jb = ticket(VIEW_TOK)
FTB = "Frame " + jb["frame_token"]
auth.delete_user("bob")
check("deleting the user kills their frame tokens", req("GET", "/api/plugin/fp/samples", FTB)[0] == 401)
ADMIN_TOK = auth.issue_session("alice", "admin")
_, jm = ticket(ADMIN_TOK)
FTM = "Frame " + jm["frame_token"]
m.unload_plugin("fp")
check("unloading the plugin kills its frame tokens", req("GET", "/api/plugin/fp/samples", FTM)[0] == 401)
m.load_plugin("fp")
_, jo = ticket(ADMIN_TOK)
FTO = "Frame " + jo["frame_token"]
m.set_ui_off("fp", True)
check("'UI off' kills frame tokens", req("GET", "/api/plugin/fp/samples", FTO)[0] == 401)
check("'UI off' refuses new tickets", ticket(ADMIN_TOK)[0] == 403)

httpd.shutdown()
print()
if FAILED:
    print("FAILED:", ", ".join(FAILED))
    sys.exit(1)
print("ALL CHECKS PASSED")
