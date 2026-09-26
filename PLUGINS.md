# Pi Hub Plugin Development Guide

How to build, package, and distribute Pi Hub plugins (**Plugin API v2**,
Pi Hub 8.0+).

Pi Hub is a Python stdlib-only homelab dashboard. Plugins add routes,
background tasks, and UI — a new tab, but also badges, columns, buttons and
banners **inside the standard tabs**, header pills, Settings cards, colour
themes, CSS and layout. They are installed from GitHub repositories through
the **Plugin Store** (Settings → Plugins) or placed directly into
`pi_hub_plugins/`.

Plugins written for API v1 (7.x: routes + `TabUIDef` tabs) keep working
unchanged. Set `plugin_api_version = 2` to use everything below.

---

## 1. Anatomy of a plugin

```
pi_hub_plugins/
├── plugins.json              # manifest: {"enabled": ["my-plugin"]}
├── plugin_state.json         # grants, chosen theme/layout (managed by Pi Hub, 0600)
└── my-plugin/
    ├── __init__.py           # the Plugin subclass (required)
    ├── pihub-plugin.json     # declared capabilities + metadata (recommended)
    ├── helpers.py            # more modules are fine (one sub-package level too)
    ├── config.json           # per-plugin config (auto-created, optional)
    └── static/               # optional assets; static/frame/ = sandboxed JS (§9)
```

```python
from __future__ import annotations

from pi_hub.plugins.base import Plugin, PluginContext, RouteDef, Contribution


class MyPlugin(Plugin):
    name = "my-plugin"               # MUST match the directory name
    version = "1.0.0"
    description = "One-line description shown in the store"
    min_core_version = "8.0.0"       # lowest Pi Hub this works on
    plugin_api_version = 2           # 1 (default) or 2
    capabilities = ["hosts.read", "ui.slots"]   # what you use — see §7

    def load(self, ctx: PluginContext) -> None:  # optional
        self.ctx = ctx

    def unload(self) -> None:                    # optional
        pass
```

- **Naming:** `name` is a URL path segment — `[A-Za-z0-9._-]{1,64}`. It must
  equal the directory name.
- `load()` is optional: a purely declarative plugin (a theme, say) needs none.
- An unknown `plugin_api_version` is refused with a readable error.
- Plugins that fail to load are listed in Settings → Plugins with their
  error text instead of disappearing.

### `pihub-plugin.json`

Declares the capabilities **before any plugin code is imported**, so the
admin can approve them first (§7):

```json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "description": "What it does, one line",
  "min_core_version": "8.0.0",
  "plugin_api_version": 2,
  "author": "you",
  "license": "MIT",
  "capabilities": ["hosts.read", "ui.slots"]
}
```

Without a manifest Pi Hub reads a literal `capabilities = [...]` list from
`__init__.py` with the `ast` module (it never runs the code). A plugin whose
capabilities cannot be determined without running it cannot be enabled.
**The class attribute `capabilities` must match the manifest** — a plugin
that asks for more at runtime than was approved fails to load.

---

## 2. Routes

Return `RouteDef`s from `get_routes()`. Every route lives under
`/api/plugin/<name>/` — plugins cannot collide with core routes.

```python
def get_routes(self):
    return [
        RouteDef("GET",    "/status",     self.status),
        RouteDef("GET",    "/item/{id}",  self.item),                    # path parameter
        RouteDef("PUT",    "/item/{id}",  self.put_item),
        RouteDef("DELETE", "/item/{id}",  self.delete_item, caps=["admin"]),
        RouteDef("POST",   "/update-all", self.update_all, caps=["admin"]),
    ]

def item(self, session, params, query):        # take only what you need
    return {"id": params["id"], "q": query}    # dict → 200; or (data, status)
```

- Methods: `GET`, `POST`, `PUT`, `DELETE`.
- Handlers receive **only the arguments their signature names** from
  `session`, `body`, `params` (path parameters) and `query` (query string,
  `{name: value}`); `**kw` receives all. v1 handlers keep working.
- Return a dict (status 200) or a `(data, status_code)` tuple.
- `{param}` matches `[A-Za-z0-9._:~-]{1,128}`; an exact route beats a
  parameterised one.
- `caps=[]` → any authenticated user; `caps=["admin"]` → admins only.
  Enforced by the manager before your handler runs.

---

