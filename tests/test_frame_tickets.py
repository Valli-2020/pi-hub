"""frame.py + manager: frame loading/validation, digest pinning, tickets,
frame tokens, scope rules, the generated document and its CSP.

Run: python3 tests/test_frame_tickets.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import textwrap

sys.path.insert(0, ".")

from pi_hub.plugins import events, frame, manager as mgr_mod  # noqa: E402
from pi_hub.plugins.base import FrameDef, PluginLoadError  # noqa: E402,F401

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


ROOT = tempfile.mkdtemp(prefix="pihub-frames-")


def write(rel: str, content: str | bytes) -> None:
    p = os.path.join(ROOT, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb" if isinstance(content, bytes) else "w") as f:
        f.write(content)


def plugin(name: str, frames_code: str, caps: str = '"ui.frame", "ui.slots", "hosts.read"', version: str = "1.0.0") -> None:
    write(f"{name}/__init__.py", textwrap.dedent(f"""
        from pi_hub.plugins.base import Plugin, FrameDef, RouteDef
        class P(Plugin):
            name = "{name}"
            version = "{version}"
            plugin_api_version = 2
            capabilities = [{caps}]
            def load(self, ctx): pass
            def get_routes(self): return [RouteDef("GET", "/samples", lambda session, body: {{"n": 1}})]
            def get_frames(self):
                return {frames_code}
    """))


def mgr() -> mgr_mod.PluginManager:
    events._reset_for_tests()
    m = mgr_mod.PluginManager()
    m._root = ROOT
    m._state = None
    m._safe = False
    return m


def try_load(mm, name: str) -> None:
    try:
        mm.load_plugin(name)
    except PluginLoadError:
        pass


def reset_state() -> None:
    p = os.path.join(ROOT, "plugin_state.json")
    if os.path.exists(p):
        os.remove(p)


ADMIN = {"user": "alice", "role": "admin", "caps": {}}
VIEWER = {"user": "bob", "role": "viewer", "caps": {}}

GOOD = '[FrameDef("main", entry=["main.js"], css=["a.css"], assets=["i.svg"], surfaces=[{"type": "tab", "label": "Pulse"}, {"type": "widget", "view": "hosts"}, {"type": "settings"}], renders=["hosts.card.badges"], reads=["hosts.status"])]'
write("fp/static/frame/main.js", "ph.render('hosts.card.badges', 'h1', {type:'badge', text:'x'});\n")
write("fp/static/frame/a.css", "body{color:red}\n")
write("fp/static/frame/i.svg", "<svg xmlns='http://www.w3.org/2000/svg'/>")
plugin("fp", GOOD)

print("loading + validation")
m = mgr()
ok, msg = m.approve("fp", ["ui.frame", "ui.slots", "hosts.read"])
check("approve", ok, msg)
m.load_plugin("fp")
lp = m._plugins.get("fp")
check("plugin with a frame loads", lp is not None and "main" in lp.frames, str(m._failed))
lf = lp.frames["main"]
check("digest recorded (64 hex)", bool(re.fullmatch(r"[0-9a-f]{64}", lf.digest)))
check("asset became a data: URL", lf.assets["i.svg"].startswith("data:image/svg+xml;base64,"))
check("frame not blocked", lf.blocked == "")
man = m.get_ui_manifest(ADMIN)
check("manifest lists the frame for admin (3 surfaces)", len(man["frames"]) == 1 and len(man["frames"][0]["surfaces"]) == 3, str(man["frames"]))
manv = m.get_ui_manifest(VIEWER)
check("settings surface hidden from viewers", [s["type"] for s in manv["frames"][0]["surfaces"]] == ["tab", "widget"], str(manv["frames"]))
check("manifest never contains file contents or digests", "digest" not in json.dumps(man["frames"]) and "ph.render" not in json.dumps(man["frames"]))
check("status lists frames", m.get_status()["fp"]["frames"] == [{"id": "main", "blocked": ""}])

print("declaration errors fail the plugin (readable message)")
bad_cases = {
    "no ui.frame → frame skipped, not fatal": None,
    "unknown read": '[FrameDef("m", entry=["main.js"], surfaces=[{"type":"tab"}], reads=["config.full"])]',
    "read without system cap": '[FrameDef("m", entry=["main.js"], surfaces=[{"type":"tab"}], reads=["proxmox.containers"])]',
    "render into tab slot": '[FrameDef("m", entry=["main.js"], surfaces=[{"type":"tab"}], renders=["tab"])]',
    "render into theme slot": '[FrameDef("m", entry=["main.js"], surfaces=[{"type":"tab"}], renders=["theme"])]',
    "render needs cap": '[FrameDef("m", entry=["main.js"], surfaces=[{"type":"tab"}], renders=["settings.card"])]',
    "path traversal": '[FrameDef("m", entry=["../__init__.py"], surfaces=[{"type":"tab"}])]',
    "absolute path": '[FrameDef("m", entry=["/etc/passwd"], surfaces=[{"type":"tab"}])]',
    "dotfile": '[FrameDef("m", entry=[".x.js"], surfaces=[{"type":"tab"}])]',
    "wrong extension": '[FrameDef("m", entry=["a.css"], surfaces=[{"type":"tab"}])]',
    "missing file": '[FrameDef("m", entry=["nope.js"], surfaces=[{"type":"tab"}])]',
    "bad id": '[FrameDef("Bad ID", entry=["main.js"], surfaces=[{"type":"tab"}])]',
    "no surface": '[FrameDef("m", entry=["main.js"])]',
    "bad surface": '[FrameDef("m", entry=["main.js"], surfaces=[{"type":"overlay"}])]',
    "bad widget view": '[FrameDef("m", entry=["main.js"], surfaces=[{"type":"widget","view":"settings"}])]',
    "height out of range": '[FrameDef("m", entry=["main.js"], surfaces=[{"type":"tab"}], height=5)]',
    "no entry": '[FrameDef("m", entry=[], surfaces=[{"type":"tab"}])]',
    "not a FrameDef": '["main"]',
    "duplicate id": '[FrameDef("m", entry=["main.js"], surfaces=[{"type":"tab"}]), FrameDef("m", entry=["main.js"], surfaces=[{"type":"tab"}])]',
}
for label, code in bad_cases.items():
    if code is None:
        plugin("bad", GOOD, caps='"ui.slots", "hosts.read"')
        mm = mgr()
        mm.approve("bad", ["ui.slots", "hosts.read"])
        try_load(mm, "bad")
        lpx = mm._plugins.get("bad")
        check(label, lpx is not None and not lpx.frames and any("frame:main" in d for d in lpx.denied), str(mm._failed))
        continue
    plugin("bad", code)
    mm = mgr()
    mm.approve("bad", ["ui.frame", "ui.slots", "hosts.read"])
    try_load(mm, "bad")
    check(label, "bad" not in mm._plugins and "bad" in mm._failed, str(mm._plugins.keys()))

print("hostile file contents")
for label, content in {"</script in js": "var a='</SCRIPT>';", "<!-- in js": "var a='<!--';"}.items():
    write("bad/static/frame/main.js", content)
    plugin("bad", '[FrameDef("m", entry=["main.js"], surfaces=[{"type":"tab"}])]')
    mm = mgr()
    mm.approve("bad", ["ui.frame", "ui.slots", "hosts.read"])
    try_load(mm, "bad")
    check(label + " refused", "bad" not in mm._plugins)
write("bad/static/frame/main.js", "1")
write("bad/static/frame/x.css", "</style><script>")
plugin("bad", '[FrameDef("m", entry=["main.js"], css=["x.css"], surfaces=[{"type":"tab"}])]')
mm = mgr()
mm.approve("bad", ["ui.frame", "ui.slots", "hosts.read"])
try_load(mm, "bad")
check("</style in css refused", "bad" not in mm._plugins)
write("bad/static/frame/big.js", "x" * (frame.MAX_FRAME_FILE + 1))
plugin("bad", '[FrameDef("m", entry=["big.js"], surfaces=[{"type":"tab"}])]')
mm = mgr()
mm.approve("bad", ["ui.frame", "ui.slots", "hosts.read"])
try_load(mm, "bad")
check("oversized file refused", "bad" not in mm._plugins)
os.symlink("/etc/passwd", os.path.join(ROOT, "bad/static/frame/link.js"))
plugin("bad", '[FrameDef("m", entry=["link.js"], surfaces=[{"type":"tab"}])]')
mm = mgr()
mm.approve("bad", ["ui.frame", "ui.slots", "hosts.read"])
try_load(mm, "bad")
check("symlink leaving static/frame refused", "bad" not in mm._plugins)

print("digest pinning")
state = json.load(open(os.path.join(ROOT, "plugin_state.json")))
check("digest pinned at first load", state["grants"]["fp"]["frames"]["main"]["sha"] == lf.digest)
m.unload_plugin("fp")
write("fp/static/frame/main.js", "/* tampered */ document.title='x';\n")
m2 = mgr()
try_load(m2, "fp")
lf2 = m2._plugins["fp"].frames["main"]
check("same version + changed file → blocked", "changed" in lf2.blocked, lf2.blocked)
check("blocked frame absent from the manifest", m2.get_ui_manifest(ADMIN)["frames"] == [])
res, code = m2.frame_ticket(ADMIN, "fp", "main")
check("blocked frame gets no ticket (409 needs_approval)", code == 409 and res.get("needs_approval"), str((res, code)))
ok, _ = m2.approve("fp", ["ui.frame", "ui.slots", "hosts.read"])
check("re-approval unblocks and re-pins", ok and m2._plugins["fp"].frames["main"].blocked == "" and
      json.load(open(os.path.join(ROOT, "plugin_state.json")))["grants"]["fp"]["frames"]["main"]["sha"] == lf2.digest)
m2.unload_plugin("fp")
write("fp/static/frame/main.js", "/* v2 */ 1;\n")
plugin("fp", GOOD, version="1.10.0")
m3 = mgr()
m3.load_plugin("fp")
check("new plugin version re-pins silently", m3._plugins["fp"].frames["main"].blocked == "")

print("tickets")
frame._reset_for_tests()
t, tok = frame.issue("alice", "fp", "main", ["hosts.status"])
check("ticket and token are long random strings", len(t) >= 40 and len(tok) >= 40 and t != tok)
check("wrong plugin → mismatch (and the ticket is spent)", frame.consume_ticket(t, "other", "main") == "mismatch")
check("spent after a mismatch", frame.consume_ticket(t, "fp", "main") == "gone")
t, tok = frame.issue("alice", "fp", "main", [])
check("first use ok", frame.consume_ticket(t, "fp", "main") == "ok")
check("replay is gone", frame.consume_ticket(t, "fp", "main") == "gone")
t, tok = frame.issue("alice", "fp", "main", [], now=1000.0)
check("expired ticket is gone", frame.consume_ticket(t, "fp", "main", now=1000.0 + frame.TICKET_TTL + 1) == "gone")
for junk in ("", "x", None, 5, "A" * 200, "../../etc/passwd"):
    check("junk ticket %r is gone" % (junk,), frame.consume_ticket(junk, "fp", "main") == "gone")
frame._reset_for_tests()
issued = [frame.issue("alice", "fp", "main", []) for _ in range(frame.MAX_TICKETS_PER_USER + 3)]
check("outstanding tickets per user are capped", sum(1 for i in issued if i) == frame.MAX_TICKETS_PER_USER)
check("other users are unaffected", frame.issue("bob", "fp", "main", []) is not None)
frame._reset_for_tests()
for _ in range(frame.MAX_TOKENS_PER_USER + 10):
    frame.issue("carol", "fp", "main", [])
    for k in list(frame._tickets):
        del frame._tickets[k]
check("frame tokens per user are capped", sum(1 for v in frame._tokens.values() if v["user"] == "carol") <= frame.MAX_TOKENS_PER_USER)

print("frame tokens")
frame._reset_for_tests()
t, tok = frame.issue("alice", "fp", "main", ["hosts.status"], now=1000.0)
sc = frame.resolve_token(tok, now=1001.0)
check("token resolves to its scope", sc and sc["user"] == "alice" and sc["plugin"] == "fp" and sc["reads"] == ["hosts.status"], str(sc))
check("the ticket is not a token (and vice versa)", frame.resolve_token(t, now=1001.0) is None)
check("token expires", frame.resolve_token(tok, now=1000.0 + frame.TOKEN_TTL + 1) is None)
check("junk tokens rejected", all(frame.resolve_token(j) is None for j in ("", "x", None, "A" * 300)))
check("only sha256 of secrets is stored", tok not in json.dumps(list(frame._tokens)) and t not in json.dumps(list(frame._tickets)))
check("resolved scope is a copy", frame.resolve_token(tok, now=1001.0).__setitem__("plugin", "evil") is None
      and frame.resolve_token(tok, now=1001.0)["plugin"] == "fp")

print("scope rules")
sc = {"plugin": "fp", "reads": ["hosts.status"]}
allowed = [("GET", "/api/plugin/fp/samples"), ("POST", "/api/plugin/fp/act"), ("PUT", "/api/plugin/fp/x/1"),
           ("DELETE", "/api/plugin/fp/x/1"), ("GET", "/api/hosts/status")]
allowed_dots = [("GET", "/api/plugin/fp/a.b"), ("GET", "/api/plugin/fp/item/host:1")]
denied = [("GET", "/api/plugin/other/samples"), ("GET", "/api/plugin/fpx/samples"), ("GET", "/api/plugin/fp"),
          ("GET", "/api/status"), ("GET", "/api/proxmox/containers"), ("POST", "/api/hosts/status"),
          ("GET", "/api/config/full"), ("GET", "/api/config"), ("GET", "/api/auth/users"), ("POST", "/api/auth/logout"),
          ("POST", "/api/plugins/frames/ticket"), ("GET", "/api/plugins/ui"), ("GET", "/api/plugins/contrib"),
          ("POST", "/api/config/plugins/fp/enable"), ("GET", "/"), ("GET", "/api/plugin/fp/../config/full"),
          ("GET", "/api/plugin/"), ("GET", ""), ("POST", "/api/hosts/x/wake"), ("GET", "/api/plugin/fp/"), ("GET", "/api/plugin/fp//x"),
          ("GET", "/api/plugin/fp/%2e%2e/x"), ("GET", "/api/plugin/fp/a\\b"), ("GET", "/api/plugin/fp/a b"), ("GET", "/api/plugin/fp/./x")]
allowed += allowed_dots
check("allowed requests", all(frame.allowed(sc, mth, p) for mth, p in allowed), str([x for x in allowed if not frame.allowed(sc, *x)]))
check("denied requests", not any(frame.allowed(sc, mth, p) for mth, p in denied), str([x for x in denied if frame.allowed(sc, *x)]))
check("reads need to be declared", not frame.allowed({"plugin": "fp", "reads": []}, "GET", "/api/hosts/status"))

print("revocation")
frame._reset_for_tests()
a = frame.issue("alice", "fp", "main", [])
b = frame.issue("bob", "fp", "main", [])
c = frame.issue("alice", "other", "main", [])
check("revoke(user) drops that user's tickets and tokens", frame.revoke(user="alice") == 4)
check("…and only theirs", frame.resolve_token(b[1]) is not None and frame.resolve_token(a[1]) is None)
check("revoke(plugin)", frame.revoke(plugin="fp") == 2 and frame.resolve_token(b[1]) is None)
frame.issue("alice", "fp", "main", [])
frame.issue("alice", "fp", "second", [])
check("revoke(plugin, frame)", frame.revoke(plugin="fp", frame="second") == 2)
frame._reset_for_tests()
m4 = mgr()
m4.load_plugin("fp")
res, code = m4.frame_ticket(ADMIN, "fp", "main")
check("ticket endpoint logic returns url + token", code == 200 and res["url"].startswith("/plugin-frame/fp/main?t=") and len(res["frame_token"]) > 40, str((res, code)))
m4.unload_plugin("fp")
check("unloading a plugin revokes its frame tokens", frame.resolve_token(res["frame_token"]) is None)
m4.load_plugin("fp")
res, code = m4.frame_ticket(ADMIN, "fp", "main")
m4.set_ui_off("fp", True)
check("'UI off' revokes tokens and refuses new tickets", frame.resolve_token(res["frame_token"]) is None
      and m4.frame_ticket(ADMIN, "fp", "main")[1] == 403)
m4.set_ui_off("fp", False)
for who, plug, fr in [(ADMIN, "nope", "main"), (ADMIN, "fp", "nope"), (ADMIN, None, None), (ADMIN, "fp", ["x"]), (ADMIN, {"a": 1}, "main")]:
    check("unknown/junk frame %r/%r → 404" % (plug, fr), m4.frame_ticket(who, plug, fr)[1] == 404)
m4._safe = True
check("safe mode serves no frames", m4.frame_ticket(ADMIN, "fp", "main")[1] == 404 and m4.get_ui_manifest(ADMIN)["frames"] == [])
m4._safe = False

print("document")
res, code = m4.frame_ticket(ADMIN, "fp", "main")
tick = res["url"].split("t=")[1]
doc, code = m4.frame_document("fp", "main", tick)
check("document served with a valid ticket", code == 200 and doc is not None)
html, csp = doc
text = html.decode()
check("second fetch with the same ticket → 410", m4.frame_document("fp", "main", tick)[1] == 410)
r2, _ = m4.frame_ticket(ADMIN, "fp", "main")
check("ticket for another frame id → 403", m4.frame_document("fp", "other", r2["url"].split("t=")[1])[1] == 403)
check("bridge, plugin script and CSS are inlined", "window.parent" in text and "/* v2 */" in text and "body{color:red}" in text)
check("no external references", not re.search(r"(src|href)=", text.replace('type="application/json"', "")))
scripts = re.findall(r"<script>(.*?)</script>", text, re.S)
hashes = re.findall(r"'sha256-([A-Za-z0-9+/=]+)'", csp)
check("CSP pins exactly the inline scripts", len(scripts) == 2 and len(hashes) == 2 and
      all(base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() in hashes for s in scripts), csp)
check("only attribute-less executable scripts", len(re.findall(r"<script\b(?![^>]*application/json)", text)) == 2)
for d in ("default-src 'none'", "connect-src 'none'", "frame-src 'none'", "form-action 'none'", "base-uri 'none'",
          "object-src 'none'", "worker-src 'none'", "frame-ancestors 'self'", "sandbox allow-scripts", "style-src 'unsafe-inline'"):
    check("frame CSP has " + d, d in csp, csp)
check("no unsafe-eval / no 'self' script", "unsafe-eval" not in csp and "script-src 'self'" not in csp)
check("frame headers", frame.FRAME_HEADERS["Cache-Control"] == "no-store" and frame.FRAME_HEADERS["Referrer-Policy"] == "no-referrer"
      and frame.FRAME_HEADERS["X-Frame-Options"] == "SAMEORIGIN" and "camera=()" in frame.FRAME_HEADERS["Permissions-Policy"])
check("asset map is embedded as inert JSON", 'id="ph-assets"' in text and "data:image/svg+xml;base64," in text)
evil = frame.LoadedFrame("p", {"id": "x", "surfaces": [], "renders": [], "reads": [], "height": 100}, ["1"], "",
                         {"a</script><script>alert(1)</script>.svg": "data:x</script>"}, "d")
etext = frame.build_document(evil)[0].decode()
check("asset names/values cannot close the JSON block", etext.count("</script>") == 3 and "<\\/script>" in etext, etext[-400:])
check("document is cached per frame", frame.build_document(evil) is frame.build_document(evil))
check("bridge has no CR and no </script", "\r" not in frame.BRIDGE_JS and "</script" not in frame.BRIDGE_JS.lower())

print()
if FAILED:
    print("FAILED:", ", ".join(FAILED))
    sys.exit(1)
print("ALL CHECKS PASSED")
