"""Header pill (8.3.1): ``opens`` is validated against the plugin's own tabs,
a provider returning None hides the pill, exceptions still report an error.

Runs against a temporary plugins root — never touches pi_hub_plugins/.
Run: python3 tests/test_plugin_pill.py
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import textwrap

sys.path.insert(0, ".")

from pi_hub.plugins import events, manager as mgr_mod  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


ROOT = tempfile.mkdtemp(prefix="pihub-pill-")


def write_plugin(name: str, body: str, caps: list, frame: bool = False) -> None:
    d = os.path.join(ROOT, name)
    os.makedirs(os.path.join(d, "static", "frame"), exist_ok=True)
    with open(os.path.join(d, "static", "frame", "main.js"), "w") as f:
        f.write("ph.toast('hi');")
    with open(os.path.join(d, "__init__.py"), "w") as f:
        f.write(textwrap.dedent(f"""
            from pi_hub.plugins.base import Contribution, FrameDef, Plugin

            class P(Plugin):
                name = {name!r}
                version = "1.0.0"
                plugin_api_version = 2
                capabilities = {caps!r}
                def load(self, ctx):
                    self.state = {{"pill": {{"type": "badge", "text": "3 flights"}}}}
        """) + textwrap.indent(textwrap.dedent(body), "    "))


events._reset_for_tests()
m = mgr_mod.PluginManager()
m._root = ROOT
m._state = None
m._safe = False
ADMIN = {"user": "a", "role": "admin", "caps": {}}


def load(name: str, caps: list) -> str:
    m.approve(name, caps)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        m.load_plugin(name)
    return out.getvalue()


CAPS = ["ui.header", "ui.tab", "ui.frame"]
write_plugin("pills", """
    def get_contributions(self):
        return [
            Contribution("header.pill", "to-frame", lambda: self.state["pill"], opens="flights"),
            Contribution("header.pill", "to-tab", lambda: {"type": "badge", "text": "t"}, opens="stats"),
            Contribution("header.pill", "to-nowhere", lambda: {"type": "badge", "text": "x"}, opens="missing"),
            Contribution("header.pill", "plain", lambda: None),
            Contribution("tab", "stats", lambda: {"type": "text", "text": "s"}, label="Stats", opens="stats"),
        ]
    def get_frames(self):
        return [FrameDef("flights", entry=["main.js"], surfaces=[{"type": "tab", "label": "Flights"}]),
                FrameDef("widget", entry=["main.js"], surfaces=[{"type": "widget", "view": "hosts"}])]
""", CAPS)
log = load("pills", CAPS)
check("plugin with pills loads", "pills" in m._plugins, log)
by = {c["id"]: c for c in m.get_ui_manifest(ADMIN)["contribs"]}
check("opens a frame tab", by["pills/to-frame"]["opens"] == {"kind": "frame", "id": "flights"}, str(by["pills/to-frame"]))
check("opens a tab contribution", by["pills/to-tab"]["opens"] == {"kind": "tab", "id": "stats"}, str(by["pills/to-tab"]))
check("unknown tab id: ignored, pill not clickable", by["pills/to-nowhere"]["opens"] is None)
check("unknown tab id: warning logged", "opens unknown tab 'missing'" in log, log)
check("pill without opens has none", by["pills/plain"]["opens"] is None)
check("opens on a non-pill slot is ignored with a warning",
      by["pills/stats"]["opens"] is None and "only header.pill can open a tab" in log, log)

write_plugin("widgetonly", """
    def get_contributions(self):
        return [Contribution("header.pill", "p", lambda: None, opens="w")]
    def get_frames(self):
        return [FrameDef("w", entry=["main.js"], surfaces=[{"type": "widget", "view": "hosts"}])]
""", CAPS)
log = load("widgetonly", CAPS)
by = {c["id"]: c for c in m.get_ui_manifest(ADMIN)["contribs"]}
check("a frame without a tab surface is not a target", by["widgetonly/p"]["opens"] is None and "unknown tab" in log, log)

write_plugin("other", """
    def get_contributions(self):
        return [Contribution("header.pill", "p", lambda: None, opens="flights")]
""", ["ui.header"])
log = load("other", ["ui.header"])
by = {c["id"]: c for c in m.get_ui_manifest(ADMIN)["contribs"]}
check("another plugin's tab is not a target", by["other/p"]["opens"] is None and "unknown tab" in log, log)

write_plugin("noframe", """
    def get_contributions(self):
        return [Contribution("header.pill", "p", lambda: None, opens="f")]
    def get_frames(self):
        return [FrameDef("f", entry=["main.js"], surfaces=[{"type": "tab", "label": "F"}])]
""", ["ui.header"])
log = load("noframe", ["ui.header"])
by = {c["id"]: c for c in m.get_ui_manifest(ADMIN)["contribs"]}
check("a frame that is not granted (no ui.frame) is not a target", by["noframe/p"]["opens"] is None, log)

print("provider results")


def data(cid: str) -> dict:
    lc = m.find_contribution(cid)
    lc.cache.clear()
    return m.contrib_data([cid], ADMIN)["data"][cid]


d = data("pills/plain")
check("None from a pill provider is ok with no node (renders nothing)",
      d.get("ok") is True and d["data"] == {"keyed": False, "node": None}, str(d))
d = data("pills/to-frame")
check("a node still renders", d.get("ok") is True and d["data"]["node"]["text"] == "3 flights", str(d))
m._plugins["pills"].plugin.state["pill"] = None
check("switching to None hides it", data("pills/to-frame")["data"]["node"] is None)
m._plugins["pills"].plugin.state.clear()          # provider now raises KeyError
out = io.StringIO()
with contextlib.redirect_stderr(out):
    d = data("pills/to-frame")
check("an exception still reports an error (the '!' chip)", d.get("ok") is False, str(d))
m._plugins["pills"].plugin.state["pill"] = {"type": "script"}
d = data("pills/to-frame")
check("an invalid node still reports an error", d.get("ok") is False and "invalid payload" in d.get("error", ""), str(d))

write_plugin("slotnone", """
    def get_contributions(self):
        return [Contribution("hosts.top", "banner", lambda: None)]
""", ["ui.slots"])
load("slotnone", ["ui.slots"])
d = data("slotnone/banner")
check("other slots keep rejecting None", d.get("ok") is False, str(d))

print()
if FAILED:
    print("FAILED:", ", ".join(FAILED))
    sys.exit(1)
print("ALL CHECKS PASSED")
