"""Plugin base types — ABC, PluginContext, and declarative descriptors.

This module defines the contract every Pi Hub plugin implements and the
through-which-all-system-access-happens context that enforces the plugin
capability boundary.

Security
--------
PluginContext is the ONLY way plugins touch the rest of the system.  It
never hands back raw module references (no ``pi_hub.config`` import), no
file paths outside the plugin's own directory, and no tokens.  Every
action is checked against the plugin's declared capabilities at call
time (not just at load time), mirroring the per-request ``can_act()``
guard in :mod:`pi_hub.auth`.
"""

from __future__ import annotations

import abc
import json
import os
import re
import threading
import time
from copy import deepcopy as _deepcopy
from typing import Any, Callable, Dict, List, Optional

# ═══════════════════════════════════════════════════════════════════════════════
# Declarative descriptors (for get_routes / get_tasks / get_ui)
# ═══════════════════════════════════════════════════════════════════════════════


class RouteDef:
    """One HTTP route exposed by a plugin.

    ``caps`` is the list of user capabilities (from
    :data:`pi_hub.auth.CAP_ACTIONS` plus plugin-specific ones) required to
    call the route.  ``[]`` means any authenticated user; ``["admin"]``
    means admin-only (same semantics as :func:`pi_hub.auth.classify`).
    """

    __slots__ = ("method", "path", "handler", "caps")

    def __init__(
        self,
        method: str,
        path: str,
        handler: Callable[..., Any],
        caps: list[str] | None = None,
    ):
        self.method = method.upper()
        self.path = path
        self.handler = handler
        self.caps = caps or []


class TaskDef:
    """One background task exposed by a plugin.

    The task function is launched in a daemon thread via
    :meth:`PluginContext.run_task`.  Task names are namespaced
    ``plugin:<plugin_name>:<name>`` automatically — plugins never
    collide with core tasks (``boot_windows`` etc.).
    """

    __slots__ = ("name", "fn")

    def __init__(self, name: str, fn: Callable[[], None]):
        self.name = name
        self.fn = fn


class TabUIDef:
    """A sidebar tab contributed by a plugin.

    The frontend's generic plugin renderer calls ``poll_endpoint`` every
    10 s and renders the JSON it returns into the tab's content area
    (the same merged-poll pattern the existing UI uses).
    """

    __slots__ = (
        "id",
        "label",
        "icon_svg",
        "position",
        "poll_endpoint",
        "actions",
    )

    def __init__(
        self,
        id: str,
        label: str,
        icon_svg: str,
        position: int = 99,
        poll_endpoint: str = "",
        actions: list["ActionDef"] | None = None,
    ):
        self.id = id
        self.label = label
        self.icon_svg = icon_svg
        self.position = position
        self.poll_endpoint = poll_endpoint
        self.actions = actions or []


class CardUIDef:
    """A card contributed to the Settings page (admin-only surface)."""

    __slots__ = ("title", "content_template")

    def __init__(self, title: str, content_template: str = ""):
        self.title = title
        self.content_template = content_template


class ActionDef:
    """A button on a plugin tab.

    ``caps`` works like :class:`RouteDef` — admin-only if ``["admin"]``,
    any-authed if ``[]``.

    ``fields`` (optional) turns the button into a form dialog: the core
    frontend renders one input per field (allowlisted types only) and
    POSTs the values as a JSON body to ``/api/plugin/<name>/<id>``
    instead of a bodyless POST.  Field dicts:
    ``{"name", "label", "type", "default", "placeholder"}`` with
    ``type`` in ``text|password|number|checkbox``.
    """

    __slots__ = ("id", "label", "style", "caps", "fields")

    def __init__(
        self,
        id: str,
        label: str,
        style: str = "secondary",
        caps: list[str] | None = None,
        fields: list[dict] | None = None,
    ):
        self.id = id
        self.label = label
        self.style = style
        self.caps = caps or []
        self.fields = fields or []


# ═══════════════════════════════════════════════════════════════════════════════
# Plugin ABC
# ═══════════════════════════════════════════════════════════════════════════════


