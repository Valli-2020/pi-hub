"""Plugin event bus (API v2).

Core code calls :func:`emit` at the moment something changes; a single
dispatcher thread delivers the event to every subscribed plugin, so a slow
or crashing plugin can never block the request thread that produced the
event.  Exceptions are isolated per plugin.

Events and the capability a plugin needs to subscribe:

==================  ================  ==========================================
event               capability        payload
==================  ================  ==========================================
``host.state``      ``hosts.read``    ``{host_id, old, new}``
``container.state`` ``proxmox.read``  ``{instance, vmid, name, old, new}``
``scan.complete``   ``services.read`` ``{candidates}``
``config.changed``  (none)            ``{}``
==================  ================  ==========================================
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Tuple

EVENT_CAPS: Dict[str, str | None] = {
    "host.state": "hosts.read",
    "container.state": "proxmox.read",
    "scan.complete": "services.read",
    "config.changed": None,
}

_QUEUE_MAX = 1000

_lock = threading.Lock()
_subs: Dict[str, List[Tuple[str, Callable[[str, dict], None]]]] = {}
_queue: "queue.Queue[Tuple[str, dict]]" = queue.Queue(maxsize=_QUEUE_MAX)
_thread: threading.Thread | None = None


def subscribe(plugin: str, events: List[str], fn: Callable[[str, dict], None],
              caps: set[str]) -> List[str]:
    """Subscribe *fn* to *events*; returns the events actually granted
    (an event whose capability the plugin lacks is skipped)."""
    granted: List[str] = []
    with _lock:
        for ev in events:
            if ev not in EVENT_CAPS:
                continue
            need = EVENT_CAPS[ev]
            if need is not None and need not in caps:
                continue
            _subs.setdefault(ev, []).append((plugin, fn))
            granted.append(ev)
    if granted:
        _ensure_thread()
        if "host.state" in granted or "container.state" in granted:
            _ensure_watcher()
    return granted


def unsubscribe(plugin: str) -> None:
    with _lock:
        for ev in list(_subs):
            _subs[ev] = [(p, f) for (p, f) in _subs[ev] if p != plugin]
            if not _subs[ev]:
                del _subs[ev]


def has_subscribers(*events: str) -> bool:
    """True when at least one plugin listens to any of *events* (all events
    when none are named).  Lets the core skip expensive probing."""
    with _lock:
        if not events:
            return bool(_subs)
        return any(_subs.get(e) for e in events)


def emit(event: str, payload: dict | None = None) -> None:
    """Queue *event* for delivery.  Never blocks and never raises; when the
    queue is full the event is dropped."""
    if event not in EVENT_CAPS:
        return
    with _lock:
        if not _subs.get(event):
            return
    try:
        _queue.put_nowait((event, dict(payload or {})))
    except queue.Full:
        pass


def _ensure_thread() -> None:
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_dispatch_loop, name="plugin-events",
                                   daemon=True)
        _thread.start()


def _dispatch_loop() -> None:
    while True:
        event, payload = _queue.get()
        with _lock:
            targets = list(_subs.get(event, ()))
        for plugin, fn in targets:
            try:
                fn(event, dict(payload))
            except Exception:
                print("PluginEvents: %s failed on %s" % (plugin, event))
                traceback.print_exc()


# ── Change detection ─────────────────────────────────────────────────────
# The core feeds every fresh probe result through these two helpers, so
# events fire no matter which request (or the watcher) triggered the probe.
# The first observation only seeds the baseline — no event for it.

_state_lock = threading.Lock()
_host_last: Dict[str, str] = {}
_ct_last: Dict[Tuple[str, str], str] = {}


def note_hosts(status: Dict[str, Dict[str, Any]]) -> None:
    """Diff ``hosts.get_all_status()`` output; emit ``host.state``."""
    if not has_subscribers("host.state"):
        return
    changes = []
    with _state_lock:
        for hid, rec in status.items():
            new = "up" if rec.get("online") else "down"
            old = _host_last.get(hid)
            _host_last[hid] = new
            if old is not None and old != new:
                changes.append({"host_id": hid, "old": old, "new": new})
    for c in changes:
        emit("host.state", c)


def note_containers(instance: str, containers: List[dict]) -> None:
    """Diff one Proxmox instance's container list; emit ``container.state``."""
    if not has_subscribers("container.state"):
        return
    changes = []
    with _state_lock:
        for ct in containers:
            key = (instance, str(ct.get("vmid", "")))
            new = str(ct.get("status", "unknown"))
            old = _ct_last.get(key)
            _ct_last[key] = new
            if old is not None and old != new:
                changes.append({"instance": instance, "vmid": key[1],
                                "name": str(ct.get("name", "")), "old": old, "new": new})
    for c in changes:
        emit("container.state", c)


# ── Watcher ──────────────────────────────────────────────────────────────
# Probes happen on demand while somebody has the dashboard open.  A plugin
# that reacts to state changes needs them while nobody does, so a watcher
# probes hosts every 30 s and containers every 60 s — but only while at
# least one plugin subscribes to the matching event.

HOST_INTERVAL = 30.0
CONTAINER_INTERVAL = 60.0
_watcher: threading.Thread | None = None


def _ensure_watcher() -> None:
    global _watcher
    with _lock:
        if _watcher is not None and _watcher.is_alive():
            return
        _watcher = threading.Thread(target=_watch_loop, name="plugin-watcher", daemon=True)
        _watcher.start()


def _watch_loop() -> None:
    next_host = next_ct = 0.0
    while True:
        time.sleep(1.0)
        now = time.monotonic()
        try:
            if now >= next_host and has_subscribers("host.state"):
                next_host = now + HOST_INTERVAL
                from .. import hosts
                hosts.get_all_status()            # feeds note_hosts()
            if now >= next_ct and has_subscribers("container.state"):
                next_ct = now + CONTAINER_INTERVAL
                from .. import proxmox
                proxmox.fetch_proxmox_containers("")   # feeds note_containers()
        except Exception:
            traceback.print_exc()


def _reset_for_tests() -> None:
    with _state_lock:
        _host_last.clear()
        _ct_last.clear()
    with _lock:
        _subs.clear()
    while True:
        try:
            _queue.get_nowait()
        except queue.Empty:
            break


def drain(timeout: float = 2.0) -> bool:
    """Wait until the queue is empty (used by tests).  Returns True on success."""
    import time
    end = time.time() + timeout
    while time.time() < end:
        if _queue.empty():
            time.sleep(0.05)              # let the in-flight callback finish
            return True
        time.sleep(0.01)
    return False
