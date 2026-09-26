"""Plugin manager — discovery, consent, load, unload, dispatch, contributions.

Reads the manifest at ``pi_hub_plugins/plugins.json``, loads each enabled
plugin, and wires its routes/tasks/UI into the Pi Hub server.

Security
--------
- Manifest is deny-by-default: only plugins listed in ``enabled`` load.
- **Consent (API v2):** a plugin only loads when the admin approved every
  capability it declares.  Approvals live in ``plugin_state.json`` (NOT in
  ``plugins.json``, which the store rewrites).  Declarations are read
  *without importing* the plugin (``pihub-plugin.json`` or an AST scan of
  ``__init__.py``) so no plugin code runs before consent.  An update that
  declares new capabilities waits for re-approval.  Grants are consent and
  a kill-switch, not a sandbox: plugin Python runs in-process.
- Plugin routes are namespaced ``/api/plugin/<name>/`` — no collision.
- Static files use an exact-match map (no path traversal).
- Capabilities are checked per-call, not just at load time.
- Contribution payloads are validated by :mod:`pi_hub.plugins.contrib`
  before they reach a browser.
"""

from __future__ import annotations

import ast
import concurrent.futures
import importlib
import importlib.util
import inspect
import json
import os
import re
import sys
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from pi_hub.plugins import contrib, events, frame as frame_mod
from pi_hub.plugins.base import (
    Contribution,
    FrameDef,
    Plugin,
    PluginContext,
    PluginLoadError,
    RouteDef,
)

#: Plugin API levels this core understands.
SUPPORTED_API = (1, 2)

# ═══════════════════════════════════════════════════════════════════════════════
# Module-level singletons
# ═══════════════════════════════════════════════════════════════════════════════

_lock = threading.Lock()
_instance: Optional["PluginManager"] = None

# Static file map: {url_path: (plugin_name, real_path)}
_static_map: Dict[str, Tuple[str, str]] = {}

# Toast buffer: list of {seq, ts, plugin, message, kind}
_toasts: List[Dict[str, Any]] = []
_toast_lock = threading.Lock()
_toast_seq = 0
_TOAST_TTL = 60.0  # seconds
_TOAST_MAX = 100
_TOAST_KINDS = {"info": "info", "ok": "ok", "success": "ok", "err": "err",
                "error": "err", "warn": "info", "warning": "info"}


def push_toast(plugin_name: str, message: str, kind: str = "info") -> None:
    """Add a toast to the global buffer.  Delivered to browsers through
    ``GET /api/plugins/contrib?since=<seq>``."""
    global _toast_seq
    with _toast_lock:
        _toast_seq += 1
        _toasts.append({
            "seq": _toast_seq,
            "ts": time.time(),
            "plugin": str(plugin_name)[:64],
            "message": str(message)[:300],
            "kind": _TOAST_KINDS.get(str(kind).lower(), "info"),
        })
        cutoff = time.time() - _TOAST_TTL
        while _toasts and (_toasts[0]["ts"] < cutoff or len(_toasts) > _TOAST_MAX):
            _toasts.pop(0)


def toasts_since(seq: int) -> List[Dict[str, Any]]:
    with _toast_lock:
        return [dict(t) for t in _toasts if t["seq"] > seq]


def toast_seq() -> int:
    with _toast_lock:
        return _toast_seq


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


# ═══════════════════════════════════════════════════════════════════════════════
# Route matching
# ═══════════════════════════════════════════════════════════════════════════════

_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_PARAM_VALUE = r"[A-Za-z0-9._:~-]{1,128}"
_ROUTE_METHODS = ("GET", "POST", "PUT", "DELETE")


class _CompiledRoute:
    __slots__ = ("route", "regex", "exact")

    def __init__(self, route: RouteDef):
        self.route = route
        path = route.path if route.path.startswith("/") else "/" + route.path
        self.exact = _PARAM_RE.search(path) is None
        if self.exact:
            self.regex = None
        else:
            pat, last = "", 0
            for m in _PARAM_RE.finditer(path):
                pat += re.escape(path[last:m.start()])
                pat += "(?P<%s>%s)" % (m.group(1), _PARAM_VALUE)
                last = m.end()
            pat += re.escape(path[last:])
            self.regex = re.compile("^" + pat + "$")