## 3. Background tasks

```python
from pi_hub.plugins.base import TaskDef

def get_tasks(self):
    return [TaskDef("probe", self._probe, interval=20)]   # every 20 s, starts at load
```

- `TaskDef(name, fn, interval=0, autostart=True)`. With `interval > 0` the
  function repeats until the plugin unloads; exceptions are logged and the
  loop continues.
- Or start work yourself with `ctx.run_task(name, fn, interval=0)`.
- Tasks are daemon threads named `plugin:<name>:<task>`;
  `thread_cancel()` returns the cancel `Event` — check it in long loops.
- `ctx.set_task_status(name, status, message)` is polled via
  `GET /api/task/plugin:<name>:<task>`.
- **Single-flight** (refuse concurrent runs): guard with a lock and return
  `409 Already running`.
- `ctx.toast(message, kind)` shows a toast to every connected UI
  (`info` / `ok` / `err`).

---

## 4. UI: contributions

A **contribution** puts a piece of UI into a named **slot**. Return them
from `get_contributions()`:

```python
def get_contributions(self):
    return [
        Contribution("hosts.card.badges", "latency", self.p_latency, poll=15),
        Contribution("containers.column", "cpu5", self.p_cpu, poll=30, label="CPU 5m avg"),
        Contribution("header.pill", "flaps", self.p_pill),
        Contribution("tab", "overview", self.p_tab, label="Insights", icon_svg="<svg …/>"),
    ]

def p_latency(self, session=None):
    # keyed slot: one node per host id
    return {"nas": {"type": "badge", "text": "12 ms", "tone": "ok"}}

def p_pill(self, session=None):
    # singleton slot: one node
    return {"type": "badge", "text": "stable", "tone": "ok"}
```

`Contribution(slot, id, provider=None, poll=30, order=100, caps=None, label="", icon_svg="", static=None)`

- `id`: `[a-z0-9-]{1,32}`, unique within the plugin.
- `provider(session=None)` returns the payload. It runs on the server in a
  thread pool with a 3 s budget; a provider that raises or times out shows a
  small **`!`** chip (with the error on hover) and backs off exponentially
  after 5 consecutive failures — it can never blank the page.
- `poll`: client refresh interval in seconds (5–3600; `0` = fetch once).
- `caps`: who sees it — same semantics as `RouteDef.caps`.
- `order`: sorts additive slots (then plugin name, then id).

### Slots

| Slot | Kind | Where | Key | Needs |
|---|---|---|---|---|
| `hosts.card.badges` / `.body` / `.actions` | keyed | host cards | host id | `ui.slots` |
| `services.card.badges` / `.actions` | keyed | service cards | service name | `ui.slots` |
| `containers.column` | keyed | extra column in Containers (`label` = header) | `<instance>:<vmid>` | `ui.slots` |
| `containers.row.actions` | keyed | buttons in a container row | `<instance>:<vmid>` | `ui.slots` |
| `stacks.row.badges` | keyed | Dockge stack rows | stack name | `ui.slots` |
| `hosts.top`, `services.top`, `containers.top`, `stacks.top` | singleton | banner under the view title | — | `ui.slots` |
| `users.top`, `settings.top` | singleton, admin only | banner | — | `ui.slots` |
| `header.pill` | singleton | next to the health chips | — | `ui.header` |
| `settings.card` | singleton, admin only | card in Settings (`label` = title) | — | `ui.settings` |
| `tab` | singleton | its own sidebar tab | — | `ui.tab` |
| `theme` | static, **exclusive** | colour tokens | — | `ui.theme` |
| `style` | static | CSS | — | `ui.style` (`ui.style.global` for global) |
| `layout` | static, **exclusive** | default view, nav order, density | — | `ui.layout` |

Keyed providers return `{key: node}`; keys that match nothing are ignored.
With no plugin contributing to a slot the core DOM is exactly what it always
was.

### Nodes

Plugins never send HTML. A payload is a tree of JSON **nodes** from a closed
vocabulary; the server validates it and the browser renders it with
escaping. Unknown fields are dropped, unknown node types are rejected.

