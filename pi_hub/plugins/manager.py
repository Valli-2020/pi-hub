"""Plugin manager — discovery, load, unload, dispatch.

Reads the manifest at ``pi_hub_plugins/plugins.json``, loads each enabled
plugin, and wires its routes/tasks/UI into the Pi Hub server.

Security
--------
- Manifest is deny-by-default: only plugins listed in ``enabled`` load.
- Plugin routes are namespaced ``/api/plugin/<name>/`` — no collision.
- Static files use an exact-match map (no path traversal).
- Capabilities are checked per-call, not just at load time.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from pi_hub.plugins.base import (
    Plugin,
    PluginContext,
    PluginLoadError,
    RouteDef,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Module-level singletons
# ═══════════════════════════════════════════════════════════════════════════════

_lock = threading.Lock()
_instance: Optional["PluginManager"] = None

# Static file map: {url_path: (plugin_name, real_path)}
_static_map: Dict[str, Tuple[str, str]] = {}

# Toast buffer: list of (timestamp, plugin_name, message, kind)
_toasts: List[Dict[str, Any]] = []
_TOAST_TTL = 30.0  # seconds


def push_toast(plugin_name: str, message: str, kind: str = "info") -> None:
    """Add a toast to the global buffer (polled by the frontend)."""
    _toasts.append({
        "ts": time.time(),
        "plugin": plugin_name,
        "message": message,
        "kind": kind,
    })
    # Expire old toasts
    cutoff = time.time() - _TOAST_TTL
    while _toasts and _toasts[0]["ts"] < cutoff:
        _toasts.pop(0)


# ── Core restart signal ──────────────────────────────────────────────────

_restart_requested: Optional[str] = None


def request_core_restart(tag: str) -> None:
    """Request the core server to restart.  ``server.py``'s main loop
    checks ``get_restart_request()`` and performs the actual
    ``os._exit(0)`` (which systemd Restart=always catches)."""
    global _restart_requested
    _restart_requested = tag


def get_restart_request() -> Optional[str]:
    """Return the requested restart tag if any, else None.  Consumes
    the request (returns once)."""
    global _restart_requested
    tag = _restart_requested
    _restart_requested = None
    return tag


def register_plugin_static(
    plugin_name: str,
    url_path: str,
    file_path: str,
    plugin_dir: str,
) -> None:
    """Register a static file for a plugin.  ``file_path`` must resolve
    inside the plugin's own directory (anti-traversal)."""
    real_plugin = os.path.realpath(plugin_dir)
    real_file = os.path.realpath(os.path.join(plugin_dir, file_path))
    if not real_file.startswith(real_plugin + os.sep):
        raise PluginLoadError(
            f"Plugin '{plugin_name}': static path '{file_path}' escapes plugin dir"
        )
    # URL path is always /plugin-static/<name>/<relative>
    full_url = f"/plugin-static/{plugin_name}/{url_path.lstrip('/')}"
    _static_map[full_url] = (plugin_name, real_file)


class _LoadedPlugin:
    """Wrapper around a loaded plugin and its context."""

    def __init__(self, plugin: Plugin, ctx: PluginContext, directory: str):
        self.plugin = plugin
        self.ctx = ctx
        self.directory = directory
        self.status = "loaded"
        self.last_error = ""
        self.routes: List[RouteDef] = plugin.get_routes()
        self.tasks = plugin.get_tasks()
        self.ui = plugin.get_ui()
        self._thread_names: List[str] = []


