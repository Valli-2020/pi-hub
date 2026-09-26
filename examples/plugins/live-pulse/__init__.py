"""Live pulse — a sandboxed-frame demo for Pi Hub (Plugin API v2).

The Python side only collects data (TCP-connect latency per host, host state
changes).  Everything visual — canvas sparklines, the event log — is
JavaScript in ``static/frame/pulse.js``, which runs in an opaque-origin
iframe and reaches this plugin only through ``ph.call('samples')`` /
``ph.call('events')``.  It also feeds a latency badge into every host card
with ``ph.render``.
"""

from __future__ import annotations

import socket
import threading
import time
from collections import deque
from typing import Any

from pi_hub.plugins.base import FrameDef, Plugin, RouteDef, TaskDef

SAMPLES = 60          # points kept per host (5 s apart → 5 minutes)


class LivePulse(Plugin):
    name = "live-pulse"
    version = "1.0.0"
    description = "Latency sparklines and an event log in sandboxed frames"
    min_core_version = "8.0.0"
    plugin_api_version = 2
    capabilities = ["hosts.read", "ui.frame", "ui.slots"]
    events = ["host.state"]

    def load(self, ctx) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._samples: dict[str, deque] = {}
        self._events: deque = deque(maxlen=100)

    def get_tasks(self):
        return [TaskDef("sample", self._sample, interval=5)]

    def _sample(self) -> None:
        for h in self.ctx.get_hosts():
            t0 = time.perf_counter()
            try:
                with socket.create_connection((h["ip"], 22), timeout=0.8):
                    ms: Any = round((time.perf_counter() - t0) * 1000, 1)
            except OSError:
                ms = None
            with self._lock:
                self._samples.setdefault(h["id"], deque(maxlen=SAMPLES)).append(ms)

    def on_event(self, name: str, payload: dict) -> None:
        if name == "host.state":
            with self._lock:
                self._events.append({"ts": int(time.time()), "host": str(payload.get("host_id", "")),
                                     "text": "%s → %s" % (payload.get("old"), payload.get("new"))})

    def get_routes(self):
        return [RouteDef("GET", "/samples", self.samples),
                RouteDef("GET", "/events", self.events_log)]

    def samples(self, session, body):
        names = {h["id"]: h.get("name", h["id"]) for h in self.ctx.get_hosts()}
        with self._lock:
            return {"hosts": [{"id": hid, "name": names.get(hid, hid), "points": list(pts)}
                              for hid, pts in self._samples.items()]}

    def events_log(self, session, body):
        with self._lock:
            return {"events": list(self._events)[-50:]}

    def get_frames(self):
        return [FrameDef(
            "pulse",
            entry=["pulse.js"], css=["pulse.css"],
            surfaces=[{"type": "widget", "view": "hosts"}, {"type": "tab", "label": "Pulse"}],
            renders=["hosts.card.badges"],
            reads=["hosts.status"],
            height=150,
        )]