| `type` | Fields |
|---|---|
| `text` | `text`, `mono`, `tone` |
| `badge` | `text`, `tone`, `title` |
| `status` | `state` (`ok warn bad off unknown`), `text` |
| `stat` | `value`, `unit`, `label`, `tone` |
| `gauge` / `progress` | `pct` (0–100), `label`, `text` |
| `kv` | `rows`: `[[label, value-or-node], …]` |
| `link` | `text`, `href` (`http(s)://…` or a same-origin `/path`; opens with `noopener noreferrer`) |
| `button` | `label`, `action`, `method` (`GET POST PUT DELETE`), `style` (`primary ok bad quiet`), `confirm`, `caps`, `fields` |
| `empty` | `title`, `text` |
| `stack` | `children`, `dir` (`row`/`col`) |
| `columns` | `children` |
| `section` | `title`, `children` |
| `table` | `columns: [{key, label, mono, align}]`, `rows: [{key: value-or-node, _id}]`, `row_actions: [button…]` |

`tone` is one of `ok warn bad muted accent info`.

**Buttons** call `POST /api/plugin/<name>/<action>` with body
`{key, row?, …field values}` (GET/DELETE send them as query parameters).
`action` is a relative route (`a/b`; no `..`, no leading slash, no query).
`confirm` opens a confirmation dialog; `fields` (`text password number
checkbox select`) open a form dialog first. Register a `RouteDef` for every
action. Row actions get the row's `_id` as `row`.

**Limits:** depth ≤ 6, ≤ 2000 nodes, strings ≤ 500 characters, ≤ 500 rows /
keys, ≤ 256 KB per response.

### Tabs

A `tab` contribution renders its node (usually a `stack` with a `table` and
`button`s) in its own sidebar entry, polled at `poll`. The v1 `TabUIDef` path
still works; its `position` now sorts the sidebar, `ActionDef.style` maps to
the button style and `ActionDef.caps` hides the button for users who may not
use it.

### Theme, style, layout

Static contributions carry their payload in `static=` and have no provider:

```python
Contribution("theme", "midnight", label="Midnight", static={"tokens": {
    "dark":  {"--bg": "#0f0d1a", "--accent": "#a78bfa"},
    "light": {"--accent": "#7c3aed"},
}})
Contribution("layout", "compact", label="Compact", static={
    "default_view": "containers", "nav_order": ["containers", "hosts"],
    "nav_hidden": ["stacks"], "density": "compact"})
Contribution("style", "css", static={"css": ".hostcard { box-shadow: none; }"})
```

