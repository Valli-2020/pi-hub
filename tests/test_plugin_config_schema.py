"""Config schema 8.3: url / textarea / list fields, sections, row coercion,
schema inference for plugins without get_config_schema(), secret masking.

Runs against a temporary plugins root — never touches pi_hub_plugins/.
Run: python3 tests/test_plugin_config_schema.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import textwrap

sys.path.insert(0, ".")

from pi_hub.plugins import contrib, events, manager as mgr_mod  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def err_of(schema: list, values: dict, current: dict | None = None) -> str | None:
    return contrib.coerce_config_values(schema, values, current or {})[1]


LINKS = {"name": "links", "label": "Links", "type": "list", "section": "Display", "max_items": 3,
         "fields": [{"name": "title", "label": "Title", "type": "text", "required": True},
                    {"name": "url", "label": "URL", "type": "url", "required": True},
                    {"name": "port", "label": "Port", "type": "number", "min": 1, "max": 65535},
                    {"name": "on", "label": "On", "type": "checkbox"},
                    {"name": "mode", "label": "Mode", "type": "select", "options": ["a", "b"]}]}

# ── schema validation ───────────────────────────────────────────────────────
print("schema validation")
schema = contrib.validate_config_schema([
    {"name": "plain", "type": "text"},
    {"name": "home", "label": "Home", "type": "url", "section": "Connection", "required": True},
    {"name": "notes", "label": "Notes", "type": "textarea", "section": "Connection"},
    {"name": "token", "label": "Token", "type": "password", "section": "Connection"},
    LINKS,
    {"name": "odd", "type": "fancy", "section": "x" * 80},
    {"name": "plain", "type": "number"},
])
names = [f["name"] for f in schema]
check("new types kept, duplicates dropped", names == ["plain", "home", "notes", "token", "links", "odd"], str(names))
by = {f["name"]: f for f in schema}
check("section kept", by["home"]["section"] == "Connection" and by["plain"]["section"] == "")
check("section capped at 60", len(by["odd"]["section"]) == 60)
check("unknown type falls back to text", by["odd"]["type"] == "text")
check("url / textarea types", by["home"]["type"] == "url" and by["notes"]["type"] == "textarea")
check("list keeps sub-fields and max_items", [s["name"] for s in by["links"]["fields"]] == ["title", "url", "port", "on", "mode"]
      and by["links"]["max_items"] == 3, json.dumps(by["links"]))
check("list is never secret", by["links"]["secret"] is False)
check("sub-fields carry no section", all("section" not in s for s in by["links"]["fields"]))
check("list default is an empty list", by["links"]["default"] == [])
check("min/max only on numbers", "min" not in contrib.validate_config_schema(
    [{"name": "t", "type": "text", "min": 1}])[0])

check("schema capped at 60 fields", len(contrib.validate_config_schema(
    [{"name": "f%d" % i} for i in range(80)])) == 60)


def list_with(sub, **extra) -> list:
    return contrib.validate_config_schema([dict({"name": "l", "type": "list", "fields": sub}, **extra)])


check("list without fields dropped", list_with(None) == [] and list_with([]) == [])
check("list with 9 sub-fields dropped", list_with([{"name": "c%d" % i} for i in range(9)]) == [])
check("list with 8 sub-fields kept", len(list_with([{"name": "c%d" % i} for i in range(8)])) == 1)
for bad_type in ("password", "textarea", "list"):
    check("sub-field type %s drops the list" % bad_type,
          list_with([{"name": "a"}, {"name": "b", "type": bad_type}]) == [])
check("secret sub-field drops the list", list_with([{"name": "a", "secret": True}]) == [])
check("bad sub-field name drops the list", list_with([{"name": "a"}, {"name": "1x"}]) == [])
check("duplicate sub-field drops the list", list_with([{"name": "a"}, {"name": "a"}]) == [])
check("select sub-field without options drops the list", list_with([{"name": "a", "type": "select"}]) == [])
check("unknown sub-field type becomes text", list_with([{"name": "a", "type": "weird"}])[0]["fields"][0]["type"] == "text")
check("max_items defaults to 50", list_with([{"name": "a"}])[0]["max_items"] == 50)
check("max_items capped at 100", list_with([{"name": "a"}], max_items=500)[0]["max_items"] == 100)
check("invalid max_items -> default", list_with([{"name": "a"}], max_items=0)[0]["max_items"] == 50
      and list_with([{"name": "a"}], max_items=True)[0]["max_items"] == 50)
d = list_with([{"name": "a"}, {"name": "n", "type": "number"}],
              default=[{"a": "x", "n": "4", "junk": 1}])[0]["default"]
check("list default coerced", d == [{"a": "x", "n": 4}], str(d))
check("invalid list default -> []", list_with([{"name": "a"}], default="nope")[0]["default"] == [])

# ── url / textarea ─────────────────────────────────────────────────────────
print("url / textarea")
check("url accepted", err_of(schema, {"home": "https://nas.example.local:8443/x?y=1"}) is None)
check("http url accepted", err_of(schema, {"home": "HTTP://192.0.2.10"}) is None)
for bad in ("ftp://example.local", "javascript:alert(1)", "example.local", "https://", "https://a b",
            "https://a\tb", "https://a\nb", "https://a\x7f", " https://example.local"):
    check("url rejected: %r" % bad, err_of(schema, {"home": bad}) == "Home must start with http:// or https://",
          str(err_of(schema, {"home": bad})))
check("url too long rejected", err_of(schema, {"home": "https://e.local/" + "a" * 2000}) is not None)
check("required url cannot be empty", err_of(schema, {"home": ""}) == "Home is required")
opt_url = contrib.validate_config_schema([{"name": "u", "type": "url"}])
check("optional url may be empty", contrib.coerce_config_values(opt_url, {"u": ""}, {}) == ({"u": ""}, None))
check("url must be a string", err_of(opt_url, {"u": 5}) is not None)
check("textarea keeps newlines", contrib.coerce_config_values(schema, {"notes": "a\nb"}, {})[0] == {"notes": "a\nb"})
check("textarea 8000 ok", err_of(schema, {"notes": "x" * 8000}) is None)
check("textarea 8001 rejected", err_of(schema, {"notes": "x" * 8001}) == "Notes must be text (max 8000)")
check("text still limited to 2000", err_of(schema, {"plain": "x" * 2001}) is not None)

# ── list coercion ──────────────────────────────────────────────────────────
print("list coercion")
rows = [{"title": "A", "url": "https://a.example.local", "port": "8080", "on": 1, "mode": "b", "junk": "x"},
        {"title": "B", "url": "http://192.0.2.5", "port": ""}]
vals, err = contrib.coerce_config_values(schema, {"links": rows}, {})
check("rows coerced, unknown keys dropped, empty optional number left out",
      err is None and vals == {"links": [{"title": "A", "url": "https://a.example.local", "port": 8080, "on": True,
                                           "mode": "b"},
                                          {"title": "B", "url": "http://192.0.2.5"}]}, str((vals, err)))
check("empty list ok", contrib.coerce_config_values(schema, {"links": []}, {}) == ({"links": []}, None))
bad_rows = rows[:1] + [{"title": "C", "url": "https://c.example.local"}, {"title": "D", "url": "nope"}]
check("row error names the row", err_of(schema, {"links": bad_rows}) == "Links, row 3: URL must start with http:// or https://",
      str(err_of(schema, {"links": bad_rows})))
check("required sub-field missing", err_of(schema, {"links": [{"url": "https://e.local"}]}) == "Links, row 1: Title is required")
check("required sub-field blank", err_of(schema, {"links": [{"title": " ", "url": "https://e.local"}]}) == "Links, row 1: Title is required")
check("sub-field number bounds", err_of(schema, {"links": [{"title": "a", "url": "https://e.local", "port": 0}]})
      == "Links, row 1: Port must be >= 1")
check("sub-field select validated", err_of(schema, {"links": [{"title": "a", "url": "https://e.local", "mode": "z"}]})
      == "Links, row 1: Mode: invalid choice")
check("max_items enforced", err_of(schema, {"links": [{"title": "a", "url": "https://e.local"}] * 4}) == "Links: at most 3 rows")
check("max_items reached is fine", err_of(schema, {"links": [{"title": "a", "url": "https://e.local"}] * 3}) is None)
check("non-list rejected", err_of(schema, {"links": "x"}) == "Links must be a list")
check("non-object row rejected", err_of(schema, {"links": ["x"]}) == "Links, row 1: must be an object")
req_list = contrib.validate_config_schema([{"name": "l", "label": "L", "type": "list", "required": True,
                                            "fields": [{"name": "a"}]}])
check("required list needs a row", err_of(req_list, {"l": []}) == "L needs at least one row")
check("one bad row writes nothing", contrib.coerce_config_values(schema, {"home": "https://ok.local", "links": bad_rows}, {})[0] == {})

# ── secrets ────────────────────────────────────────────────────────────────
print("secrets")
check("blank secret kept", "token" not in contrib.coerce_config_values(schema, {"token": ""}, {"token": "old"})[0])
check("__set placeholder kept", "token" not in contrib.coerce_config_values(schema, {"token": {"__set": True}}, {})[0])
check("new secret stored", contrib.coerce_config_values(schema, {"token": "new"}, {"token": "old"})[0] == {"token": "new"})

# ── inference ──────────────────────────────────────────────────────────────
print("inference")
cfg = {
    "enabled": True, "port": 22, "ratio": 0.5, "name": "box", "base_url": "https://panel.example.local",
    "weird_url": "https://bad host", "API_Key": "k", "auth_token": "", "passwd": "p", "my_secret": "s",
    "links": [{"title": "A", "url": "https://a.example.local", "port": 1},
              {"title": "B", "url": "", "port": 2, "extra": True}],
    "mixed": [{"a": 1}, {"a": "x"}], "tags": ["a", "b"], "nested": {"x": 1}, "nothing": None,
    "rows_with_secret": [{"name": "a", "token": "t"}], "deep_rows": [{"a": {"b": 1}}],
    "wide_rows": [{"c%d" % i: i for i in range(9)}], "empty_list": [], "huge": "x" * 2001,
    "bad key": 1, "infinite": float("inf"),
}
inf = contrib.infer_config_schema(cfg)
types = {f["name"]: f["type"] for f in inf}
check("scalar types inferred", types.get("enabled") == "checkbox" and types.get("port") == "number"
      and types.get("ratio") == "number" and types.get("name") == "text" and types.get("base_url") == "url", str(types))
check("invalid url string stays text", types.get("weird_url") == "text")
check("secret keys -> password", all(types.get(k) == "password" for k in ("API_Key", "auth_token", "passwd", "my_secret")), str(types))
check("password fields are secret", all(f["secret"] for f in inf if f["type"] == "password"))
check("list of dicts -> list", types.get("links") == "list")
links = next(f for f in inf if f["name"] == "links")
check("list columns from the union of row keys", [(s["name"], s["type"]) for s in links["fields"]]
      == [("title", "text"), ("url", "url"), ("port", "number"), ("extra", "checkbox")], str(links["fields"]))
check("inferred list max_items 100", links["max_items"] == 100)
for skipped in ("mixed", "tags", "nested", "nothing", "rows_with_secret", "deep_rows", "wide_rows",
                "empty_list", "huge", "bad key", "infinite"):
    check("skipped: %s" % skipped, skipped not in types)
check("label from key", next(f for f in inf if f["name"] == "base_url")["label"] == "Base url")
check("order follows config.json", [f["name"] for f in inf][:4] == ["enabled", "port", "ratio", "name"])
check("non-dict config -> []", contrib.infer_config_schema(None) == [] and contrib.infer_config_schema([1]) == [])
vals, err = contrib.coerce_config_values(inf, {"links": cfg["links"], "port": "23"}, cfg)
check("inferred schema round-trips its own values", err is None and vals["links"] == cfg["links"] and vals["port"] == 23,
      str((vals, err)))

# ── manager: GET / POST with own and inferred schema ──────────────────────
print("manager")
ROOT = tempfile.mkdtemp(prefix="pihub-plugins-")


def write_plugin(name: str, code: str, config: dict | None = None) -> None:
    d = os.path.join(ROOT, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "__init__.py"), "w") as f:
        f.write(textwrap.dedent(code))
    if config is not None:
        with open(os.path.join(d, "config.json"), "w") as f:
            json.dump(config, f)


write_plugin("schemed", """
    from pi_hub.plugins.base import Plugin
    class P(Plugin):
        name = "schemed"
        version = "1.0.0"
        capabilities = []
        def load(self, ctx): self.ctx = ctx
        def get_config_schema(self):
            return [{"name": "home", "label": "Home", "type": "url", "section": "Connection"},
                    {"name": "key", "label": "Key", "type": "password", "section": "Connection"},
                    {"name": "links", "label": "Links", "type": "list", "section": "Display",
                     "fields": [{"name": "title", "label": "Title", "required": True},
                                {"name": "url", "label": "URL", "type": "url"}]}]
