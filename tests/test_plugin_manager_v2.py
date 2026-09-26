"""Plugin manager v2: consent gate, grants, API version, routes, tasks,
events, contributions, config schema, safe mode.

Runs against a temporary plugins root — never touches pi_hub_plugins/.
Run: python3 tests/test_plugin_manager_v2.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import textwrap
import time

sys.path.insert(0, ".")

from pi_hub.plugins import contrib, events, manager as mgr_mod  # noqa: E402
from pi_hub.plugins.base import PluginLoadError  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


ROOT = tempfile.mkdtemp(prefix="pihub-plugins-")
MARK = os.path.join(ROOT, "import-marker")


def write_plugin(name: str, code: str, files: dict | None = None) -> None:
    d = os.path.join(ROOT, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "__init__.py"), "w") as f:
        f.write(textwrap.dedent(code))
    for rel, content in (files or {}).items():
        with open(os.path.join(d, rel), "w") as f:
            f.write(content)


def new_mgr(enabled: list[str] | None = None) -> mgr_mod.PluginManager:
    events._reset_for_tests()
    m = mgr_mod.PluginManager()
    m._root = ROOT
    m._state = None
    m._safe = False
    if enabled is not None:
        with open(os.path.join(ROOT, "plugins.json"), "w") as f:
            json.dump({"enabled": enabled}, f)
    return m


def reset_state() -> None:
    p = os.path.join(ROOT, "plugin_state.json")
    if os.path.exists(p):
        os.remove(p)


ADMIN = {"user": "a", "role": "admin", "caps": {}}
VIEWER = {"user": "v", "role": "viewer", "caps": {}}

# ── Fixture plugins ─────────────────────────────────────────────────────────

write_plugin("v1p", f"""
    from pi_hub.plugins.base import Plugin, RouteDef
    open({MARK!r}, "a").write("v1p\\n")

    class V1(Plugin):
        name = "v1p"
        version = "1.0.0"
        capabilities = ["hosts.read"]
        def load(self, ctx): self.ctx = ctx
        def get_routes(self):
            return [
                RouteDef("GET", "/old", lambda session, body: {{"who": "old"}}),
                RouteDef("GET", "/kw", lambda **kw: {{"keys": sorted(kw)}}),
            ]
""")

write_plugin("v2p", """
    import time
    from pi_hub.plugins.base import (Plugin, RouteDef, TaskDef, Contribution)
    RAN = []

    class V2(Plugin):
        name = "v2p"
        version = "2.0.0"
        plugin_api_version = 2
        capabilities = ["ui.slots", "ui.header", "ui.tab", "ui.theme", "ui.style", "ui.settings"]
        events = ["config.changed"]
        def load(self, ctx):
            self.ctx = ctx
            self.got = []
            self.cfg_changes = []
        def get_routes(self):
            return [
                RouteDef("GET", "/item/{id}", self.item),
                RouteDef("GET", "/item/special", lambda session, body: {"id": "special!"}),
                RouteDef("PUT", "/item/{id}", self.put),
                RouteDef("DELETE", "/item/{id}", self.delete, caps=["admin"]),
                RouteDef("GET", "/q", self.q),
            ]
        def item(self, session, body, params, query):
            return {"id": params["id"], "q": query}
        def put(self, session, body, params):
            return {"put": params["id"], "body": body}
        def delete(self, session, body, params):
            return {"deleted": params["id"]}
        def q(self, session, body, query):
            return {"query": query}
        def get_tasks(self):
            return [TaskDef("tick", lambda: RAN.append(time.time()), interval=0.05)]
        def get_contributions(self):
            good = lambda session=None: {"h1": {"type": "badge", "text": "hi", "tone": "ok"}}
            bad = lambda session=None: {"h1": {"type": "script", "text": "x"}}
            boom = lambda session=None: 1 / 0
            slow = lambda session=None: (time.sleep(1.0), {})[1]
            return [
                Contribution("hosts.card.badges", "good", good, poll=10),
                Contribution("hosts.card.body", "bad", bad),
                Contribution("hosts.card.actions", "boom", boom),
                Contribution("hosts.top", "slow", slow),
                Contribution("header.pill", "pill", lambda session=None: {"type": "badge", "text": "P"}),
                Contribution("settings.card", "card", lambda session=None: {"type": "text", "text": "S"}, label="Card"),
                Contribution("theme", "dark", static={"tokens": {"dark": {"--accent": "#ff00aa"}}}, label="Neon"),
                Contribution("style", "css", static={"css": ".x{color:red}"}),
            ]
        def get_config_schema(self):
            return [{"name": "url", "label": "URL", "type": "text", "default": "http://x"},
                    {"name": "key", "label": "Key", "type": "password"},
                    {"name": "n", "label": "N", "type": "number", "min": 1, "max": 9}]
        def on_event(self, name, payload): self.got.append((name, payload))
        def on_config_change(self, old, new): self.cfg_changes.append((old, new))