- **Theme tokens** are limited to the core's own CSS variables (`--bg
  --surface --surface-2 --border --border-strong --text --muted --faint
  --accent --accent-hover --accent-ink --ok --ok-ink --warn --bad --bad-ink
  --radius --radius-sm --shadow`); values may not contain `; { } < > \ url(
  @import /*`.
- **CSS** is sanitised (no `@import`, `url(`, `expression`, `</`, backslash,
  `@font-face`, `image-set`…; braces balanced; ≤ 32 KB) and wrapped in
  `.plg-<name>{…}` so it only reaches your own elements. Global CSS
  (`{"css": …, "global": True}`) needs `ui.style.global`, which the consent
  dialog flags as high risk.
- **Exclusive slots** (theme, layout) have no winner until the admin picks
  one in **Settings → Appearance**; the light/dark switch keeps working.
- `settings` and `layout.nav_hidden` can never hide the Settings tab.

---

## 5. Config schema

Declare settings and Pi Hub renders the form (Settings → Plugins →
**Configure**):

```python
def get_config_schema(self):
    return [
        {"name": "probe_port", "label": "Probe port", "type": "number", "default": 22, "min": 1, "max": 65535},
        {"name": "api_key",    "label": "API key",    "type": "password"},   # secret: masked, kept when left blank
        {"name": "mode",       "label": "Mode",       "type": "select", "options": ["fast", "slow"]},
        {"name": "verbose",    "label": "Verbose",    "type": "checkbox"},
        {"name": "url",        "label": "URL",        "type": "text", "required": True},
    ]
```

Values are validated and stored in the plugin's `config.json`
(`ctx.get_config()`); secrets are never sent back to the browser. When your
`version` changes, `migrate_config(old_version, config) -> dict` runs before
`load()`, and `on_config_change(config)` runs after an admin saves.

---

## 6. Events

Subscribe by listing event names; Pi Hub calls `on_event(name, payload)` on a
dedicated dispatcher thread, so a slow or crashing plugin cannot block a
request:

```python
events = ["host.state", "container.state"]

def on_event(self, name, payload):
    if name == "host.state":
        ...   # {"host_id", "old": "up", "new": "down"}
```

| Event | Capability | Payload |
|---|---|---|
| `host.state` | `hosts.read` | `{host_id, old, new}` (`up`/`down`) |
| `container.state` | `proxmox.read` | `{instance, vmid, name, old, new}` |
| `scan.complete` | `services.read` | `{candidates}` |
| `config.changed` | — | `{}` |

While at least one plugin subscribes to `host.state` / `container.state`, a
watcher probes hosts every 30 s and containers every 60 s so changes are seen
even when nobody has the dashboard open. The first observation only seeds the
baseline. The v1 hooks `on_host_state_change(host_id, new_state)` and
`on_scan_complete(results)` now work too.

---

## 7. Permissions, consent and safety

**Capabilities.** A plugin declares what it needs; nothing is granted
implicitly.

| Capability | Gives |
|---|---|
| `hosts.read` `services.read` `proxmox.read` `dockge.read` | read-only access through `PluginContext` |
| `hosts.wake` `proxmox.control` `ssh.execute` | actions (WOL, start/stop containers, power/`pct exec`) — flagged as high risk |
| `ui.tab` `ui.slots` `ui.header` `ui.settings` | contribute UI to those places |
| `ui.theme` `ui.style` `ui.layout` | appearance |
| `ui.style.global` | CSS that can restyle **everything** (can hide or fake elements) — high risk |
| `ui.frame` | run your own JavaScript in a sandboxed frame (§9) — high risk |

**Consent.** When an admin enables a plugin, Settings shows what it
declares in plain language. **Nothing of the plugin is imported until the
admin approves.** Approvals are stored in `pi_hub_plugins/plugin_state.json`
(mode 0600). An update that declares new capabilities waits for
re-approval. Plugins that were already enabled when you upgrade to 8.0 are
grandfathered with their declared capabilities (plus `ui.tab`).

**What this is — and is not.** Grants are *consent and a kill-switch*, not a
sandbox for Python: an approved plugin's Python runs in the Pi Hub process
with the privileges of the service. Only install plugins you would run as
code on your Pi. The UI layer, in contrast, is contained: plugins cannot
inject HTML, script or arbitrary CSS into the dashboard.

**Safe mode.** If a plugin breaks the UI:

| Switch | Effect |
|---|---|
| `/?safe=1` | this page load shows the plain dashboard — no plugin UI at all |
| `/?noframes=1` | frames only are disabled |
| Settings → Plugins → **Hide UI** | switch one plugin's UI off (its routes and tasks keep running) |
| `python3 run.py --safe-mode` or `PIHUB_SAFE_MODE=1` | the server loads no plugins |

**Dashboard hardening (8.0).** The dashboard page is served with a
hash-pinned Content-Security-Policy: `script-src` lists the SHA-256 of the
page's two inline scripts — no `'self'`, no `'unsafe-inline'` — so injected
markup cannot execute. If you hand-edit `web/index.html` and add inline
scripts or `on*=` handlers, they will be blocked. Plugin assets under
`/plugin-static/` are served with a script-less policy, so a
`<script src="/plugin-static/…">` does not work (and never did; requests
need the `Authorization` header).

---

## 8. System access — PluginContext

`PluginContext` is the only sanctioned way to touch the Pi Hub system. Each
method checks the plugin's **approved** capabilities at call time; a missing
capability raises `PermissionError`.

| Method | Capability | What it gives you |
|--------|-----------|-------------------|
| `get_hosts()` | `hosts.read` | Host registry (id, name, ip, …) |
| `get_services()` | `services.read` | Configured services |
| `get_proxmox_containers(instance_id="")` | `proxmox.read` | Container list (PVE API) |
| `get_proxmox_instances()` | `proxmox.read` | Configured Proxmox instances |
| `get_dockge_stacks()` | `dockge.read` | Dockge stacks |
| `ssh_action(host_id, action)` | `ssh.execute` | Safe power actions (shutdown/reboot) |
| `ssh_cmd(host_id, command)` | `ssh.execute` | **Allowlisted** SSH: only `pct exec …` |
| `wake_host(host_id)` | `hosts.wake` | WOL magic packet |
| `proxmox_action(instance_id, vmid, action)` | `proxmox.control` | Start/stop/reboot containers |
| `run_task(name, fn, interval=0)` | — | Background task |
| `set_task_status` / `get_task_status` | — | Task progress |
| `toast(message, kind)` | — | Toast in every connected UI |
| `get_config()` / `save_config()` | — | Your `config.json` |
| `register_static(url_path, file_path)` | — | Serve a file from `static/` (`/plugin-static/<name>/…`, auth required) |
| `request_restart(tag)` | — | Ask the core to restart |

Never `import pi_hub.config` or reach into core internals — that bypasses the
capability boundary.

---

## 9. Sandboxed JavaScript frames (`ui.frame`)

Declarative nodes cover most needs. When you need your own JavaScript — a
canvas chart, a live log — ship it as a **frame**. It runs in
`<iframe sandbox="allow-scripts">` **without** `allow-same-origin`: an
opaque origin that cannot read the dashboard's `localStorage` (where the
admin token lives) or cookies, cannot make network requests
(`connect-src 'none'`), cannot navigate the page, open popups or submit
forms. It talks to the dashboard only through a validated message bridge.

```python
from pi_hub.plugins.base import FrameDef

capabilities = ["ui.frame", "ui.slots", "hosts.read"]

def get_frames(self):
    return [FrameDef(
        "pulse",
        entry=["pulse.js"], css=["pulse.css"], assets=["logo.svg"],   # under static/frame/
        surfaces=[{"type": "tab", "label": "Pulse"},
                  {"type": "widget", "view": "hosts"}],               # or {"type": "settings"}
        renders=["hosts.card.badges"],     # slots it may feed with ph.render
        reads=["hosts.status"],            # core read APIs it may ask for (needs hosts.read)
        height=240,
    )]
```

Frame code (`static/frame/pulse.js`) gets a frozen global `ph`:

```js
ph.call('samples').then(d => draw(d));            // your own route: GET /api/plugin/<name>/samples
ph.call('act', { method: 'POST', body: { n: 1 } });
ph.read('hosts.status').then(hosts => …);         // hosts.status services.status proxmox.containers dockge.stacks
ph.render('hosts.card.badges', 'nas', { type: 'badge', text: '12 ms', tone: 'ok' });  // node in a slot
ph.on('theme', t => …); ph.on('visible', v => …); ph.on('refresh', () => …);
ph.theme;  ph.user.role;  ph.surface;             // 'tab' | 'widget' | 'settings'
ph.toast('done', 'ok');  ph.resize(300);  ph.asset('logo.svg');   // asset → data: URL
```

- The bridge sets CSS variables (`var(--accent)`, …) and `data-theme` so the
  frame matches the dashboard and follows the light/dark switch.
- Height follows the content (clamped: 1200 px for widgets, 4000 for tabs).
- Each surface is its own frame instance and is mounted while its view is
  visible, unmounted after 5 minutes hidden. `ph.render` output exists only
  while the frame is mounted.
- Files: entry `.js`, css `.css`, assets `.svg .png .json .woff2`; ≤ 512 KB per
  file, ≤ 2 MB per frame. Scripts must not contain `</script` or `<!--`
  (write `'<\/script>'`). Frame files are read once at load and pinned by
  SHA-256; if the files change without a version bump the frame is blocked
  until the admin approves again.

**Enforcement, twice.** The dashboard validates every message (allow-listed
op, route pattern, ≤ 64 KB bodies, node vocabulary and size, declared
`renders`/`reads`), rate-limits (10 calls/s, burst 20; 5 renders/s per slot;
one toast per 5 s; ≤ 4 in flight) and stops a frame after three violations
within a minute. Independently the server scopes the frame's token
(`Authorization: Frame <token>`, minted per frame, never visible to the
frame): only `/api/plugin/<its own name>/…` and the declared read APIs — a
compromised dashboard bridge still reaches nothing else. The iframe URL
carries a one-time 30-second ticket; tickets and tokens are revoked on
logout, plugin unload, "Hide UI" and re-approval. A frame that stops
answering pings is removed; a frame that froze the tab three times in a row
is not mounted again in that tab (reload with `?noframes=1`).

**Known limit: busy loops.** A frame that spins forever cannot be preempted
by the dashboard. In Chromium a sandboxed frame from the same site may share
the tab's renderer, so the whole tab stays frozen for as long as the loop
runs (the ping watchdog only helps where the browser isolates the frame in
its own process). The crash guard and `?noframes=1` are the safeguards; the
admin token and the dashboard state are unaffected by the freeze
(`tests/browser_evil_frame.mjs` checks this).

**Trust boundary.** Frames stop hostile *data* — hostnames, SSH usernames,
container names — from turning into code in the dashboard origin, and they
stop a frame from stealing the admin token. They do **not** stop an approved
author's frame from leaking data it was legitimately given (browsers cannot
block WebRTC or DNS prefetch via CSP). That is the same trust you already
place in the plugin's Python.

---

## 10. Static files

```python
def load(self, ctx): ctx.register_static("/assets/app.js", "static/app.js")
# served at /plugin-static/<name>/assets/app.js
```

The path is validated against escaping the plugin directory; content types
are allowlisted (never `text/html`). Requests need the `Authorization`
header, so a plain `<script src>` cannot load them — use a frame (§9) for
JavaScript.

---

## 11. Packaging & distribution (Plugin Store)

Publish a plugin as a **GitHub repo with a release**:

```
my-plugin-repo/
├── pi_hub_plugins/
│   └── my-plugin/
│       ├── __init__.py
│       ├── pihub-plugin.json
│       └── static/frame/…
└── README.md
```

Every release ships exactly two assets:

1. **`pihub-plugin.json`** — the store manifest (same file as in the plugin
   directory; use `"plugin_api_version": 2` and list `capabilities`).
2. **`pi-hub-plugin-my-plugin-<version>.tar.gz`** — the code, top-level dir
   `pi_hub_plugins/my-plugin/`:

   ```bash
   tar -czf pi-hub-plugin-my-plugin-1.0.0.tar.gz pi_hub_plugins
   ```

**Store flow.** The admin adds the repo URL in Settings → Plugins; Pi Hub
scans releases for `pihub-plugin.json` (server-side asset resolution — the
client never supplies URLs). Install downloads the tarball and extracts
**only allowlisted files** with size caps and a `py_compile` gate, then
swaps it in atomically. Installed ≠ enabled: the admin clicks Enable and
approves the declared capabilities (§7).

Extracted: top-level `__init__.py`, `config.json`, `pihub-plugin.json`,
`README.md`, `LICENSE`, any `<identifier>.py` module, `<package>/<module>.py`
(one level), and `static/**` (`static/frame/**` only `.js .css .svg .png
.json .woff2`, ≤ 512 KB per file, ≤ 2 MB per frame). Never extracted:
dotfiles, `__pycache__`, `*.pyc`, links, anything else; at most 300 files.

Rules: **one release = one plugin version** (semver tags), both assets in the
same release, delete superseded releases, and keep `min_core_version`
honest.

---

## 12. Checklist

- [ ] `name` matches the directory, `[A-Za-z0-9._-]{1,64}`
- [ ] `plugin_api_version = 2`, `min_core_version = "8.0.0"` if you use v2
- [ ] `capabilities` (class **and** `pihub-plugin.json`) list exactly what you use
- [ ] All system access goes through `PluginContext` (no `pi_hub.config`)
- [ ] Providers are fast (3 s budget) and return small payloads
- [ ] Background loops check `thread_cancel()`; long actions are single-flight
- [ ] Every button `action` has a matching `RouteDef`
- [ ] `python3 -m py_compile` passes for every `.py`
- [ ] Tried with `/?safe=1` and with the plugin's UI hidden
- [ ] If store-distributed: both assets, one plugin per release, semver tag
- [ ] English only (code, comments, UI, manifest)

---

## 13. Reference examples

- `examples/plugins/host-insights/` — declarative v2: badges on host cards, a
  Containers column, a header pill, a Settings card, a tab with a table and
  a form, config schema, task, events.
- `examples/plugins/midnight-theme/` — theme + layout + style, no code.
- `examples/plugins/live-pulse/` — a sandboxed frame with canvas sparklines.
- `tests/fixtures/evil-frame/` — a **hostile** frame used by the test-suite to
  prove the sandbox holds. Do not install it.
- `Valli-2020/pi-hub-plugin-proxmox-update-all` — a complete v1 store plugin.

Upgrading from API v1: nothing to do. To use slots, add
`plugin_api_version = 2`, declare `ui.*` capabilities and return
`Contribution`s.
