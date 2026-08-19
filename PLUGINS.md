# Pi Hub Plugin Development Guide

How to build, package, and distribute Pi Hub plugins.

Pi Hub is a Python stdlib-only homelab dashboard. Plugins add routes,
background tasks, and UI without touching core code. They are installed
from GitHub repositories through the **Plugin Store** (Settings →
Plugins) or placed directly into `pi_hub_plugins/`.

---

## 1. Anatomy of a plugin

A plugin is a directory inside `pi_hub_plugins/`:

```
pi_hub_plugins/
├── plugins.json              # manifest: {"enabled": ["my-plugin"]}
└── my-plugin/
    ├── __init__.py           # the Plugin subclass (required)
    ├── config.json           # per-plugin config (auto-created, optional)
    └── static/               # optional assets (served read-only)
```

### The Plugin subclass

```python
from __future__ import annotations

from typing import Any

from pi_hub.plugins.base import (
    Plugin, PluginContext, RouteDef, TaskDef, TabUIDef, ActionDef,
)


class MyPlugin(Plugin):
    name = "my-plugin"               # MUST match the directory name
    version = "1.0.0"
    description = "One-line description shown in the store"
    min_core_version = "7.3.2"       # lowest Pi Hub this works on
    capabilities: list[str] = []     # declared capability needs

    def load(self, ctx: PluginContext) -> None:
        """Called once when enabled. Store ctx for later use."""
        self.ctx = ctx

    def unload(self) -> None:
        """Called when disabled. Cancel background work here."""
        pass
```

**Naming rules:** `name` is a URL path segment — `[A-Za-z0-9._-]{1,64}`,
no spaces. It must equal the directory name.

---

## 2. Routes

Return `RouteDef`s from `get_routes()`. Every route is namespaced under
`/api/plugin/<name>/` — plugins can never collide with core routes.

```python
def get_routes(self) -> list[RouteDef]:
    return [
        RouteDef("GET", "/status", self.status_handler, caps=[]),
        RouteDef("POST", "/update-all", self.update_all_handler, caps=["admin"]),
    ]

def status_handler(self, **kw: Any) -> tuple[Any, int]:
    return {"running": False}, 200

def update_all_handler(self, body: dict | None = None, **kw: Any) -> tuple[Any, int]:
    return {"success": True}, 202
```

- Handlers receive `session` and `body` (POST) as keyword args and must
  return a `(data, status_code)` tuple — the server serializes it as
  JSON.
- `caps=[]` → any authenticated user; `caps=["admin"]` → admin only.
- Plugin routes are classified as authenticated-but-not-admin by the
  core auth; the plugin enforces its own `caps` per route.

---

## 3. Background tasks

Long-running work runs in daemon threads, pollable via the task store.
`ctx.run_task()` is the ONLY way to start work — `get_tasks()` /
`TaskDef` are read by the manager but their `fn` is NEVER invoked, so
the declarative mechanism is inert; do not rely on it.

```python
# from any handler:
def update_all_handler(self, **kw: Any) -> tuple[Any, int]:
    self.ctx.set_task_status("update-all", "running", "starting")
    self.ctx.run_task("update-all", self._run)      # daemon thread
    return {"success": True, "started": True}, 202

def _run(self) -> None:
    cancel = thread_cancel()                        # cooperative cancel
    for i in range(10):
        if cancel is not None and cancel.is_set():
            break
        self.ctx.set_task_status("update-all", "running", f"step {i}")
    self.ctx.set_task_status("update-all", "done", "finished")
    self.ctx.toast("All done", "success")
```

- Task names are auto-namespaced `plugin:<name>:<task>` — no collisions.
- `thread_cancel()` returns a cancel `Event` when the plugin is being
  unloaded; check it in loops so `unload()` can stop the work.
- `set_task_status` / `get_task_status` are polled via
  `GET /api/task/plugin:<name>:<task>`.
- **Single-flight pattern** (refuse concurrent runs):

  ```python
  with self._lock:
      if self._running:
          return {"error": "Already running"}, 409
      self._running = True
  ```

---

## 4. UI

Plugins can contribute a **sidebar tab** (rendered by the core frontend
since 7.3.2). Return `TabUIDef`s from `get_ui()`:

```python
def get_ui(self) -> list[Any]:
    return [
        TabUIDef(
            id="my-plugin",                       # unique tab id
            label="My plugin",
            icon_svg='<svg viewBox="0 0 24 24" ...>...</svg>',
            position=10,                          # sidebar order
            poll_endpoint="/api/plugin/my-plugin/status",
            actions=[
                ActionDef("update-all", "Update all", style="primary",
                          caps=["admin"]),
            ],
        ),
    ]
```

- The tab polls `poll_endpoint` every 10 s and renders the JSON:
  top-level scalar fields become rows, arrays of objects become tables.
  Keep payloads simple and flat.
- Action buttons POST to `/api/plugin/<name>/<action-id>` — register a
  matching `RouteDef` for each action.
- Keep payloads small (the tab polls constantly).
- **NOT implemented:** `CardUIDef` (settings-page cards) is not rendered
  by the frontend yet, and `ActionDef.caps` is not filtered client-side —
  the action buttons are shown to every authenticated user.  Only
  `TabUIDef` is a live surface today.

---

## 5. System access — PluginContext

`PluginContext` is the **only** sanctioned way to touch the Pi Hub
system. Every method checks the plugin's declared `capabilities` at call
time. A plugin that declares a capability it doesn't use is fine; one
that calls a method without declaring the capability gets a
`PermissionError`.