class PluginManager:
    """Singleton plugin manager.  Access via :func:`get_instance`."""

    def __init__(self):
        self._plugins: Dict[str, _LoadedPlugin] = {}
        self._root = self._find_plugins_dir()

    @staticmethod
    def get_instance() -> "PluginManager":
        global _instance
        if _instance is None:
            with _lock:
                if _instance is None:
                    _instance = PluginManager()
        return _instance

    # ── Discovery ──────────────────────────────────────────────────────────

    @staticmethod
    def _find_plugins_dir() -> str:
        """Return the absolute path to ``pi_hub_plugins/`` (repo root
        level, sibling of ``pi_hub/``)."""
        pkg = os.path.dirname(os.path.abspath(__file__))  # pi_hub/plugins/
        pi_hub_pkg = os.path.dirname(pkg)  # pi_hub/
        repo = os.path.dirname(pi_hub_pkg)  # repo root
        return os.path.join(repo, "pi_hub_plugins")

    def _read_manifest(self) -> List[str]:
        """Read the enabled plugin list from ``plugins.json``.

        Deny-by-default: if the file is missing, no plugins load.
        An existing-but-invalid manifest still fails closed.
        """
        path = os.path.join(self._root, "plugins.json")
        if not os.path.isfile(path):
            return []
        try:
            with open(path) as f:
                data = json.load(f)
            enabled = data.get("enabled", [])
            return [str(e) for e in enabled if isinstance(e, str)]
        except (OSError, ValueError, KeyError):
            return []

    def _validate_name(self, name: str) -> bool:
        """Plugin names must be safe URL path segments — and must never
        be ``.`` / ``..`` / all-dots, which would escape the plugins root
        into the repo root."""
        return bool(re.match(r"^(?!\.{1,64}$)[A-Za-z0-9._-]{1,64}$", name))

    # ── Load ───────────────────────────────────────────────────────────────

    def load_all(self) -> None:
        """Load every enabled plugin from the manifest.  Failures are
        logged and the plugin is skipped (other plugins keep loading)."""
        enabled = self._read_manifest()
        for name in enabled:
            if not self._validate_name(name):
                print(f"PluginManager: invalid plugin name '{name}' — skipped")
                continue
            try:
                self.load_plugin(name)
            except PluginLoadError as e:
                print(f"PluginManager: failed to load '{name}': {e}")

    def load_plugin(self, name: str) -> None:
        """Load a single plugin by name."""
        plugin_dir = os.path.join(self._root, name)
        if not os.path.isdir(plugin_dir):
            raise PluginLoadError(f"plugin directory not found: {plugin_dir}")

        init_path = os.path.join(plugin_dir, "__init__.py")
        if not os.path.isfile(init_path):
            raise PluginLoadError(f"missing __init__.py in {plugin_dir}")

        # Import the plugin module
        spec = importlib.util.spec_from_file_location(
            f"pi_hub_plugins.{name}", init_path
        )
        if spec is None or spec.loader is None:
            raise PluginLoadError(f"cannot import plugin module: {name}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"pi_hub_plugins.{name}"] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            sys.modules.pop(f"pi_hub_plugins.{name}", None)
            raise PluginLoadError(f"import failed: {e}") from e

        # Find the Plugin subclass
        plugin_cls = None
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name)
            if isinstance(attr, type) and issubclass(attr, Plugin) and attr is not Plugin:
                plugin_cls = attr
                break
        if plugin_cls is None:
            raise PluginLoadError(f"no Plugin subclass found in {init_path}")

        # Instantiate and load
        plugin = plugin_cls()
        # Override name from directory if not set
        if not plugin.name:
            plugin.name = name
        # Check core version compatibility
        self._check_core_version(plugin)
        # Build context with declared capabilities
        ctx = PluginContext(plugin, plugin_dir, plugin.capabilities)
        loaded = _LoadedPlugin(plugin, ctx, plugin_dir)
        try:
            plugin.load(ctx)
        except Exception as e:
            ctx.unload()
            raise PluginLoadError(f"plugin load() raised: {e}") from e

        self._plugins[name] = loaded
        print(f"PluginManager: loaded '{name}' v{plugin.version}")

    @staticmethod
    def _check_core_version(plugin: Plugin) -> None:
        from pi_hub import __version__
        core = _semver_tuple(__version__)
        req = _semver_tuple(plugin.min_core_version)
        if core is not None and req is not None and core < req:
            raise PluginLoadError(
                f"plugin requires Pi Hub >= {plugin.min_core_version}, "
                f"running {__version__}"
            )

    # ── Unload ─────────────────────────────────────────────────────────────

    def unload_all(self) -> None:
        """Unload every loaded plugin (called on server shutdown)."""
        for name in list(self._plugins):
            self.unload_plugin(name)

    def unload_plugin(self, name: str) -> None:
        loaded = self._plugins.pop(name, None)
        if loaded is None:
            return
        try:
            loaded.ctx.unload()
        except Exception as e:
            print(f"PluginManager: error unloading '{name}': {e}")
        # Drop this plugin's static registrations — a disabled/uninstalled
        # plugin must not keep serving assets, and a later plugin installed
        # under the same name must not inherit stale entries.
        for url in [u for u, (p, _f) in _static_map.items() if p == name]:
            del _static_map[url]
        loaded.status = "unloaded"

    # ── Route dispatch ─────────────────────────────────────────────────────

    def dispatch(
        self,
        method: str,
        path: str,
        session: dict | None,
        body: dict | None = None,
    ) -> Tuple[Any, int] | None:
        """Dispatch a request to a plugin route.

        ``path`` is the full request path (e.g. ``/api/plugin/foo/containers``).
        Returns ``(data, status)`` or ``None`` if no plugin matches."""
        # Strip the /api/plugin/ prefix
        prefix = "/api/plugin/"
        if not path.startswith(prefix):
            return None
        rest = path[len(prefix):]
        # Find the plugin name (next path segment)
        parts = rest.split("/", 1)
        name = parts[0]
        sub_path = "/" + parts[1] if len(parts) > 1 else "/"

        loaded = self._plugins.get(name)
        if loaded is None:
            return {"error": f"Unknown plugin: {name}"}, 404

        # Match method + path
        for route in loaded.routes:
            if route.method != method:
                continue
            if route.path != sub_path:
                continue
            # Check user caps.  Plugin route caps are capability names
            # ("admin" is special-cased as the role check).  For
            # non-admin caps the user's caps dict must list the
            # capability AND permit at least one target — an empty list
            # means explicitly permitted nothing and must NOT pass.
            user_role = (session or {}).get("role", "")
            user_caps = (session or {}).get("caps", {}) if session else {}
            for cap in route.caps:
                if cap == "admin" and user_role != "admin":
                    return {"error": "Forbidden"}, 403
                if cap != "admin":
                    targets = user_caps.get(cap) if isinstance(user_caps, dict) else None
                    allowed = (isinstance(targets, list) and len(targets) > 0) \
                        or user_role == "admin"
                    if not allowed:
                        return {"error": "Forbidden"}, 403
            try:
                result = route.handler(session=session, body=body or {})
                if isinstance(result, tuple):
                    return result
                return result, 200
            except Exception as e:
                return {"error": f"Plugin error: {e}"}, 500

        return {"error": f"Not found in plugin {name}"}, 404

    # ── Status ─────────────────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """Return plugin status for the ``/api/plugins/list`` endpoint."""
        return {
            name: {
                "name": lp.plugin.name,
                "version": lp.plugin.version,
                "description": lp.plugin.description,
                "status": lp.status,
                "last_error": lp.last_error,
                "capabilities": lp.plugin.capabilities,
                "routes": [{"method": r.method, "path": r.path} for r in lp.routes],
                "tasks": [t.name for t in lp.tasks],
                "ui": _serialize_ui(lp.ui),
            }
            for name, lp in self._plugins.items()
        }

    # ── Static files ───────────────────────────────────────────────────────

    def serve_static(self, path: str) -> Tuple[bytes, str] | None:
        """Return ``(data, content_type)`` for a plugin static file, or
        None if not found.  Uses exact-match map (no traversal)."""
        entry = _static_map.get(path)
        if entry is None:
            return None
        _, real_path = entry
        try:
            with open(real_path, "rb") as f:
                data = f.read()
        except OSError:
            return None
        content_type = _guess_content_type(real_path)
        return data, content_type


