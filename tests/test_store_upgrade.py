"""Store upgrade path: enabled plugins stay enabled, grants and frame pins
follow the upgrade, plugin data survives, uninstall removes it.

Runs against a temporary plugins root and a fake GitHub asset download —
never touches pi_hub_plugins/ or the network.
Run: python3 tests/test_store_upgrade.py
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
import textwrap

sys.path.insert(0, ".")

from pi_hub.plugins import events, manager as mgr_mod, store  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


T = tempfile.mkdtemp(prefix="pihub-upgrade-")
ROOT = os.path.join(T, "pi_hub_plugins")
os.makedirs(ROOT)
store.PLUGINS_ROOT = ROOT
store.SOURCES_PATH = os.path.join(T, "plugins_sources.json")
SRC = "example-plugins"

TARBALLS: dict = {}
store._gh_asset = lambda owner, repo, asset_id: (TARBALLS[asset_id], None)


def plugin_code(name: str, version: str, caps: list, frame_js: str = "") -> dict:
    frames = ""
    if frame_js:
        frames = """
        def get_frames(self):
            return [FrameDef("view", entry=["main.js"], surfaces=[{"type": "tab", "label": "View"}])]"""
    code = textwrap.dedent(f"""
        from pi_hub.plugins.base import FrameDef, Plugin

        class P(Plugin):
            name = {name!r}
            version = {version!r}
            plugin_api_version = 2
            capabilities = {caps!r}
            def load(self, ctx):
                self.ctx = ctx
    """) + textwrap.indent(textwrap.dedent(frames), "    ")
    files = {"__init__.py": code, "pihub-plugin.json": json.dumps({"name": name, "version": version, "capabilities": caps})}
    if frame_js:
        files["static/frame/main.js"] = frame_js
    return files


def publish(name: str, version: str, files: dict, extra: dict | None = None) -> None:
    """Put a release of *name* into the fake GitHub and the source's scan cache."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for rel, data in {**files, **(extra or {})}.items():
            raw = data.encode() if isinstance(data, str) else data
            ti = tarfile.TarInfo(f"pi_hub_plugins/{name}/{rel}")
            ti.size = len(raw)
            tf.addfile(ti, io.BytesIO(raw))
    asset = len(TARBALLS) + 1
    TARBALLS[asset] = buf.getvalue()
    src = store._find_source(SRC)
    src["plugins"] = [p for p in src["plugins"] if not (p["name"] == name and p["version"] == version)]
    src["plugins"].append({"name": name, "version": version, "asset_id": asset, "core_compatible": True,
                           "installed": False, "enabled": False})
    store._save_sources()


def manifest() -> list:
    with open(os.path.join(ROOT, "plugins.json")) as f:
        return json.load(f)["enabled"]


def new_mgr() -> mgr_mod.PluginManager:
    events._reset_for_tests()
    m = mgr_mod.PluginManager()
    m._root = ROOT
    m._state = None
    m._safe = False
    mgr_mod._instance = m
    return m


store._load_sources()["sources"].append({"id": SRC, "owner": "example", "repo": "plugins", "plugins": []})
store._save_sources()
m = new_mgr()


def install_enable(name: str, version: str, caps: list, **kw) -> None:
    publish(name, version, plugin_code(name, version, caps, **kw))
    ok, msg = store.install(SRC, name, version)
    assert ok, msg
    m.approve(name, caps)
    ok, msg = store.enable(name)
    assert ok, msg


# ── 1. upgrading enabled plugins keeps them enabled ─────────────────────────
print("upgrade keeps plugins enabled")
install_enable("alpha", "1.0.0", ["ui.header"])
install_enable("beta", "1.0.0", [])
install_enable("gamma", "1.0.0", ["ui.frame", "ui.tab"], frame_js="ph.toast('v1');")
publish("delta", "1.0.0", plugin_code("delta", "1.0.0", []))
store.install(SRC, "delta", "1.0.0")                       # installed, never enabled
check("three plugins enabled before the upgrade", manifest() == ["alpha", "beta", "gamma"], str(manifest()))

for name, caps, kw in (("alpha", ["ui.header"], {}), ("beta", [], {}),
                       ("gamma", ["ui.frame", "ui.tab"], {"frame_js": "ph.toast('v2');"})):
    publish(name, "1.1.0", plugin_code(name, "1.1.0", caps, **kw))
    ok, msg = store.install(SRC, name, "1.1.0")
    check(f"{name}: upgrade succeeds", ok, msg)
check("plugins.json still lists all three after upgrading each", sorted(manifest()) == ["alpha", "beta", "gamma"], str(manifest()))
for name in ("alpha", "beta", "gamma"):
    lp = m._plugins.get(name)
    check(f"{name}: running the new version right after the swap", lp is not None and lp.plugin.version == "1.1.0",
          str(lp and lp.plugin.version))
    check(f"{name}: grant version follows the upgrade", m._grant_of(name).get("version") == "1.1.0", str(m._grant_of(name)))
pins = m._grant_of("gamma").get("frames", {})
check("gamma: frame pin re-recorded for the new version", pins.get("view", {}).get("version") == "1.1.0", str(pins))
check("gamma: frame is not blocked", not m._plugins["gamma"].frames["view"].blocked)
check("delta (installed, disabled) stays disabled", "delta" not in manifest() and "delta" not in m._plugins)
publish("delta", "1.1.0", plugin_code("delta", "1.1.0", []))
store.install(SRC, "delta", "1.1.0")
check("upgrading a disabled plugin does not enable it", "delta" not in manifest() and "delta" not in m._plugins, str(manifest()))
entry = next(p for p in store._find_source(SRC)["plugins"] if p["name"] == "alpha" and p["version"] == "1.1.0")
check("store cache marks the upgraded plugin enabled", entry.get("installed") is True and entry.get("enabled") is True, str(entry))