| Method | Capability | What it gives you |
|--------|-----------|-------------------|
| `get_hosts()` | `hosts.read` | Host registry (id, name, ip, …) |
| `get_services()` | `services.read` | Configured services |
| `get_proxmox_containers(instance_id="")` | `proxmox.read` | Container list (PVE API) |
| `get_proxmox_instances()` | `proxmox.read` | Configured Proxmox instances |
| `get_dockge_stacks()` | `dockge.read` | Dockge stacks |
| `ssh_action(host_id, action)` | `ssh.execute` | Safe power actions (shutdown/reboot) |
| `ssh_cmd(host_id, command)` | `ssh.execute` | **Allowlisted** SSH: only `pct exec …` (container management) |
| `wake_host(host_id)` | `hosts.wake` | WOL magic packet |
| `proxmox_action(instance_id, vmid, action)` | `proxmox.control` | Start/stop/reboot containers |
| `run_task(name, fn)` | — | Start a background task (the ONLY task mechanism) |
| `set_task_status` / `get_task_status` | — | Task progress store |
| `toast(message, kind)` | — | UI toast (info/warn/error/success) — **buffered, not yet surfaced in the UI** |
| `register_static(url_path, file_path)` | — | Serve a file from `static/` |
| `request_restart(tag)` | — | Ask the core to restart (systemd picks up) |

**Never** `import pi_hub.config` or reach into core internals from a
plugin — that bypasses the capability boundary and breaks the security
model.

---

## 6. Events (optional)

**NOT IMPLEMENTED** — the hooks below are described for forward
compatibility but the core currently never calls them.  Do not build
against them yet; they may be removed or change signature.

```python
def on_host_state_change(self, host_id: str, new_state: str) -> None:
    """Not called yet — reserved."""

def on_scan_complete(self, results: dict) -> None:
    """Not called yet — reserved."""

def migrate_config(self, old_version: str, config: dict) -> dict:
    """Not called yet — reserved."""
```

---

## 7. Config

Each plugin gets `config.json` in its own directory, read/written via
the context:

```python
def load(self, ctx: PluginContext) -> None:
    self.ctx = ctx
    cfg = ctx.get_config()          # mutable dict
    cfg.setdefault("interval", 60)
    ctx.save_config()               # atomic write (tmp + fsync + replace)
```

---

## 8. Static files

Files under `static/` can be served read-only:

```python
def load(self, ctx: PluginContext) -> None:
    ctx.register_static("/assets/app.js", "static/app.js")
# served at /plugin-static/<name>/assets/app.js
```

The path is validated against escaping the plugin directory; content
types are allowlisted (never `text/html`).

**Note:** `/plugin-static/*` requires an `Authorization` header, so a
plain `<script src="/plugin-static/...">` tag does NOT work — fetch the
asset with the token and inject it, or inline the code.

---

## 9. Packaging & distribution (Plugin Store)

To make a plugin installable from the store, publish it as a **GitHub
repo with a release**:

### Repository layout

```
my-plugin-repo/
├── pi_hub_plugins/
│   └── my-plugin/
│       ├── __init__.py
│       └── config.json
├── pihub-plugin.json          # store manifest (release asset)
└── README.md
```

### Release assets

Every release ships exactly two assets:

1. **`pihub-plugin.json`** — the store manifest:

   ```json
   {
     "name": "my-plugin",
     "version": "1.0.0",
     "description": "What it does, one line",
     "min_core_version": "7.3.2",
     "plugin_api_version": 1,
     "author": "you",
     "license": "MIT"
   }
   ```

2. **`pi-hub-plugin-my-plugin-<version>.tar.gz`** — the code, with the
   top-level dir `pi_hub_plugins/my-plugin/`:

   ```bash
   tar -czf pi-hub-plugin-my-plugin-1.0.0.tar.gz pi_hub_plugins
   ```

### Store flow

1. Admin adds the repo URL (`https://github.com/<owner>/<repo>`) in
   Settings → Plugins.
2. Pi Hub scans the releases for `pihub-plugin.json` and lists the
   plugin (server-side asset resolution — the client never supplies
   URLs).
3. Install downloads the tarball, extracts **only** allowlisted files
   (`__init__.py`, `config.json`, `static/*`) with size caps and a
   `py_compile` gate, then atomically swaps it into `pi_hub_plugins/`.
4. Installed ≠ enabled (deny-by-default): the admin must click Enable,
   which loads it at runtime.

### Rules for store releases

- **One release = one plugin version.** Keep tags semver (`v1.2.3`).
- Both assets must be in the SAME release.
- Delete old releases when superseded, so scans stay clean.
- `min_core_version` must match the earliest Pi Hub version your plugin
  actually works on — the store flags incompatible plugins.

---

## 10. Checklist

- [ ] `name` matches the directory, `[A-Za-z0-9._-]{1,64}`
- [ ] `min_core_version` is honest
- [ ] `capabilities` lists exactly what `ctx` methods you call
- [ ] All `ctx` access goes through `PluginContext` (no `pi_hub.config`)
- [ ] Background loops check `thread_cancel()` and honor single-flight
- [ ] All user-facing strings escaped by the frontend renderer; payloads
      are flat JSON
- [ ] `python3 -m py_compile pi_hub_plugins/<name>/__init__.py` passes
- [ ] If store-distributed: release has BOTH assets, one plugin per
      release, semver tag
- [ ] English only (code, comments, UI, manifest)
- [ ] No dead code, no empty blocks, no leftover review-artifact
      comments

---

## 12. Reference example

`Valli-2020/pi-hub-plugin-proxmox-update-all` is a complete, store-ready
plugin: routes, background task with single-flight, `TabUIDef` UI,
`pct exec` SSH, toasts, and a clean release setup. Use it as the
starting point.