def _semver_tuple(v: str) -> tuple | None:
    """Parse '7.1.0' → (7, 1, 0).  Returns None on parse failure."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", v or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _serialize_ui(ui_list: list) -> list[dict]:
    """Serialize UI descriptors to JSON-friendly dicts."""
    out = []
    for item in ui_list:
        if hasattr(item, "id"):  # TabUIDef or ActionDef-like
            out.append({
                "type": type(item).__name__,
                "id": item.id,
                "label": getattr(item, "label", ""),
                "icon_svg": getattr(item, "icon_svg", ""),
                "position": getattr(item, "position", 99),
                "poll_endpoint": getattr(item, "poll_endpoint", ""),
                "title": getattr(item, "title", ""),
                "caps": getattr(item, "caps", []),
                "actions": [
                    {
                        "id": a.id,
                        "label": a.label,
                        "style": a.style,
                        "caps": a.caps,
                    }
                    for a in getattr(item, "actions", [])
                    if hasattr(a, "id")
                ],
            })
        else:
            out.append({"type": type(item).__name__})
    return out


def _guess_content_type(path: str) -> str:
    """Return a safe Content-Type for a static file.  Never returns
    text/html (prevents stored XSS from plugin uploads)."""
    ext = os.path.splitext(path)[1].lower()
    return {
        ".js": "application/javascript",
        ".css": "text/css",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".json": "application/json",
        ".map": "application/json",
        ".woff2": "font/woff2",
    }.get(ext, "application/octet-stream")