""")
write_plugin("legacy", """
    from pi_hub.plugins.base import Plugin
    class P(Plugin):
        name = "legacy"
        version = "1.0.0"
        capabilities = []
        def load(self, ctx): self.ctx = ctx
        def on_config_change(self, old, new): self.changed = (old, new)
""", {"server": "https://panel.example.local", "api_token": "s3cret-token", "interval": 30,
      "targets": [{"name": "a", "port": 1}], "nested": {"keep": True}})
write_plugin("bare", """
    from pi_hub.plugins.base import Plugin
    class P(Plugin):
        name = "bare"
        version = "1.0.0"
        capabilities = []
        def load(self, ctx): self.ctx = ctx
""")

events._reset_for_tests()
m = mgr_mod.PluginManager()
m._root = ROOT
m._state = None
m._safe = False
for n in ("schemed", "legacy", "bare"):
    m.approve(n, [])
    m.load_plugin(n)
st = m.get_status()
check("own schema -> has_config", st["schemed"]["has_config"] is True)
check("config.json without schema -> has_config", st["legacy"]["has_config"] is True)
check("no schema, no config -> no has_config", st["bare"]["has_config"] is False)

body, code = m.plugin_config_get("schemed")
check("own schema not marked inferred", code == 200 and body["inferred"] is False)
check("list default sent as []", body["values"]["links"] == [], json.dumps(body["values"]))
rows = [{"title": "A", "url": "https://a.example.local"}, {"title": "B", "url": ""}]
body, code = m.plugin_config_set("schemed", {"home": "https://nas.example.local", "key": "k1", "links": rows})
check("own schema saves list", code == 200, json.dumps(body))
body, code = m.plugin_config_get("schemed")
check("list round-trips", body["values"]["links"] == rows, json.dumps(body["values"]))
check("secret masked on read", body["values"]["key"] == {"__set": True} and "k1" not in json.dumps(body))
body, code = m.plugin_config_set("schemed", {"home": "nas.example.local", "key": ""})
check("invalid url -> 400 with message", code == 400 and body["error"] == "Home must start with http:// or https://",
      json.dumps(body))
body, code = m.plugin_config_set("schemed", {"links": [{"title": "", "url": ""}]})
check("row error -> 400", code == 400 and body["error"] == "Links, row 1: Title is required", json.dumps(body))
check("failed saves changed nothing", m._plugins["schemed"].ctx.get_config()["home"] == "https://nas.example.local")

body, code = m.plugin_config_get("legacy")
check("inferred schema returned", code == 200 and body["inferred"] is True
      and [f["name"] for f in body["schema"]] == ["server", "api_token", "interval", "targets"],
      json.dumps(body["schema"])[:300])
check("inferred secret masked", body["values"]["api_token"] == {"__set": True} and "s3cret-token" not in json.dumps(body))
body, code = m.plugin_config_set("legacy", {"server": "https://panel2.example.local", "api_token": "",
                                            "interval": "60", "targets": [{"name": "b", "port": "2"}]})
check("inferred schema saves", code == 200, json.dumps(body))
cfg = m._plugins["legacy"].ctx.get_config()
check("values stored with their types", cfg["interval"] == 60 and cfg["targets"] == [{"name": "b", "port": 2}]
      and cfg["server"] == "https://panel2.example.local", str(cfg))
check("blank inferred secret kept", cfg["api_token"] == "s3cret-token")
check("keys outside the inferred schema kept", cfg["nested"] == {"keep": True})
check("on_config_change called", getattr(m._plugins["legacy"].plugin, "changed", None) is not None)
body, code = m.plugin_config_set("legacy", {"server": "not a url"})
check("inferred url validated", code == 400, json.dumps(body))
body, code = m.plugin_config_set("bare", {"x": 1})
check("plugin without schema or config still refused", code == 400)
m.unload_all()
shutil.rmtree(ROOT, ignore_errors=True)

print()
if FAILED:
    print("FAILED:", ", ".join(FAILED))
    sys.exit(1)
print("ALL CHECKS PASSED")