# Same version reinstalled with different frame files: the admin chose the
# reinstall, so the pin follows it instead of blocking the frame.
publish("gamma", "1.1.0", plugin_code("gamma", "1.1.0", ["ui.frame", "ui.tab"], frame_js="ph.toast('v2 rebuilt');"))
ok, msg = store.install(SRC, "gamma", "1.1.0")
check("gamma: same-version reinstall succeeds", ok, msg)
check("gamma: reinstalled frame is served (pin updated, not blocked)",
      "gamma" in m._plugins and not m._plugins["gamma"].frames["view"].blocked,
      str("gamma" in m._plugins and m._plugins["gamma"].frames["view"].blocked))
check("gamma: still enabled after the reinstall", "gamma" in manifest())

# ── 2. more capabilities: installed, not enabled, waiting for approval ──────
print("upgrade that asks for more")
publish("alpha", "2.0.0", plugin_code("alpha", "2.0.0", ["ui.header", "hosts.read"]))
ok, msg = store.install(SRC, "alpha", "2.0.0")
check("alpha 2.0.0 installs", ok, msg)
check("alpha is not running and not in plugins.json", "alpha" not in m._plugins and "alpha" not in manifest(), str(manifest()))
check("other plugins are untouched", sorted(manifest()) == ["beta", "gamma"], str(manifest()))
st = m.get_status().get("alpha", {})
check("/api/plugins/list shows alpha waiting for approval", st.get("status") == "needs_approval" and st.get("pending") == ["hosts.read"], str(st))
check("the install message says so", "approv" in msg, msg)
m.approve("alpha", ["ui.header", "hosts.read"])
ok, msg = store.enable("alpha")
check("approving + enabling loads it", ok and m._plugins.get("alpha") and m._plugins["alpha"].plugin.version == "2.0.0", msg)

# ── 3. persistent data ──────────────────────────────────────────────────────
print("data_dir")
ctx = m._plugins["beta"].ctx
d = ctx.data_dir()
check("data_dir is <plugin dir>/data", d == os.path.join(os.path.realpath(ROOT), "beta", "data"), d)
check("data_dir exists with mode 0700", os.path.isdir(d) and stat.S_IMODE(os.stat(d).st_mode) == 0o700,
      oct(stat.S_IMODE(os.stat(d).st_mode)))
check("data_dir is idempotent", ctx.data_dir() == d)
with open(os.path.join(d, "history.json"), "w") as f:
    f.write('{"n": 1}')
os.makedirs(os.path.join(d, "tile-cache", "7"))
with open(os.path.join(d, "tile-cache", "7", "1.png"), "wb") as f:
    f.write(b"png")
with open(os.path.join(ROOT, "beta", "config.json"), "w") as f:
    f.write('{"k": "v"}')

publish("beta", "1.2.0", plugin_code("beta", "1.2.0", []),
        extra={"data/history.json": '{"n": "from the tarball"}', "data/evil.py": "x = 1", "data/sub/x.json": "{}"})
ok, msg = store.install(SRC, "beta", "1.2.0")
check("beta upgrade with data/ in the tarball succeeds", ok, msg)
with open(os.path.join(ROOT, "beta", "data", "history.json")) as f:
    check("data/ survives the upgrade, tarball data/ is ignored", f.read() == '{"n": 1}')
check("nested data survives", os.path.isfile(os.path.join(ROOT, "beta", "data", "tile-cache", "7", "1.png")))
check("nothing from the tarball's data/ was extracted",
      not os.path.exists(os.path.join(ROOT, "beta", "data", "evil.py"))
      and not os.path.exists(os.path.join(ROOT, "beta", "data", "sub")))
check("data/ keeps mode 0700", stat.S_IMODE(os.stat(os.path.join(ROOT, "beta", "data")).st_mode) == 0o700)
with open(os.path.join(ROOT, "beta", "config.json")) as f:
    check("config.json survives the upgrade", f.read() == '{"k": "v"}')
check("no park directory left behind", not os.path.exists(os.path.join(ROOT, "beta.old")))
check("beta is running 1.2.0 with the same data dir",
      m._plugins["beta"].plugin.version == "1.2.0" and m._plugins["beta"].ctx.data_dir() == d)

ok, msg = store.install(SRC, "beta", "1.2.0")
check("same-version reinstall keeps data/", ok and os.path.isfile(os.path.join(ROOT, "beta", "data", "history.json")), msg)

ok, msg = store.uninstall("beta")
check("uninstall succeeds", ok, msg)
check("uninstall removes data/", not os.path.exists(os.path.join(ROOT, "beta")))
check("uninstall leaves the others enabled", sorted(manifest()) == ["alpha", "gamma"], str(manifest()))

publish("beta", "1.3.0", plugin_code("beta", "1.3.0", []), extra={"data/history.json": "{}"})
ok, msg = store.install(SRC, "beta", "1.3.0")
check("fresh install never creates data/ from the tarball",
      ok and not os.path.exists(os.path.join(ROOT, "beta", "data")), msg)

shutil.rmtree(T, ignore_errors=True)
print()
if FAILED:
    print("FAILED:", ", ".join(FAILED))
    sys.exit(1)
print("ALL CHECKS PASSED")