class Plugin(abc.ABC):
    """Every plugin subclasses this.  Declarative — return routes/tasks/UI
    from the respective ``get_*`` methods; the framework wires them.
    """

    #: Unique id (must match the directory name under ``pi_hub_plugins/``).
    name: str = ""
    #: Semver string, e.g. ``"0.1.0"``.
    version: str = "0.1.0"
    #: Short human-readable description.
    description: str = ""
    #: Minimum Pi Hub core version required (semver, e.g. ``"7.1.0"``).
    min_core_version: str = "7.1.0"
    #: Capability needs — list of strings, e.g. ``["proxmox.read", "ssh.execute"]``.
    capabilities: list[str] = []

    def load(self, ctx: "PluginContext") -> None:
        """Called once when the plugin is loaded.  Set up state here."""
        raise NotImplementedError

    def unload(self) -> None:
        """Called when the plugin is disabled.  Cancel background work."""
        pass

    def get_routes(self) -> list[RouteDef]:
        """Return the plugin's HTTP routes."""
        return []

    def get_tasks(self) -> list[TaskDef]:
        """Return the plugin's background tasks."""
        return []

    def get_ui(self) -> list[Any]:
        """Return TabUIDef / CardUIDef / ActionDef descriptors."""
        return []

    # ── Optional event hooks ────────────────────────────────────────────────

    def on_host_state_change(self, host_id: str, new_state: str) -> None:
        """Reserved — NOT called by the core (see PLUGINS.md §6)."""
        pass

    def on_scan_complete(self, results: dict) -> None:
        """Reserved — NOT called by the core (see PLUGINS.md §6)."""
        pass

    def migrate_config(self, old_version: str, config: dict) -> dict:
        """Reserved — NOT called by the core (see PLUGINS.md §6)."""
        return config


# ═══════════════════════════════════════════════════════════════════════════════
# PluginContext — the security boundary
# ═══════════════════════════════════════════════════════════════════════════════


class PluginLoadError(Exception):
    """Raised when a plugin fails to load (capability missing, invalid
    manifest, init error)."""


class _PluginThread(threading.Thread):
    """Thread that can be cancelled via its ``_cancel`` event."""

    def __init__(self, target: Callable[[], None], **kw):
        self._cancel = threading.Event()
        # wrap target so it can check _cancel periodically
        fn = target

        def _wrap():
            # pass cancel handle via thread-local so plugin code can check it
            _PLUGIN_THREAD_CANCEL.cancel = self._cancel
            try:
                fn()
            finally:
                _PLUGIN_THREAD_CANCEL.cancel = None

        super().__init__(target=_wrap, daemon=True, **kw)


_PLUGIN_THREAD_CANCEL = threading.local()


def thread_cancel() -> threading.Event | None:
    """Return the cancel event for the current plugin thread, or None."""
    return getattr(_PLUGIN_THREAD_CANCEL, "cancel", None)


