"""contrib.py: node vocabulary, limits, hostile payloads, CSS / theme / layout
sanitising, config coercion.

Run: python3 tests/test_plugin_contrib.py
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")

from pi_hub.plugins import contrib  # noqa: E402
from pi_hub.plugins.contrib import ContribError  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def rejects(fn, *a) -> bool:
    try:
        fn(*a)
    except ContribError:
        return True
    except Exception as e:                       # any other exception is a bug
        print("      unexpected", type(e).__name__, e)
        return False
    return False


print("nodes")
n = contrib.validate_node({"type": "badge", "text": "hi", "tone": "ok", "evil": "<script>"})
check("badge normalised, unknown keys dropped", n == {"type": "badge", "text": "hi", "tone": "ok", "title": ""}, json.dumps(n))
check("unknown tone falls back to muted", contrib.validate_node({"type": "badge", "text": "x", "tone": "hotpink"})["tone"] == "muted")
check("unknown node type rejected", rejects(contrib.validate_node, {"type": "script", "text": "x"}))
check("iframe/img nodes do not exist", rejects(contrib.validate_node, {"type": "img", "src": "x"}))
check("non-object rejected", rejects(contrib.validate_node, "text") and rejects(contrib.validate_node, [1]))
check("missing required text", rejects(contrib.validate_node, {"type": "badge"}))
check("string too long", rejects(contrib.validate_node, {"type": "text", "text": "x" * 501}))
check("pct clamped", contrib.validate_node({"type": "gauge", "label": "c", "pct": 250})["pct"] == 100.0
      and contrib.validate_node({"type": "gauge", "label": "c", "pct": -5})["pct"] == 0.0)
check("pct NaN rejected", rejects(contrib.validate_node, {"type": "gauge", "pct": float("nan")}))
check("pct non-number rejected", rejects(contrib.validate_node, {"type": "gauge", "pct": "abc"}))
check("status state allowlisted", contrib.validate_node({"type": "status", "state": "pwned"})["state"] == "unknown")

print("links")
ok_links = ["https://example.com/x?y=1", "http://10.0.0.1:8080/", "/some/path", "/"]
bad_links = ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x", "//evil.com/x",
             "vbscript:x", "ftp://x", "https:///x", "", "https://a b", "/x\\y", " javascript:x",
             "https://x\n.evil", "file:///etc/passwd"]
check("good links accepted", all(contrib.validate_node({"type": "link", "text": "t", "href": h})["href"] == h for h in ok_links))
check("hostile links rejected", all(rejects(contrib.validate_node, {"type": "link", "text": "t", "href": h}) for h in bad_links),
      str([h for h in bad_links if not rejects(contrib.validate_node, {"type": "link", "text": "t", "href": h})]))

print("buttons / fields")
b = contrib.validate_node({"type": "button", "label": "Go", "action": "do/it", "style": "bad", "confirm": "sure?",
                           "fields": [{"name": "host", "type": "text"}, {"name": "n", "type": "sql"}]})
check("button normalised", b["action"] == "do/it" and b["method"] == "POST" and b["fields"][1]["type"] == "text", json.dumps(b))
for bad in ["../admin", "a/../b", "/abs", "a b", "a?x=1", "a#f", "http://x", "a//b", "", "a\\b"]:
    check("button action %r rejected" % bad, rejects(contrib.validate_node, {"type": "button", "label": "x", "action": bad}))
check("button method allowlisted", contrib.validate_node({"type": "button", "label": "x", "action": "a", "method": "TRACE"})["method"] == "POST")
check("field name must be identifier", rejects(contrib.validate_node, {"type": "button", "label": "x", "action": "a", "fields": [{"name": "a b"}]}))
check("select needs options", rejects(contrib.validate_node, {"type": "button", "label": "x", "action": "a", "fields": [{"name": "s", "type": "select"}]}))

print("tables")
t = contrib.validate_node({"type": "table", "columns": [{"key": "ip", "label": "IP", "mono": True}, {"key": "n"}],
                           "rows": [{"ip": "1.2.3.4", "n": 5, "extra": "dropped"},
                                    {"ip": {"type": "badge", "text": "x"}, "n": None, "_id": "r1"}],
                           "row_actions": [{"label": "Ban", "action": "ban"}]})
check("table normalised (extra cell keys dropped)", "extra" not in t["rows"][0] and t["rows"][1]["_id"] == "r1", json.dumps(t))
check("row action validated as button", t["row_actions"][0]["type"] == "button" and t["row_actions"][0]["action"] == "ban")
check("cell node allowed inline", t["rows"][1]["ip"]["type"] == "badge")
check("block node in cell rejected", rejects(contrib.validate_node, {"type": "table", "columns": [{"key": "a"}],
                                                                  "rows": [{"a": {"type": "table", "columns": [{"key": "b"}], "rows": []}}]}))
check("too many rows", rejects(contrib.validate_node, {"type": "table", "columns": [{"key": "a"}], "rows": [{"a": 1}] * 501}))
check("too many columns", rejects(contrib.validate_node, {"type": "table", "columns": [{"key": "c%d" % i} for i in range(13)], "rows": []}))
check("object cell value rejected", rejects(contrib.validate_node, {"type": "table", "columns": [{"key": "a"}], "rows": [{"a": [1, 2]}]}))

print("limits")
deep = {"type": "text", "text": "x"}
for _ in range(8):
    deep = {"type": "stack", "children": [deep]}
check("nesting depth capped", rejects(contrib.validate_node, deep))
wide = {"type": "stack", "children": [{"type": "text", "text": "x"}] * 100}
big = {"type": "stack", "children": [wide] * 25}
check("node count capped (2000)", rejects(contrib.validate_node, big))
check("kv rows validated", rejects(contrib.validate_node, {"type": "kv", "rows": [["a"]]}))
check("children must be a list", rejects(contrib.validate_node, {"type": "stack", "children": "x"}))
huge = {"type": "stack", "children": [{"type": "text", "text": "y" * 500}] * 100}
big2 = {"type": "stack", "children": [huge, huge, huge]}
check("payload byte cap", rejects(contrib.validate_node, big2) or len(json.dumps(contrib.validate_node(big2))) <= contrib.MAX_BYTES)

print("keyed slots")
k = contrib.validate_keyed({"h1": {"type": "badge", "text": "a"}, "vm:100": {"type": "text", "text": "b"}})
check("keyed ok", set(k) == {"h1", "vm:100"})
check("non-dict rejected", rejects(contrib.validate_keyed, [1]))
check("bad key rejected", rejects(contrib.validate_keyed, {"a<b": {"type": "text", "text": "x"}}))
check("key too long rejected", rejects(contrib.validate_keyed, {"k" * 200: {"type": "text", "text": "x"}}))
check("prototype-ish keys are plain strings", "__proto__" not in contrib.validate_keyed({"host1": {"type": "text", "text": "x"}}))
check("too many keys", rejects(contrib.validate_keyed, {"k%d" % i: {"type": "text", "text": "x"} for i in range(501)}))

print("css")
good = contrib.sanitize_css(".a > .b { color: red; } /* c */ @media (max-width: 600px) { .x { top: 0 } }")
check("normal css accepted (comments stripped)", "color: red" in good and "/*" not in good)
for bad in ["@import 'x';", "a{background:url(http://evil/x)}", "a{b:expression(x)}", "</style><script>x</script>",
            "a{b:c}}", "a{b:c", "a{-moz-binding:url(x)}", "a{content:'\\41'}", "@font-face{src:x}", "a{b:javascript:x}",
            "/* unterminated a{}", "a{background:image-set(x 1x)}", "x{}<!--", "@charset 'x';"]:
    check("css rejected: %r" % bad[:32], rejects(contrib.sanitize_css, bad))
check("css: braces inside strings cannot close the scope wrapper",
      rejects(contrib.sanitize_css, 'a{content:"{"}}html{background:red}b{content:"{"}}'))
check("css: CR/FF/NUL cannot hide string boundaries",
      all(rejects(contrib.sanitize_css, 'a{b:"%s}} body{display:none} a{c:"}' % c) for c in ("\f", "\r", "\x00")))
check("css: unterminated string rejected", rejects(contrib.sanitize_css, 'a{content:"x}'))
check("css: a legit content string is fine", contrib.sanitize_css('a::before{content:"ok"}') == 'a::before{content:"ok"}')
check("css size cap", rejects(contrib.sanitize_css, "a{b:c}" * 6000))
check("non-string css", rejects(contrib.sanitize_css, {"a": 1}))
st = contrib.validate_style({"css": ".a{b:c}"}, "my_plugin")
check("scoped css wrapped in plugin class", st["css"] == ".plg-my_plugin{.a{b:c}}" and not st["global"], st["css"])
check("plugin name sanitised in wrapper", contrib.validate_style({"css": "a{b:c}"}, "x{}y")["css"].startswith(".plg-x--y{"))
check("global flag kept", contrib.validate_style({"css": "a{b:c}", "global": True}, "p") == {"css": "a{b:c}", "global": True})

print("theme")
th = contrib.validate_theme({"tokens": {"dark": {"--accent": "oklch(0.7 0.2 300)", "--bg": "#101010"}}})
check("theme accepted", th["tokens"]["dark"]["--accent"] == "oklch(0.7 0.2 300)" and th["tokens"]["light"] == {})
check("unknown token rejected", rejects(contrib.validate_theme, {"tokens": {"dark": {"--evil": "red"}}}))
check("non-core property rejected", rejects(contrib.validate_theme, {"tokens": {"dark": {"color": "red"}}}))
for bad in ["red;}body{display:none", "url(http://x)", "a\\b", "x" * 101, "red}", "<x>", "/*x*/", ""]:
    check("theme value rejected: %r" % bad[:20], rejects(contrib.validate_theme, {"tokens": {"dark": {"--accent": bad}}}))
check("empty theme rejected", rejects(contrib.validate_theme, {"tokens": {}}))

print("layout")
check("layout accepted", contrib.validate_layout({"default_view": "containers", "nav_order": ["containers", "hosts"],
                                                  "nav_hidden": ["stacks"], "density": "compact"})["default_view"] == "containers")
check("unknown view rejected", rejects(contrib.validate_layout, {"default_view": "admin"}))
check("settings cannot be hidden", rejects(contrib.validate_layout, {"nav_hidden": ["settings"]}))
check("bad density", rejects(contrib.validate_layout, {"density": "huge"}))
check("empty layout rejected", rejects(contrib.validate_layout, {}))

print("slots")
check("every slot has a known ui cap", all(m["cap"] in contrib.UI_CAPS for m in contrib.SLOTS.values()))
check("static slots have no keyed flag", all(not m.get("keyed") for m in contrib.SLOTS.values() if m.get("static")))
check("admin-only slots marked", contrib.SLOTS["settings.card"]["admin"] and contrib.SLOTS["users.top"]["admin"])

print("config coercion")
schema = contrib.validate_config_schema([
    {"name": "url", "type": "text", "required": True},
    {"name": "key", "type": "password"},
    {"name": "n", "type": "number", "min": 1, "max": 10},
    {"name": "on", "type": "checkbox"},
    {"name": "mode", "type": "select", "options": ["a", "b"]},
    {"name": "bad name", "type": "text"}, {"type": "text"}, "junk",
])
check("schema drops invalid fields", [f["name"] for f in schema] == ["url", "key", "n", "on", "mode"], str([f["name"] for f in schema]))
check("password implies secret", schema[1]["secret"] is True)
vals, err = contrib.coerce_config_values(schema, {"url": "http://x", "n": "7", "on": 1, "mode": "b", "unknown": 5}, {})
check("values coerced, unknown dropped", vals == {"url": "http://x", "n": 7, "on": True, "mode": "b"} and err is None, str((vals, err)))
check("required text", contrib.coerce_config_values(schema, {"url": "  "}, {})[1] is not None)
check("select validated", contrib.coerce_config_values(schema, {"mode": "z"}, {})[1] is not None)
check("number bounds", contrib.coerce_config_values(schema, {"n": 0}, {})[1] is not None)
check("secret placeholder keeps value", "key" not in contrib.coerce_config_values(schema, {"key": {"__set": True}}, {"key": "old"})[0])
check("text field rejects non-string", contrib.coerce_config_values(schema, {"url": {"x": 1}}, {})[1] is not None)

print()
if FAILED:
    print("FAILED:", ", ".join(FAILED))
    sys.exit(1)
print("ALL CHECKS PASSED")
