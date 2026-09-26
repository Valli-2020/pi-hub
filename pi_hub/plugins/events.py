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
``scan.complete``   ``services.read`` ``{added, total}``
``config.changed``  (none)            ``{}``
==================  ================  ==========================================
"""

from __future__ import annotations

import queue
import threading
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


def _reset_for_tests() -> None:
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