class PluginContext:
    """The ONLY way a plugin touches the Pi Hub system.  Every method
    checks capabilities at call time (not just load time).

    Instances are constructed by :class:`PluginManager` and are NOT
    shareable across plugins — each plugin gets its own.
    """

    def __init__(
        self,
        plugin: Plugin,
        plugin_dir: str,
        capabilities: list[str],
    ):
        self._plugin = plugin
        self._plugin_dir = os.path.realpath(plugin_dir)
        self._caps = set(capabilities)
        self._config: dict = {}
        self._threads: list[_PluginThread] = []
        self._unloaded = False              # D6: set by unload(); see _check_alive
        self._config_path = os.path.join(plugin_dir, "config.json")
        self._load_config()

    # ── Internal helpers ───────────────────────────────────────────────────

    def _load_config(self) -> None:
        try:
            with open(self._config_path) as f:
                self._config = json.load(f)
        except (OSError, ValueError):
            self._config = {}

    def _save_config(self) -> None:
        from pi_hub.config import _config_edit_lock  # local: avoid cycle
        with _config_edit_lock:
            tmp = self._config_path + ".tmp"
            # Atomic 0600 write — plugin config routinely holds API keys, so
            # it gets the same file mode as the core's secrets, not whatever
            # the process umask happens to be.
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(self._config, indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._config_path)

    def _require_cap(self, cap: str) -> None:
        self._check_alive()
        if cap not in self._caps:
            raise PermissionError(
                f"Plugin '{self._plugin.name}' lacks capability '{cap}'"
            )

    def _check_alive(self) -> None:
        """D6 (review 2026-08-18): after unload() the context is DEAD.
        A daemon thread that ignored ``thread_cancel()`` must not keep
        driving the system through a fully functional ctx — every public
        ctx method funnels through here and raises once the plugin was
        unloaded."""
        if self._unloaded:
            raise RuntimeError(
                f"Plugin '{self._plugin.name}' was unloaded — its context "
                "is no longer usable")

    # ── Config (per-plugin, namespaced) ────────────────────────────────────

    def get_config(self) -> dict:
        """Return this plugin's config dict (mutable reference — save with
        :meth:`save_config` after editing)."""
        self._check_alive()
        return self._config

    def save_config(self) -> None:
        """Atomically persist the plugin's config to ``config.json``."""
        self._check_alive()
        self._save_config()

    # ── System reads (capability-gated) ────────────────────────────────────

    def get_hosts(self) -> list[dict]:
        """Return all configured hosts (needs ``hosts.read``).

        D4 (review 2026-08-18): returns a deep copy — the raw module
        cache list was handed out, so a plugin write mutated the live
        config (see get_services).
        """
        self._require_cap("hosts.read")
        from pi_hub.config import get_hosts
        return _deepcopy(get_hosts())

    def get_proxmox_containers(self, instance_id: str = "") -> dict:
        """Return Proxmox container list (needs ``proxmox.read``)."""
        self._require_cap("proxmox.read")
        from pi_hub import proxmox
        return proxmox.fetch_proxmox_containers(instance_id)

    def get_proxmox_instances(self) -> list[dict]:
        """Return the configured Proxmox instances (needs ``proxmox.read``).

        Each entry: ``{id, host, node, ssh_user, enabled}``.  No tokens,
        no api_url — just what a plugin needs to address the hosts.
        """
        self._require_cap("proxmox.read")
        from pi_hub.config import proxmox_instances
        return [
            {
                "id": str(p.get("id", "")),
                "host": str(p.get("host", "")),
                "node": str(p.get("node", "")),
                "ssh_user": str(p.get("ssh_user", "") or "root"),
                "enabled": bool(p.get("enabled", bool(p.get("host")))),
            }
            for p in proxmox_instances()
            if p.get("host")
        ]

    def ssh_cmd(self, host_id: str, command: str, timeout: int = 1800) -> dict:
        """Run a command via SSH on a *registry host* (needs ``ssh.execute``).

        Only ``pct exec <vmid> -- <container command>`` is allowed (the
        capability boundary for plugins that manage containers — e.g. an
        update-all plugin).  Raw shell access on the Proxmox host is never
        granted.

        The command is parsed with :mod:`shlex` and rebuilt with
        ``shlex.join``: a prefix check alone is insufficient, because the
        SSH transport hands the string to the *remote shell*, so an
        unquoted ``;`` / ``&&`` / ``|`` after the prefix would escape the
        allowlist and run on the Proxmox host as root.

        ``timeout`` is the SSH timeout in seconds.  The default is long
        (30 min) because ``pct exec`` commands such as ``apt-get upgrade``
        routinely run far past the core's short default — a short timeout
        would kill the remote command mid-transaction and leave the
        container in an inconsistent state.

        Returns ``{"ok": bool, "output": str, "code": int}``.  On a
        non-zero remote exit, ``output`` contains the captured stderr/stdout
        so callers can surface the real failure reason (rather than a
        generic "ssh failed").
        """
        self._require_cap("ssh.execute")
        from pi_hub.config import find_host_by_id
        from pi_hub.hosts import ssh_result
        host = find_host_by_id(host_id)
        if not host:
            return {"ok": False, "output": "unknown host", "code": 1}

        import shlex
        cmd = str(command or "").strip()
        try:
            parts = shlex.split(cmd)
        except ValueError:
            return {"ok": False, "output": "command not allowed (unbalanced quotes)",
                    "code": 1}
        # Structure: pct exec <vmid> -- <container command>
        if len(parts) < 4 or parts[0] != "pct" or parts[1] != "exec":
            return {"ok": False,
                    "output": "command not allowed (only 'pct exec <vmid> -- ...')",
                    "code": 1}
        if not parts[2].isdigit() or not (100 <= int(parts[2]) <= 999999):
            return {"ok": False, "output": "command not allowed (invalid vmid)",
                    "code": 1}
        if parts[3] != "--":
            return {"ok": False, "output": "command not allowed (missing '--')",
                    "code": 1}
        # Rebuild with shlex.join: tokens containing shell metacharacters
        # (;, &&, |, $, backticks, redirects) get re-quoted, so the remote
        # shell cannot interpret them outside the container command.
        safe_cmd = shlex.join(parts)
        res = ssh_result(host, safe_cmd, timeout=timeout)
        if res["returncode"] is None:
            return {"ok": False, "output": res["stderr"] or "ssh failed",
                    "code": 1}
        output = res["stdout"] or res["stderr"]
        return {"ok": res["ok"], "output": output, "code": res["returncode"]}

    def get_services(self) -> list[dict]:
        """Return configured services (needs ``services.read``).

        D4 (review 2026-08-18): returns a deep copy — the raw module
        cache list was handed out, so a plugin write mutated the live
        config until the next mtime-triggered reload (never, on a stable
        machine).
        """
        self._require_cap("services.read")
        from pi_hub.config import get_config
        return _deepcopy(get_config().get("services", []))

    def get_dockge_stacks(self) -> list[dict]:
        """Return Dockge stacks (needs ``dockge.read``)."""
        self._require_cap("dockge.read")
        from pi_hub import dockge
        return dockge.list_stacks()

    # ── System actions (capability-gated) ──────────────────────────────────

    def ssh_action(self, host_id: str, action: str) -> bool:
        """Run a power action via SSH (needs ``ssh.execute``).  ``action``
        is validated against the same allowlist the core UI uses — no raw
        command injection.

        D3 (review 2026-08-18): ``hosts.ssh_action`` takes the host ID and
        resolves it itself — passing the host DICT made the internal
        ``find_host_by_id`` lookup always fail, so every call returned a
        truthy ``{"success": False, ...}`` while nothing was ever sent.
        The result is now a real bool derived from ``success``.
        """
        self._require_cap("ssh.execute")
        from pi_hub.hosts import ssh_action as core_ssh_action
        res = core_ssh_action(str(host_id or ""), str(action or ""))
        return bool(res.get("success")) if isinstance(res, dict) else False

    def wake_host(self, host_id: str) -> bool:
        """Send a WOL magic packet (needs ``hosts.wake``)."""
        self._require_cap("hosts.wake")
        from pi_hub.config import find_host_by_id
        from pi_hub.net import send_wol
        host = find_host_by_id(host_id)
        if not host:
            return False
        return bool(send_wol(host.get("mac", ""), host.get("ip", "")))

    def proxmox_action(self, instance_id: str, vmid: str, action: str) -> dict:
        """Start/stop/reboot/shutdown a container (needs ``proxmox.control``)."""
        self._require_cap("proxmox.control")
        from pi_hub import proxmox
        return proxmox.pve_action(vmid, action, instance_id)

    # ── Background tasks ───────────────────────────────────────────────────

    def run_task(self, name: str, fn: Callable[[], None]) -> None:
        """Launch a background task in a daemon thread.  The thread is
        tracked so :meth:`Plugin.unload` can cancel it.  Task name is
        namespaced to this plugin."""
        self._check_alive()
        full_name = f"plugin:{self._plugin.name}:{name}"
        t = _PluginThread(target=fn, name=full_name)
        self._threads.append(t)
        t.start()

    def set_task_status(self, name: str, status: str, message: str = "") -> None:
        """Update the status of a running task (polled via
        ``GET /api/task/plugin:<name>:<task>``)."""
        self._check_alive()
        from pi_hub import tasks
        full_name = f"plugin:{self._plugin.name}:{name}"
        tasks.set_task_status(full_name, status, message)

    def get_task_status(self, name: str) -> Optional[dict]:
        """Return the last status of a task."""
        self._check_alive()
        from pi_hub import tasks
        full_name = f"plugin:{self._plugin.name}:{name}"
        return tasks.get_task_status(full_name)

    # ── UI / notifications ────────────────────────────────────────────────

    def toast(self, message: str, kind: str = "info") -> None:
        """Push a toast to all connected UIs (stored in the global
        ``_PLUGINS_TOASTS`` buffer polled by the frontend)."""
        self._check_alive()
        from pi_hub.plugins.manager import push_toast
        push_toast(self._plugin.name, message, kind)

    def register_static(self, url_path: str, file_path: str) -> None:
        """Register a static file served at ``/plugin-static/<name>/...``.

        ``file_path`` must live inside the plugin's ``static/`` directory
        — path traversal is rejected."""
        self._check_alive()
        from pi_hub.plugins.manager import register_plugin_static
        # Defer resolution to serve time so plugin_dir is known
        register_plugin_static(self._plugin.name, url_path, file_path, self._plugin_dir)

    def request_restart(self, tag: str) -> None:
        """Signal the core server to restart (e.g. after applying an
        update).  The actual restart is handled by the core's
        ``server.py`` which calls ``os._exit(0)`` — plugins can't do that
        directly without killing other plugins."""
        self._check_alive()
        from pi_hub.plugins.manager import request_core_restart
        request_core_restart(tag)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def unload(self) -> None:
        """Signal all plugin threads to stop and wait briefly.

        D6 (review 2026-08-18): the context is marked DEAD FIRST so any
        thread that ignores ``thread_cancel()`` and keeps running fails
        on its next ctx call instead of acting with full capabilities
        after uninstall/disable.
        """
        self._unloaded = True
        for t in self._threads:
            if hasattr(t, "_cancel"):
                t._cancel.set()
        for t in self._threads:
            t.join(timeout=3.0)
        self._threads.clear()
        self._plugin.unload()