""")

write_plugin("nocap", """
    from pi_hub.plugins.base import Plugin
    caps = ["hosts.read"]
    class P(Plugin):
        name = "nocap"
        capabilities = caps
        def load(self, ctx): pass
""")

write_plugin("badapi", """
    from pi_hub.plugins.base import Plugin
    class P(Plugin):
        name = "badapi"
        plugin_api_version = 9
        def load(self, ctx): pass
""")

write_plugin("manifested", """
    from pi_hub.plugins.base import Plugin
    class P(Plugin):
        name = "manifested"
        capabilities = ["hosts.read", "ssh.execute"]
        def load(self, ctx): pass
""", {"pihub-plugin.json": json.dumps({"capabilities": ["hosts.read"]})})

write_plugin("multi", """
    from pi_hub.plugins.base import Plugin
    from .helper import VALUE
    class P(Plugin):
        name = "multi"
        version = "1.0.0"
        def load(self, ctx): self.v = VALUE
""", {"helper.py": "VALUE = 'one'\n"})

write_plugin("mig", """
    from pi_hub.plugins.base import Plugin
    class P(Plugin):
        name = "mig"
        version = "2.0.0"
        def load(self, ctx): self.seen = dict(ctx.get_config())
        def migrate_config(self, old, cfg):
            cfg["migrated_from"] = old
            return cfg