def _call_filtered(fn: Callable[..., Any], **kw: Any) -> Any:
    """Call *fn* with only the keyword arguments its signature accepts, so
    a v1 handler ``(session, body)`` keeps working next to a v2 handler
    ``(session, body, params, query)``.  ``**kwargs`` receives everything."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(**kw)
    params = sig.parameters.values()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
        return fn(**kw)
    names = {p.name for p in sig.parameters.values()
             if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                           inspect.Parameter.KEYWORD_ONLY)}
    return fn(**{k: v for k, v in kw.items() if k in names})


def caps_allowed(caps: List[str], session: dict | None) -> bool:
    """Route-style capability check.  ``"admin"`` is the role check; any
    other name needs a non-empty ``session['caps'][name]`` list (or admin)."""
    user_role = (session or {}).get("role", "")
    user_caps = (session or {}).get("caps", {}) if session else {}
    for cap in caps or []:
        if cap == "admin":
            if user_role != "admin":
                return False
            continue
        targets = user_caps.get(cap) if isinstance(user_caps, dict) else None
        if not ((isinstance(targets, list) and len(targets) > 0) or user_role == "admin"):
            return False
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# Loaded plugin wrapper
# ═══════════════════════════════════════════════════════════════════════════════


class _LoadedContribution:
    __slots__ = ("c", "plugin", "cid", "meta", "static", "granted",
                 "errors", "next_ok", "cache")

    def __init__(self, c: Contribution, plugin: str, meta: dict, static: Any,
                 granted: bool):
        self.c = c
        self.plugin = plugin
        self.cid = "%s/%s" % (plugin, c.id)
        self.meta = meta
        self.static = static
        self.granted = granted
        self.errors = 0
        self.next_ok = 0.0
        self.cache: Dict[str, Tuple[float, dict]] = {}

    @property
    def poll(self) -> int:
        try:
            p = int(self.c.poll)
        except (TypeError, ValueError):
            p = 30
        return 0 if p <= 0 else max(5, min(3600, p))


class _LoadedPlugin:
    """Wrapper around a loaded plugin and its context."""

    def __init__(self, plugin: Plugin, ctx: PluginContext, directory: str):
        self.plugin = plugin
        self.ctx = ctx
        self.directory = directory
        self.status = "loaded"
        self.last_error = ""
        self.api_version = int(getattr(plugin, "plugin_api_version", 1) or 1)
        self.routes: List[RouteDef] = plugin.get_routes()
        self.compiled: List[_CompiledRoute] = [
            _CompiledRoute(r) for r in self.routes if r.method in _ROUTE_METHODS]
        # exact routes win over parameterised ones
        self.compiled.sort(key=lambda cr: 0 if cr.exact else 1)
        self.tasks = plugin.get_tasks()
        self.ui = plugin.get_ui()
        self.contribs: List[_LoadedContribution] = []
        self.denied: List[str] = []
        self.config_schema: List[dict] = []
        self.granted: List[str] = []
        self.frames: Dict[str, frame_mod.LoadedFrame] = {}
        self._thread_names: List[str] = []


# ═══════════════════════════════════════════════════════════════════════════════
# Manager
# ═══════════════════════════════════════════════════════════════════════════════

_MAX_CONTRIBS_PER_PLUGIN = 24
_MAX_PER_SLOT_PER_PLUGIN = {"containers.column": 2}
_DEFAULT_PER_SLOT = 4
_MAX_PLUGIN_COLUMNS = 6
_PROVIDER_BUDGET = 3.0          # seconds per batch
_MAX_BATCH = 40

_pool: Optional[concurrent.futures.ThreadPoolExecutor] = None
_pool_lock = threading.Lock()


def _get_pool() -> concurrent.futures.ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=8, thread_name_prefix="plg-contrib")
        return _pool


class PluginManager:
    """Singleton plugin manager.  Access via :func:`get_instance`."""

    def __init__(self):
        self._plugins: Dict[str, _LoadedPlugin] = {}
        self._failed: Dict[str, Dict[str, Any]] = {}
        self._root = self._find_plugins_dir()
        self._rev = 1
        self._state_lock = threading.RLock()
        self._state: Optional[dict] = None
        self._safe = os.environ.get("PIHUB_SAFE_MODE", "") == "1"

    @staticmethod
    def get_instance() -> "PluginManager":
        global _instance
        if _instance is None:
            with _lock:
                if _instance is None:
                    _instance = PluginManager()
        return _instance

    # ── Safe mode ──────────────────────────────────────────────────────────

    def set_safe_mode(self, on: bool) -> None:
        self._safe = bool(on)

    @property
    def safe_mode(self) -> bool:
        return self._safe

    @property
    def rev(self) -> int:
        return self._rev

    def _bump(self) -> None:
        self._rev += 1

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
        except (OSError, ValueError, KeyError, AttributeError):
            return []

    def _validate_name(self, name: str) -> bool:
        """Plugin names must be safe URL path segments — and must never
        be ``.`` / ``..`` / all-dots, which would escape the plugins root
        into the repo root."""
        return bool(re.match(r"^(?!\.{1,64}$)[A-Za-z0-9._-]{1,64}$", name))

    # ── Persistent state (grants, appearance) ─────────────────────────────

    def _state_path(self) -> str:
        return os.path.join(self._root, "plugin_state.json")

    def _load_state(self) -> dict:
        with self._state_lock:
            if self._state is not None:
                return self._state
            st: dict = {}
            try:
                with open(self._state_path()) as f:
                    st = json.load(f)
                if not isinstance(st, dict):
                    st = {}
            except (OSError, ValueError):
                st = {}
            st.setdefault("version", 1)
            if not isinstance(st.get("grants"), dict):
                st["grants"] = {}
            if not isinstance(st.get("active"), dict):
                st["active"] = {}
            self._state = st
            return st

    def _save_state(self) -> bool:
        with self._state_lock:
            st = self._load_state()
            tmp = self._state_path() + ".tmp"
            try:
                os.makedirs(self._root, exist_ok=True)
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as f:
                    f.write(json.dumps(st, indent=2))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self._state_path())
                return True
            except OSError as e:
                print(f"PluginManager: could not write plugin_state.json: {e}")
                return False

    def _grant_of(self, name: str) -> dict:
        g = self._load_state()["grants"].get(name)
        return g if isinstance(g, dict) else {}

    def granted_caps(self, name: str) -> List[str]:
        caps = self._grant_of(name).get("caps", [])
        return [c for c in caps if isinstance(c, str)] if isinstance(caps, list) else []

    def _grandfather(self, enabled: List[str]) -> None:
        """First 8.0 boot: no plugin_state.json yet — every already-enabled
        plugin keeps working with its declared capabilities."""
        if os.path.isfile(self._state_path()):
            return
        st = self._load_state()
        for name in enabled:
            if not self._validate_name(name):
                continue
            declared = self.declared_caps(name)
            st["grants"][name] = {
                "caps": list(declared or []),
                "version": "",
                "approved_at": int(time.time()),
                "grandfathered": True,
            }
        self._save_state()

    # ── Declared capabilities (without importing the plugin) ──────────────

    def declared_caps(self, name: str) -> Optional[List[str]]:
        """Capabilities a plugin declares, read WITHOUT importing it:
        ``pihub-plugin.json`` ``capabilities`` if present, else an AST scan
        for literal ``capabilities = [...]`` assignments in ``__init__.py``.
        Returns ``None`` when the declaration cannot be determined."""
        if not self._validate_name(name):
            return None
        d = os.path.join(self._root, name)
        mf = os.path.join(d, "pihub-plugin.json")
        if os.path.isfile(mf):
            try:
                with open(mf) as f:
                    data = json.load(f)
                caps = data.get("capabilities", [])
                if isinstance(caps, list) and all(isinstance(c, str) for c in caps):
                    return sorted(set(caps))
            except (OSError, ValueError, AttributeError):
                pass
            return None
        init = os.path.join(d, "__init__.py")
        try:
            with open(init, encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except (OSError, SyntaxError, ValueError):
            return None
        found: set = set()
        for node in ast.walk(tree):
            target = value = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign):
                target, value = node.target, node.value
            if target is None or value is None:
                continue
            tname = target.id if isinstance(target, ast.Name) else None
            if tname != "capabilities":
                continue
            try:
                lit = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return None
            if not isinstance(lit, (list, tuple)) or not all(isinstance(c, str) for c in lit):
                return None
            found.update(lit)
        return sorted(found)

    def approval_status(self, name: str) -> Dict[str, Any]:
        declared = self.declared_caps(name)
        granted = set(self.granted_caps(name))
        pending = [c for c in (declared or []) if c not in granted]
        return {
            "declared": declared,
            "granted": sorted(granted),
            "pending": pending,
            "known": declared is not None,
        }

    def approve(self, name: str, caps: List[str]) -> Tuple[bool, str]:
        """Record the admin's approval.  The admin must approve the whole
        declared set (a plugin with a partial grant would run half-broken)."""
        if not self._validate_name(name):
            return False, "invalid plugin name"
        st = self.approval_status(name)
        if not st["known"]:
            return False, ("cannot read the plugin's capabilities — declare them "
                           "as a literal list or ship pihub-plugin.json")
        want = set(caps or [])
        missing = [c for c in st["declared"] if c not in want]
        if missing:
            return False, "all declared capabilities must be approved: " + ", ".join(missing)
        with self._state_lock:
            s = self._load_state()
            old = self._grant_of(name)
            s["grants"][name] = {
                "caps": list(st["declared"]),
                "version": old.get("version", ""),
                "approved_at": int(time.time()),
                "ui_off": bool(old.get("ui_off", False)),
                "frames": dict(old.get("frames") or {}),
            }
            lp = self._plugins.get(name)
            if lp is not None:
                # Re-approval re-pins what is loaded and unblocks the frames.
                for fid, lf in lp.frames.items():
                    s["grants"][name]["frames"][fid] = {"sha": lf.digest, "version": lp.plugin.version}
                    lf.blocked = ""
            if not self._save_state():
                return False, "could not write plugin_state.json"
        frame_mod.revoke(plugin=name)
        self._bump()
        return True, "approved"

    def forget(self, name: str) -> None:
        """Drop grants of an uninstalled plugin."""
        with self._state_lock:
            s = self._load_state()
            changed = s["grants"].pop(name, None) is not None
            for slot in ("theme", "layout"):
                if str(s["active"].get(slot, "")).startswith(name + "/"):
                    s["active"].pop(slot, None)
                    changed = True
            if changed:
                self._save_state()
        self._failed.pop(name, None)
        frame_mod.revoke(plugin=name)

    def set_ui_off(self, name: str, off: bool) -> Tuple[bool, str]:
        if name not in self._plugins and name not in self._load_state()["grants"]:
            return False, "unknown plugin"
        with self._state_lock:
            s = self._load_state()
            g = s["grants"].setdefault(name, {"caps": [], "version": "", "approved_at": 0})
            g["ui_off"] = bool(off)
            if not self._save_state():
                return False, "could not write plugin_state.json"
        if off:
            frame_mod.revoke(plugin=name)
        self._bump()
        return True, "ok"

    # ── Load ───────────────────────────────────────────────────────────────

    def load_all(self) -> None:
        """Load every enabled plugin from the manifest.  Failures are
        logged, remembered (shown in Settings) and the plugin is skipped
        (other plugins keep loading)."""
        if self._safe:
            print("PluginManager: SAFE MODE — no plugins loaded")
            return
        enabled = self._read_manifest()
        self._grandfather(enabled)
        for name in enabled:
            if not self._validate_name(name):
                print(f"PluginManager: invalid plugin name '{name}' — skipped")
                continue
            try:
                self.load_plugin(name)
            except PluginLoadError as e:
                print(f"PluginManager: failed to load '{name}': {e}")

    def load_plugin(self, name: str) -> None:
        """Load a single plugin by name (records the failure on error)."""
        try:
            self._load_plugin(name)
        except PluginLoadError as e:
            cur = self._failed.get(name)
            if not cur or cur.get("status") != "needs_approval":
                self._failed[name] = {"status": "error", "error": str(e)}
            self._bump()
            raise
        except Exception as e:                       # unexpected: keep the server up
            traceback.print_exc()
            self._failed[name] = {"status": "error", "error": f"unexpected: {e}"}
            self._bump()
            raise PluginLoadError(f"unexpected error: {e}") from e

    def _load_plugin(self, name: str) -> None:
        if self._safe:
            raise PluginLoadError("safe mode is on")
        if not self._validate_name(name):
            raise PluginLoadError("invalid plugin name")
        plugin_dir = os.path.join(self._root, name)
        if not os.path.isdir(plugin_dir):
            raise PluginLoadError(f"plugin directory not found: {plugin_dir}")

        init_path = os.path.join(plugin_dir, "__init__.py")
        if not os.path.isfile(init_path):
            raise PluginLoadError(f"missing __init__.py in {plugin_dir}")

        # Consent gate — BEFORE any plugin code is imported.
        st = self.approval_status(name)
        if not st["known"]:
            raise PluginLoadError(
                "cannot determine the plugin's capabilities without importing it — "
                "declare them as a literal list or ship pihub-plugin.json")
        if st["pending"]:
            self._failed[name] = {"status": "needs_approval", "pending": st["pending"],
                                  "declared": st["declared"],
                                  "error": "waiting for approval: " + ", ".join(st["pending"])}
            raise PluginLoadError("needs approval: " + ", ".join(st["pending"]))
        declared = set(st["declared"])

        # Import the plugin module
        spec = importlib.util.spec_from_file_location(
            f"pi_hub_plugins.{name}", init_path,
            submodule_search_locations=[plugin_dir],
        )
        if spec is None or spec.loader is None:
            raise PluginLoadError(f"cannot import plugin module: {name}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"pi_hub_plugins.{name}"] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            self._drop_modules(name)
            raise PluginLoadError(f"import failed: {e}") from e

        try:
            self._finish_load(name, plugin_dir, init_path, mod, declared)
        except BaseException:
            self._drop_modules(name)
            raise

    def _finish_load(self, name: str, plugin_dir: str, init_path: str,
                     mod: Any, declared: set) -> None:
        # Find the Plugin subclass.  An explicit PLUGIN_CLASS wins; the
        # scan is the fallback and is deliberate about what it accepts —
        # dir() is alphabetical, so a module that also defines a shared
        # base class or imports another plugin's class would otherwise
        # load whichever name sorts first.
        plugin_cls = getattr(mod, "PLUGIN_CLASS", None)
        if not (isinstance(plugin_cls, type) and issubclass(plugin_cls, Plugin)):
            candidates = [
                attr for attr in (getattr(mod, n) for n in dir(mod))
                if isinstance(attr, type) and issubclass(attr, Plugin)
                and attr is not Plugin
                and attr.__module__ == mod.__name__   # defined here, not imported
            ]
            if len(candidates) > 1:
                names = ", ".join(sorted(c.__name__ for c in candidates))
                raise PluginLoadError(
                    f"{init_path} defines several Plugin subclasses ({names}) — "
                    "set PLUGIN_CLASS to the one to load")
            plugin_cls = candidates[0] if candidates else None
        if plugin_cls is None:
            raise PluginLoadError(f"no Plugin subclass found in {init_path}")

        plugin = plugin_cls()
        # D5 (review 2026-08-18): the manager keys everything by the
        # DIRECTORY name, but PluginContext addresses plugins by
        # ``plugin.name``.  A plugin that names itself differently would
        # hijack foreign static registrations and task status.  PLUGINS.md
        # requires the name to match the directory — enforce it.
        if plugin.name and plugin.name != name:
            raise PluginLoadError(
                f"plugin name '{plugin.name}' does not match directory "
                f"'{name}' (PLUGINS.md: name must equal the directory name)")
        plugin.name = name

        api = getattr(plugin, "plugin_api_version", 1)
        if not isinstance(api, int) or isinstance(api, bool) or api not in SUPPORTED_API:
            raise PluginLoadError(
                f"unsupported plugin_api_version {api!r} (this core supports "
                f"{', '.join(str(a) for a in SUPPORTED_API)})")
        self._check_core_version(plugin)

        # The runtime declaration must match what the admin approved.
        actual = set(plugin.capabilities or [])
        extra = sorted(actual - declared)
        if extra:
            raise PluginLoadError(
                "plugin declares capabilities that were not in its approved "
                "declaration: " + ", ".join(extra))
        system_caps = sorted(c for c in actual if not c.startswith("ui."))
        ctx = PluginContext(plugin, plugin_dir, system_caps)

        # Config migration (version changed since the last start).
        g = self._grant_of(name)
        old_ver = str(g.get("version") or "")
        if old_ver and old_ver != plugin.version:
            try:
                new_cfg = plugin.migrate_config(old_ver, dict(ctx.get_config()))
                if isinstance(new_cfg, dict) and new_cfg != ctx.get_config():
                    ctx._replace_config(new_cfg)
            except Exception:
                traceback.print_exc()
                print(f"PluginManager: migrate_config failed for '{name}'")

        try:
            plugin.load(ctx)
        except Exception as e:
            ctx.unload()
            raise PluginLoadError(f"plugin load() raised: {e}") from e

        try:
            loaded = _LoadedPlugin(plugin, ctx, plugin_dir)
            loaded.granted = sorted(actual)
            self._collect_contributions(name, loaded, actual)
            self._collect_frames(name, loaded, actual, plugin_dir)
            loaded.config_schema = contrib.validate_config_schema(plugin.get_config_schema())
        except Exception as e:
            ctx.unload()
            raise PluginLoadError(f"invalid plugin descriptors: {e}") from e

        self._plugins[name] = loaded
        self._failed.pop(name, None)

        # Version stamp for the next migrate_config decision.
        with self._state_lock:
            s = self._load_state()
            s["grants"].setdefault(name, {"caps": sorted(declared), "approved_at": int(time.time())})
            s["grants"][name]["version"] = plugin.version
            self._pin_frames(s, name, loaded)
            self._save_state()

        # Tasks (API v2: they finally run).
        for t in loaded.tasks:
            if getattr(t, "autostart", True) and callable(getattr(t, "fn", None)):
                try:
                    ctx.run_task(t.name, t.fn, interval=getattr(t, "interval", 0))
                except Exception:
                    traceback.print_exc()

        # Events.
        wanted = [e for e in (plugin.events or []) if isinstance(e, str)]
        if type(plugin).on_host_state_change is not Plugin.on_host_state_change:
            wanted.append("host.state")
        if type(plugin).on_scan_complete is not Plugin.on_scan_complete:
            wanted.append("scan.complete")
        if wanted:
            events.subscribe(name, list(dict.fromkeys(wanted)),
                             self._event_fn(plugin), actual)

        self._bump()
        print(f"PluginManager: loaded '{name}' v{plugin.version} (api {api})")

    @staticmethod
    def _event_fn(plugin: Plugin) -> Callable[[str, dict], None]:
        def fn(event: str, payload: dict) -> None:
            if event in (plugin.events or []):
                plugin.on_event(event, payload)
            if event == "host.state" and \
                    type(plugin).on_host_state_change is not Plugin.on_host_state_change:
                plugin.on_host_state_change(str(payload.get("host_id", "")),
                                            str(payload.get("new", "")))
            if event == "scan.complete" and \
                    type(plugin).on_scan_complete is not Plugin.on_scan_complete:
                plugin.on_scan_complete(dict(payload))
        return fn

    def _collect_contributions(self, name: str, loaded: _LoadedPlugin, caps: set) -> None:
        raw = loaded.plugin.get_contributions() or []
        if len(raw) > _MAX_CONTRIBS_PER_PLUGIN:
            raise ValueError(f"too many contributions (max {_MAX_CONTRIBS_PER_PLUGIN})")
        seen: set = set()
        per_slot: Dict[str, int] = {}
        for c in raw:
            if not isinstance(c, Contribution):
                raise ValueError("get_contributions() must return Contribution objects")
            meta = contrib.SLOTS.get(c.slot)
            if meta is None:
                raise ValueError(f"unknown slot {c.slot!r}")
            if not isinstance(c.id, str) or not contrib.CONTRIB_ID_RE.match(c.id):
                raise ValueError(f"invalid contribution id {c.id!r} (use [a-z0-9-], max 32)")
            if c.id in seen:
                raise ValueError(f"duplicate contribution id {c.id!r}")
            seen.add(c.id)
            per_slot[c.slot] = per_slot.get(c.slot, 0) + 1
            if per_slot[c.slot] > _MAX_PER_SLOT_PER_PLUGIN.get(c.slot, _DEFAULT_PER_SLOT):
                raise ValueError(f"too many contributions in slot {c.slot!r}")
            static = None
            if meta.get("static"):
                static = contrib.validate_static(c.slot, c.static, name)
            elif not callable(c.provider):
                raise ValueError(f"contribution {c.id!r} needs a provider")
            need = meta["cap"]
            if c.slot == "style" and static and static.get("global"):
                need = "ui.style.global"
            granted = need in caps
            if not granted:
                loaded.denied.append(f"{c.slot}:{c.id} (needs {need})")
            loaded.contribs.append(_LoadedContribution(c, name, meta, static, granted))

    #: slots a frame may feed through ph.render → the ui.* capability they need
    @staticmethod
    def _frame_render_slots() -> Dict[str, str]:
        return {k: m["cap"] for k, m in contrib.SLOTS.items()
                if not m.get("static") and k != "tab"}

    def _collect_frames(self, name: str, loaded: _LoadedPlugin, caps: set, plugin_dir: str) -> None:
        raw = loaded.plugin.get_frames() or []
        if len(raw) > 4:
            raise ValueError("too many frames (max 4)")
        for fd in raw:
            if not isinstance(fd, FrameDef):
                raise ValueError("get_frames() must return FrameDef objects")
            if "ui.frame" not in caps:
                loaded.denied.append(f"frame:{getattr(fd, 'id', '?')} (needs ui.frame)")
                continue
            try:
                lf = frame_mod.load_frame(name, plugin_dir, fd, caps, self._frame_render_slots())
            except frame_mod.FrameError as e:
                raise ValueError(f"frame {getattr(fd, 'id', '?')!r}: {e}") from e
            if lf.id in loaded.frames:
                raise ValueError(f"duplicate frame id {lf.id!r}")
            loaded.frames[lf.id] = lf

    @staticmethod
    def _pin_frames(state: dict, name: str, loaded: _LoadedPlugin) -> None:
        """Digest pinning.  The digest of a frame's files is recorded the
        first time the plugin loads after approval.  Later loads of the SAME
        plugin version must match it — a frame edited in place is blocked
        until the admin approves again.  A new plugin version re-pins (an
        update the admin installed).  Caller holds the state lock."""
        g = state["grants"].setdefault(name, {"caps": [], "version": "", "approved_at": 0})
        pins = g.setdefault("frames", {})
        ver = loaded.plugin.version
        for fid, lf in loaded.frames.items():
            pin = pins.get(fid)
            if pin and pin.get("version") == ver and pin.get("sha") != lf.digest:
                lf.blocked = "frame files changed since they were approved — approve again"
            else:
                pins[fid] = {"sha": lf.digest, "version": ver}
        for fid in [f for f in pins if f not in loaded.frames]:
            del pins[fid]

    def frames_manifest(self, session: dict | None) -> List[Dict[str, Any]]:
        """Frames the caller may mount (part of ``/api/plugins/ui``)."""
        out: List[Dict[str, Any]] = []
        if self._safe:
            return out
        role = (session or {}).get("role", "")
        for name in sorted(self._plugins):
            lp = self._plugins[name]
            if self._grant_of(name).get("ui_off"):
                continue
            for fid, lf in sorted(lp.frames.items()):
                if lf.blocked:
                    continue
                pub = lf.public()
                pub["surfaces"] = [sf for sf in pub["surfaces"]
                                   if not (sf["type"] == "settings" and role != "admin")]
                if pub["surfaces"]:
                    out.append(pub)
        return out

    def frame_ticket(self, session: dict | None, plugin: str, frame_id: str) -> Tuple[Dict[str, Any], int]:
        """Issue the iframe URL + scoped frame token (``POST /api/plugins/frames/ticket``)."""
        lp = self._plugins.get(plugin) if isinstance(plugin, str) else None
        lf = lp.frames.get(frame_id) if lp and isinstance(frame_id, str) else None
        if lf is None or self._safe:
            return {"error": "Unknown frame"}, 404
        if self._grant_of(plugin).get("ui_off"):
            return {"error": "Plugin UI is switched off"}, 403
        if lf.blocked:
            return {"error": lf.blocked, "needs_approval": True}, 409
        if not any(sf["type"] != "settings" or (session or {}).get("role") == "admin"
                   for sf in lf.spec["surfaces"]):
            return {"error": "Forbidden"}, 403
        got = frame_mod.issue(str((session or {}).get("user", "")), plugin, frame_id, lf.spec["reads"])
        if got is None:
            return {"error": "Too many frame tickets — slow down"}, 429
        ticket, token = got
        return {"url": f"/plugin-frame/{plugin}/{frame_id}?t={ticket}", "frame_token": token,
                "frame": lf.public()}, 200

    def frame_document(self, plugin: str, frame_id: str, ticket: str) -> Tuple[Optional[Tuple[bytes, str]], int]:
        """Consume *ticket* and return ``((html, csp), 200)`` or ``(None, status)``."""
        res = frame_mod.consume_ticket(ticket, plugin, frame_id)
        if res == "gone":
            return None, 410
        if res != "ok":
            return None, 403
        lp = self._plugins.get(plugin)
        lf = lp.frames.get(frame_id) if lp else None
        if lf is None or lf.blocked or self._safe or self._grant_of(plugin).get("ui_off"):
            return None, 404
        return frame_mod.build_document(lf), 200

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

    @staticmethod
    def _drop_modules(name: str) -> None:
        """Remove the plugin module AND its submodules so a later reinstall
        loads FRESH code (a multi-file plugin such as hermes would
        otherwise keep serving the old ``pi_hub_plugins.<name>.auth``)."""
        base = f"pi_hub_plugins.{name}"
        for key in list(sys.modules):
            if key == base or key.startswith(base + "."):
                sys.modules.pop(key, None)

    # ── Unload ─────────────────────────────────────────────────────────────

    def unload_all(self) -> None:
        """Unload every loaded plugin (called on server shutdown)."""
        for name in list(self._plugins):
            self.unload_plugin(name)

    def unload_plugin(self, name: str) -> None:
        loaded = self._plugins.pop(name, None)
        self._failed.pop(name, None)
        if loaded is None:
            return
        events.unsubscribe(name)
        frame_mod.revoke(plugin=name)
        try:
            loaded.ctx.unload()
        except Exception as e:
            print(f"PluginManager: error unloading '{name}': {e}")
        # Drop this plugin's static registrations — a disabled/uninstalled
        # plugin must not keep serving assets, and a later plugin installed
        # under the same name must not inherit stale entries.
        for url in [u for u, (p, _f) in _static_map.items() if p == name]:
            del _static_map[url]
        # D6 (review 2026-08-18): drop the module from sys.modules so a
        # later reinstall loads a FRESH module instead of the stale one
        # (and so the old module can be GC'd with its threads' references).
        self._drop_modules(name)
        loaded.status = "unloaded"
        self._bump()

    # ── Route dispatch ─────────────────────────────────────────────────────

    def dispatch(
        self,
        method: str,
        path: str,
        session: dict | None,
        body: dict | None = None,
        query: dict | None = None,
    ) -> Tuple[Any, int] | None:
        """Dispatch a request to a plugin route.

        ``path`` is the full request path (e.g. ``/api/plugin/foo/containers``).
        Routes may contain ``{param}`` segments (exact routes win).  Returns
        ``(data, status)`` or ``None`` if no plugin matches."""
        prefix = "/api/plugin/"
        if not path.startswith(prefix):
            return None
        rest = path[len(prefix):]
        parts = rest.split("/", 1)
        name = parts[0]
        sub_path = "/" + parts[1] if len(parts) > 1 else "/"

        loaded = self._plugins.get(name)
        if loaded is None:
            return {"error": f"Unknown plugin: {name}"}, 404

        for cr in loaded.compiled:
            route = cr.route
            if route.method != method:
                continue
            params: Dict[str, str] = {}
            if cr.exact:
                rp = route.path if route.path.startswith("/") else "/" + route.path
                if rp != sub_path:
                    continue
            else:
                m = cr.regex.match(sub_path)
                if not m:
                    continue
                params = m.groupdict()
            # Check user caps (see caps_allowed).
            if not caps_allowed(route.caps, session):
                return {"error": "Forbidden"}, 403
            flat_query = {k: (v[0] if isinstance(v, list) and v else v)
                          for k, v in (query or {}).items()}
            try:
                result = _call_filtered(route.handler, session=session,
                                        body=body or {}, params=params,
                                        query=flat_query)
                if isinstance(result, tuple):
                    return result
                return result, 200
            except Exception:
                # Log the detail, return a generic message: the exception
                # text carries file paths and internals, and every
                # authenticated user (including a viewer) sees this.
                traceback.print_exc()
                return {"error": f"Plugin '{name}' failed — see server log"}, 500

        return {"error": f"Not found in plugin {name}"}, 404

    # ── Status ─────────────────────────────────────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """Return plugin status for the ``/api/plugins/list`` endpoint."""
        out: Dict[str, Any] = {}
        st = self._load_state()
        for name, lp in self._plugins.items():
            out[name] = {
                "name": lp.plugin.name,
                "version": lp.plugin.version,
                "description": lp.plugin.description,
                "status": lp.status,
                "last_error": lp.last_error,
                "capabilities": lp.plugin.capabilities,
                "api_version": lp.api_version,
                "granted": lp.granted,
                "denied": lp.denied,
                "ui_off": bool(self._grant_of(name).get("ui_off")),
                "has_config": bool(lp.config_schema),
                "routes": [{"method": r.method, "path": r.path} for r in lp.routes],
                "tasks": [t.name for t in lp.tasks],
                "frames": [{"id": f.id, "blocked": f.blocked} for f in lp.frames.values()],
                "ui": _serialize_ui(lp.ui),
            }
        for name, info in self._failed.items():
            if name in out:
                continue
            out[name] = {
                "name": name, "version": "", "description": "",
                "status": info.get("status", "error"),
                "last_error": info.get("error", ""),
                "capabilities": info.get("declared") or [],
                "pending": info.get("pending") or [],
                "api_version": 0, "granted": [], "denied": [], "ui_off": False,
                "has_config": False, "routes": [], "tasks": [], "ui": [],
            }
        return out

    # ── Contributions (API v2) ─────────────────────────────────────────────

    def _visible(self, lc: _LoadedContribution, session: dict | None) -> bool:
        if not lc.granted:
            return False
        if self._grant_of(lc.plugin).get("ui_off"):
            return False
        role = (session or {}).get("role", "")
        if lc.meta.get("admin") and role != "admin":
            return False
        return caps_allowed(lc.c.caps, session)

    def get_ui_manifest(self, session: dict | None) -> Dict[str, Any]:
        """What the browser needs to place contributions (no data yet).

        Filtered by the caller's role and capabilities, so a viewer never
        even learns about admin-only slots."""
        out: Dict[str, Any] = {"rev": self._rev, "safe": self._safe,
                               "contribs": [], "styles": [], "theme": None,
                               "layout": None, "options": None, "frames": [],
                               "toast_seq": toast_seq()}
        if self._safe:
            return out
        active = self._load_state().get("active", {})
        role = (session or {}).get("role", "")
        themes, layouts = [], []
        n_columns = 0
        for name in sorted(self._plugins):
            lp = self._plugins[name]
            for lc in lp.contribs:
                if not self._visible(lc, session):
                    continue
                slot = lc.c.slot
                if slot == "theme":
                    themes.append({"id": lc.cid, "label": lc.c.label or lc.cid})
                    if active.get("theme") == lc.cid:
                        out["theme"] = {"id": lc.cid, "tokens": lc.static["tokens"]}
                    continue
                if slot == "layout":
                    layouts.append({"id": lc.cid, "label": lc.c.label or lc.cid})
                    if active.get("layout") == lc.cid:
                        out["layout"] = dict(lc.static, id=lc.cid)
                    continue
                if slot == "style":
                    out["styles"].append({"id": lc.cid, "plugin": name,
                                          "css": lc.static["css"],
                                          "global": lc.static["global"]})
                    continue
                if slot == "containers.column":
                    n_columns += 1
                    if n_columns > _MAX_PLUGIN_COLUMNS:
                        continue
                out["contribs"].append({
                    "id": lc.cid, "plugin": name, "slot": slot,
                    "keyed": bool(lc.meta.get("keyed")),
                    "poll": lc.poll, "order": lc.c.order,
                    "label": str(lc.c.label or "")[:80],
                    "icon_svg": lc.c.icon_svg if slot == "tab" else "",
                })
        out["contribs"].sort(key=lambda c: (c["order"], c["plugin"], c["id"]))
        out["frames"] = self.frames_manifest(session)
        if role == "admin":
            out["options"] = {"themes": themes, "layouts": layouts,
                              "active": {"theme": active.get("theme", ""),
                                         "layout": active.get("layout", "")}}
        return out

    def find_contribution(self, cid: str) -> Optional[_LoadedContribution]:
        plugin, _, rest = str(cid).partition("/")
        lp = self._plugins.get(plugin)
        if not lp or not rest:
            return None
        for lc in lp.contribs:
            if lc.c.id == rest:
                return lc
        return None

    def _run_provider(self, lc: _LoadedContribution, session: dict | None) -> dict:
        """Call a provider and validate its result (raises on any problem)."""
        raw = _call_filtered(lc.c.provider, session=session)
        if lc.meta.get("keyed"):
            return {"keyed": True, "nodes": contrib.validate_keyed(raw)}
        return {"keyed": False, "node": contrib.validate_node(raw)}

    def contrib_data(self, ids: List[str], session: dict | None,
                     since: int = 0) -> Dict[str, Any]:
        """Data for the requested contributions (batched, cached, isolated).

        Every provider runs in a small thread pool with a total 3 s
        budget.  A failing provider yields ``{ok: false}`` for that
        contribution only; after 5 consecutive errors it backs off
        exponentially (up to 5 minutes) instead of being hammered."""
        result: Dict[str, Any] = {}
        futures: Dict[concurrent.futures.Future, _LoadedContribution] = {}
        now = time.time()
        user = str((session or {}).get("user", ""))
        for cid in list(dict.fromkeys(ids))[:_MAX_BATCH]:
            lc = self.find_contribution(cid)
            if lc is None or lc.meta.get("static") or not self._visible(lc, session):
                result[cid] = {"ok": False, "error": "unavailable"}
                continue
            cached = lc.cache.get(user)
            ttl = max(1.0, (lc.poll or 30) / 2.0)
            if cached and now - cached[0] < ttl:
                result[cid] = cached[1]
                continue
            if lc.errors >= 5 and now < lc.next_ok:
                result[cid] = {"ok": False, "error": "backing off after repeated errors"}
                continue
            try:
                futures[_get_pool().submit(self._run_provider, lc, session)] = lc
            except RuntimeError:
                result[cid] = {"ok": False, "error": "busy"}
        if futures:
            done, pending = concurrent.futures.wait(futures, timeout=_PROVIDER_BUDGET)
            for fut in done:
                lc = futures[fut]
                try:
                    data = fut.result()
                    res = {"ok": True, "data": data}
                    lc.errors = 0
                    lc.cache[user] = (time.time(), res)
                    if len(lc.cache) > 64:
                        lc.cache.pop(next(iter(lc.cache)))
                except contrib.ContribError as e:
                    res = self._provider_failed(lc, "invalid payload: %s" % e)
                except Exception:
                    traceback.print_exc()
                    res = self._provider_failed(lc, "provider failed — see server log")
                result[lc.cid] = res
            for fut in pending:
                lc = futures[fut]
                result[lc.cid] = self._provider_failed(lc, "provider timed out")
        return {"rev": self._rev, "data": result, "toasts": toasts_since(since),
                "toast_seq": toast_seq()}

    @staticmethod
    def _provider_failed(lc: _LoadedContribution, msg: str) -> dict:
        lc.errors += 1
        if lc.errors >= 5:
            lc.next_ok = time.time() + min(300.0, (lc.poll or 10) * (2 ** (lc.errors - 5)))
        return {"ok": False, "error": msg}

    # ── Appearance (exclusive slots) ───────────────────────────────────────

    def set_appearance(self, theme: Optional[str], layout: Optional[str]) -> Tuple[bool, str]:
        """Pick the active theme / layout contribution ("" = none)."""
        with self._state_lock:
            st = self._load_state()
            for key, val, slot in (("theme", theme, "theme"), ("layout", layout, "layout")):
                if val is None:
                    continue
                if val == "":
                    st["active"].pop(key, None)
                    continue
                lc = self.find_contribution(val)
                if lc is None or lc.c.slot != slot or not lc.granted:
                    return False, f"unknown {key}: {val}"
                st["active"][key] = val
            if not self._save_state():
                return False, "could not write plugin_state.json"
        self._bump()
        return True, "ok"

    # ── Config schema (Configure dialog) ───────────────────────────────────

    def plugin_config_get(self, name: str) -> Tuple[Any, int]:
        lp = self._plugins.get(name)
        if lp is None:
            return {"error": "Unknown plugin"}, 404
        cfg = lp.ctx.get_config()
        values: Dict[str, Any] = {}
        for f in lp.config_schema:
            v = cfg.get(f["name"], f.get("default"))
            values[f["name"]] = ({"__set": bool(cfg.get(f["name"]))}
                                 if f["secret"] else v)
        return {"schema": lp.config_schema, "values": values,
                "title": lp.plugin.name}, 200

    def plugin_config_set(self, name: str, values: Any) -> Tuple[Any, int]:
        lp = self._plugins.get(name)
        if lp is None:
            return {"error": "Unknown plugin"}, 404
        if not lp.config_schema:
            return {"error": "Plugin has no configuration schema"}, 400
        cur = dict(lp.ctx.get_config())
        new_vals, err = contrib.coerce_config_values(lp.config_schema, values, cur)
        if err:
            return {"error": err}, 400
        merged = dict(cur)
        merged.update(new_vals)
        try:
            lp.ctx._replace_config(merged)
        except OSError:
            return {"error": "could not write plugin config"}, 500
        try:
            lp.plugin.on_config_change(cur, dict(merged))
        except Exception:
            traceback.print_exc()
        events.emit("config.changed", {"plugin": name})
        self._bump()
        return {"success": True, "message": "Configuration saved"}, 200

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
        # F (review 2026-08-18): CardUIDef has no `id` — the generic
        # branch dropped its title/content entirely.  Serialize it
        # explicitly so cards render with their content.
        if type(item).__name__ == "CardUIDef":
            out.append({
                "type": "CardUIDef",
                "title": getattr(item, "title", ""),
                "content_template": getattr(item, "content_template", ""),
            })
            continue
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
                        "fields": [
                            {
                                "name": f.get("name", ""),
                                "label": f.get("label", ""),
                                "type": f.get("type", "text"),
                                "default": f.get("default"),
                                "placeholder": f.get("placeholder", ""),
                            }
                            for f in getattr(a, "fields", [])
                            if isinstance(f, dict)
                        ],
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