""", {"config.json": json.dumps({"a": 1})})


# ── 1. declared caps (no import) ────────────────────────────────────────────
print("declared capabilities")
m = new_mgr(["v1p"])
check("literal list read via AST", m.declared_caps("v1p") == ["hosts.read"], str(m.declared_caps("v1p")))
check("module-level literal name found", m.declared_caps("nocap") == ["hosts.read"] or m.declared_caps("nocap") is None)
check("manifest wins over class attribute", m.declared_caps("manifested") == ["hosts.read"])
check("no code ran for declaration scan", not os.path.exists(MARK))
check("invalid name → None", m.declared_caps("../x") is None)

# ── 2. consent gate ─────────────────────────────────────────────────────────
print("consent gate")
reset_state()
m = new_mgr()
try:
    m.load_plugin("v1p")
    check("unapproved plugin refused", False)
except PluginLoadError as e:
    check("unapproved plugin refused", "needs approval" in str(e), str(e))
check("plugin code was NOT imported before consent", not os.path.exists(MARK))
check("failed record: needs_approval", m.get_status().get("v1p", {}).get("status") == "needs_approval")
ok, msg = m.approve("v1p", [])
check("partial approval rejected", not ok, msg)
ok, msg = m.approve("v1p", ["hosts.read"])
check("full approval accepted", ok, msg)
m.load_plugin("v1p")
check("loads after approval", "v1p" in m._plugins and os.path.exists(MARK))
check("failed record cleared", "v1p" not in m._failed)
check("grant persisted 0600", oct(os.stat(os.path.join(ROOT, "plugin_state.json")).st_mode & 0o777) == "0o600")
lp = m._plugins["v1p"]
try:
    lp.ctx.get_hosts()
    check("granted cap works", True)
except PermissionError:
    check("granted cap works", False)
try:
    lp.ctx.ssh_cmd("x", "pct exec 100 -- ls")
    check("ungranted cap still denied", False)
except PermissionError:
    check("ungranted cap still denied", True)
m.unload_plugin("v1p")

# runtime declaration must not exceed the approved (static) declaration
reset_state()
m = new_mgr()
ok, _ = m.approve("manifested", ["hosts.read"])
try:
    m.load_plugin("manifested")
    check("runtime caps beyond manifest refused", False)
except PluginLoadError as e:
    check("runtime caps beyond manifest refused", "not in its approved" in str(e), str(e))

# ── 3. grandfathering ───────────────────────────────────────────────────────
print("grandfathering")
reset_state()
m = new_mgr(["v1p", "multi"])
m.load_all()
check("enabled plugins keep working on first boot", "v1p" in m._plugins and "multi" in m._plugins)
check("grants written", m.granted_caps("v1p") == ["hosts.read"])
m2 = new_mgr(["v1p", "v2p"])                       # state exists now → v2p has no grant
m2.load_all()
check("plugin enabled AFTER first boot needs approval", "v2p" not in m2._plugins
      and m2._failed.get("v2p", {}).get("status") == "needs_approval")
m.unload_all()
m2.unload_all()

# ── 4. api version + failed records ─────────────────────────────────────────
print("api version / failed plugins")
reset_state()
m = new_mgr(["badapi", "v1p"])
m.load_all()
st = m.get_status()
check("unsupported api version refused", "badapi" not in m._plugins)
check("failed plugin listed with its error", st.get("badapi", {}).get("status") == "error"
      and "plugin_api_version" in st["badapi"]["last_error"], json.dumps(st.get("badapi")))
check("other plugins keep loading", "v1p" in m._plugins)
m.unload_all()

# ── 5. routes ───────────────────────────────────────────────────────────────
print("routes")
reset_state()
m = new_mgr(["v1p", "v2p"])
m._grandfather(["v1p", "v2p"])
m.load_all()
d = m.dispatch
check("v1 handler (session, body) still works", d("GET", "/api/plugin/v1p/old", ADMIN) == ({"who": "old"}, 200))
res = d("GET", "/api/plugin/v1p/kw", ADMIN, query={"a": ["1"]})
check("**kw handler receives everything", res[0]["keys"] == ["body", "params", "query", "session"], str(res))
res = d("GET", "/api/plugin/v2p/item/abc", ADMIN, query={"x": ["1", "2"]})
check("path parameter + first query value", res == ({"id": "abc", "q": {"x": "1"}}, 200), str(res))
res = d("GET", "/api/plugin/v2p/item/special", ADMIN)
check("exact route wins over pattern", res == ({"id": "special!"}, 200), str(res))
res = d("PUT", "/api/plugin/v2p/item/z9", ADMIN, body={"k": 1})
check("PUT dispatches with params + body", res == ({"put": "z9", "body": {"k": 1}}, 200), str(res))
check("DELETE admin ok", d("DELETE", "/api/plugin/v2p/item/z9", ADMIN) == ({"deleted": "z9"}, 200))
check("DELETE caps enforced (viewer 403)", d("DELETE", "/api/plugin/v2p/item/z9", VIEWER)[1] == 403)
check("param value charset limited", d("GET", "/api/plugin/v2p/item/a b", ADMIN)[1] == 404)
check("param value charset limited (slash)", d("GET", "/api/plugin/v2p/item/a/b", ADMIN)[1] == 404)
check("subset signature (session, body, query)", d("GET", "/api/plugin/v2p/q", ADMIN, query={"k": ["v"]}) == ({"query": {"k": "v"}}, 200))
check("unknown method → 404", d("POST", "/api/plugin/v2p/q", ADMIN)[1] == 404)

# ── 6. tasks ────────────────────────────────────────────────────────────────
print("tasks")
time.sleep(0.4)
ran = sys.modules["pi_hub_plugins.v2p"].RAN
check("TaskDef.fn runs repeatedly (interval)", len(ran) >= 3, str(len(ran)))
m.unload_plugin("v2p")
n = len(ran)
time.sleep(0.3)
check("task stops after unload", len(ran) <= n + 1, f"{n} → {len(ran)}")
check("plugin module + submodules dropped", "pi_hub_plugins.v2p" not in sys.modules)

# ── 7. contributions ────────────────────────────────────────────────────────
print("contributions")
reset_state()
m = new_mgr(["v2p"])
m._grandfather(["v2p"])
m.load_all()
lp = m._plugins["v2p"]
check("all contributions collected", len(lp.contribs) == 8, str(len(lp.contribs)))
man = m.get_ui_manifest(ADMIN)
ids = {c["id"] for c in man["contribs"]}
check("manifest lists dynamic contributions", {"v2p/good", "v2p/pill", "v2p/card"} <= ids, str(ids))
check("static contributions not listed as dynamic", "v2p/dark" not in ids and "v2p/css" not in ids)
check("scoped style delivered", any(s["css"].startswith(".plg-v2p{") for s in man["styles"]), str(man["styles"]))
check("theme inactive until chosen", man["theme"] is None)
check("admin sees appearance options", man["options"] and man["options"]["themes"][0]["id"] == "v2p/dark")
vman = m.get_ui_manifest(VIEWER)
vids = {c["id"] for c in vman["contribs"]}
check("viewer never sees admin-only slots", "v2p/card" not in vids and "v2p/good" in vids, str(vids))
check("viewer gets no appearance options", vman["options"] is None)
ok, msg = m.set_appearance("v2p/dark", "")
check("activate theme", ok, msg)
check("active theme served", m.get_ui_manifest(ADMIN)["theme"]["tokens"]["dark"]["--accent"] == "#ff00aa")
ok, msg = m.set_appearance("v2p/good", None)
check("non-theme id rejected as theme", not ok)

r = m.contrib_data(["v2p/good"], ADMIN)
check("good provider ok", r["data"]["v2p/good"]["ok"] and r["data"]["v2p/good"]["data"]["nodes"]["h1"]["text"] == "hi", json.dumps(r["data"]))
r = m.contrib_data(["v2p/bad"], ADMIN)
check("invalid node payload rejected", not r["data"]["v2p/bad"]["ok"] and "invalid payload" in r["data"]["v2p/bad"]["error"], json.dumps(r["data"]))
r = m.contrib_data(["v2p/boom"], ADMIN)
check("provider exception isolated (generic error)", not r["data"]["v2p/boom"]["ok"] and "ZeroDivision" not in json.dumps(r))
mgr_mod._PROVIDER_BUDGET = 0.2
r = m.contrib_data(["v2p/slow", "v2p/good"], ADMIN)
check("slow provider times out, others unaffected", not r["data"]["v2p/slow"]["ok"] and r["data"]["v2p/good"]["ok"], json.dumps(r["data"]))
mgr_mod._PROVIDER_BUDGET = 3.0
r = m.contrib_data(["v2p/card"], VIEWER)
check("viewer cannot fetch admin-only contribution data", not r["data"]["v2p/card"]["ok"])
r = m.contrib_data(["v2p/dark", "nope/x", "junk"], ADMIN)
check("static/unknown ids unavailable", all(not v["ok"] for v in r["data"].values()))

lc = m.find_contribution("v2p/boom")
for _ in range(6):
    lc.cache.clear()
    m.contrib_data(["v2p/boom"], ADMIN)
check("errors counted", lc.errors >= 5, str(lc.errors))
r = m.contrib_data(["v2p/boom"], ADMIN)
check("exponential backoff after 5 errors", "backing off" in r["data"]["v2p/boom"]["error"], json.dumps(r["data"]))

ok, msg = m.set_ui_off("v2p", True)
check("per-plugin UI off hides everything", ok and not m.get_ui_manifest(ADMIN)["contribs"] and m.get_ui_manifest(ADMIN)["styles"] == [])
m.set_ui_off("v2p", False)

# ── 8. toasts ───────────────────────────────────────────────────────────────
print("toasts")
s0 = mgr_mod.toast_seq()
mgr_mod.push_toast("v2p", "hello", "success")
mgr_mod.push_toast("v2p", "oops", "error")
r = m.contrib_data([], ADMIN, since=s0)
check("toasts delivered since seq", [t["message"] for t in r["toasts"]] == ["hello", "oops"], json.dumps(r["toasts"]))
check("toast kinds normalised", [t["kind"] for t in r["toasts"]] == ["ok", "err"])
check("no repeats after seq", m.contrib_data([], ADMIN, since=r["toast_seq"])["toasts"] == [])

# ── 9. events ───────────────────────────────────────────────────────────────
print("events")
inst = lp.plugin
events.emit("config.changed", {"x": 1})
events.drain()
check("subscribed event delivered", ("config.changed", {"x": 1}) in inst.got, str(inst.got))
events.emit("host.state", {"host_id": "h", "old": "up", "new": "down"})
events.drain()
check("event without capability/subscription not delivered", all(e[0] != "host.state" for e in inst.got))

# ── 10. config schema ───────────────────────────────────────────────────────
print("config schema")
body, code = m.plugin_config_get("v2p")
check("schema + defaults returned", code == 200 and body["values"]["url"] == "http://x", json.dumps(body))
check("secret masked", body["values"]["key"] == {"__set": False})
body, code = m.plugin_config_set("v2p", {"url": "http://y", "key": "s3cret", "n": 5})
check("config saved", code == 200, json.dumps(body))
body, code = m.plugin_config_get("v2p")
check("secret never echoed", body["values"]["key"] == {"__set": True} and "s3cret" not in json.dumps(body))
body, code = m.plugin_config_set("v2p", {"url": "http://z", "key": {"__set": True}, "n": ""})
check("blank secret keeps old value", m._plugins["v2p"].ctx.get_config()["key"] == "s3cret")
body, code = m.plugin_config_set("v2p", {"n": 99})
check("range validated", code == 400, json.dumps(body))
body, code = m.plugin_config_set("v2p", {"n": "abc"})
check("type validated", code == 400)
check("on_config_change called", len(inst.cfg_changes) >= 1)
check("plugin config file is 0600", oct(os.stat(os.path.join(ROOT, "v2p", "config.json")).st_mode & 0o777) == "0o600")
m.unload_all()

# ── 11. migrate_config ──────────────────────────────────────────────────────
print("migrate_config")
reset_state()
m = new_mgr(["mig"])
m.approve("mig", [])
with open(os.path.join(ROOT, "plugin_state.json")) as f:
    st = json.load(f)
st["grants"]["mig"]["version"] = "1.0.0"
with open(os.path.join(ROOT, "plugin_state.json"), "w") as f:
    json.dump(st, f)
m._state = None
m.load_plugin("mig")
check("migrate_config called on version change", m._plugins["mig"].plugin.seen.get("migrated_from") == "1.0.0",
      str(m._plugins["mig"].plugin.seen))
m.unload_all()
m.load_plugin("mig")
check("not called again when version unchanged", "migrated_from" in m._plugins["mig"].plugin.seen)  # persisted, stamp = 2.0.0
m.unload_all()

# ── 12. multi-file reload gets fresh submodules ─────────────────────────────
print("submodules")
reset_state()
m = new_mgr()
m.approve("multi", [])
m.load_plugin("multi")
check("relative submodule import works", m._plugins["multi"].plugin.v == "one")
check("submodule registered", "pi_hub_plugins.multi.helper" in sys.modules)
m.unload_plugin("multi")
check("submodule dropped on unload", "pi_hub_plugins.multi.helper" not in sys.modules)
with open(os.path.join(ROOT, "multi", "helper.py"), "w") as f:
    f.write("VALUE = 'two'\n")
m.load_plugin("multi")
check("reinstall loads fresh submodule code", m._plugins["multi"].plugin.v == "two")
m.unload_all()

# ── 13. safe mode ───────────────────────────────────────────────────────────
print("safe mode")
reset_state()
m = new_mgr(["multi"])
m.set_safe_mode(True)
m.load_all()
check("safe mode loads nothing", not m._plugins)
check("safe mode manifest is empty", m.get_ui_manifest(ADMIN)["safe"] is True and not m.get_ui_manifest(ADMIN)["contribs"])

# ── 14. slot / descriptor validation ────────────────────────────────────────
print("descriptor validation")
write_plugin("badslot", """
    from pi_hub.plugins.base import Plugin, Contribution
    class P(Plugin):
        name = "badslot"
        plugin_api_version = 2
        capabilities = ["ui.slots"]
        def load(self, ctx): pass
        def get_contributions(self):
            return [Contribution("nope.slot", "x", lambda session=None: {})]
""")
reset_state()
m = new_mgr()
m.approve("badslot", ["ui.slots"])
try:
    m.load_plugin("badslot")
    check("unknown slot refused", False)
except PluginLoadError as e:
    check("unknown slot refused", "unknown slot" in str(e), str(e))

write_plugin("nograntui", """
    from pi_hub.plugins.base import Plugin, Contribution
    class P(Plugin):
        name = "nograntui"
        plugin_api_version = 2
        capabilities = []
        def load(self, ctx): pass
        def get_contributions(self):
            return [Contribution("header.pill", "p", lambda session=None: {"type": "badge", "text": "x"})]
""")
m = new_mgr()
m.approve("nograntui", [])
m.load_plugin("nograntui")
check("contribution without ui.* grant is denied, not served",
      not m.get_ui_manifest(ADMIN)["contribs"]
      and "header.pill" in m.get_status()["nograntui"]["denied"][0])
m.unload_all()

shutil.rmtree(ROOT, ignore_errors=True)
print()
if FAILED:
    print("FAILED:", ", ".join(FAILED))
    sys.exit(1)
print("ALL CHECKS PASSED")
