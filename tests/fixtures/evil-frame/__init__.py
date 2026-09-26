"""evil-frame — a hostile plugin frame used to prove the sandbox holds.

NEVER install this on a real hub.  Its frames try to steal the admin token,
reach the network, navigate the dashboard, flood the bridge and freeze the
tab; the browser test (tests/browser_evil_frame.mjs) asserts that every
attempt fails.  Results are reported through the plugin's own route.
"""

from __future__ import annotations

import threading

from pi_hub.plugins.base import FrameDef, Plugin, RouteDef


class EvilFrame(Plugin):
    name = "evil-frame"
    version = "1.0.0"
    description = "Hostile frame fixture — do not install"
    plugin_api_version = 2
    capabilities = ["ui.frame", "ui.slots", "ui.header", "hosts.read"]

    def load(self, ctx) -> None:
        self._lock = threading.Lock()
        self._results: dict = {}
        self._hits = 0
        self._target = "http://127.0.0.1:9"      # a listener the test starts; must never be contacted

    def get_routes(self):
        return [
            RouteDef("POST", "/report", self.report),
            RouteDef("POST", "/target", self.set_target),
            RouteDef("GET", "/target", lambda session, body: {"url": self._target}),
            RouteDef("GET", "/results", lambda session, body: self._snapshot()),
            RouteDef("GET", "/samples", self.samples),
        ]

    def report(self, session, body):
        with self._lock:
            self._results[str(body.get("frame", "?"))[:32]] = body.get("results", {})
        return {"ok": True}

    def set_target(self, session, body):
        self._target = str(body.get("url", ""))[:200]
        return {"ok": True}

    def samples(self, session, body):
        with self._lock:
            self._hits += 1
            return {"hits": self._hits}

    def _snapshot(self):
        with self._lock:
            return {"results": dict(self._results), "hits": self._hits}

    def get_frames(self):
        common = dict(renders=["hosts.card.badges", "header.pill"], reads=["hosts.status"])
        return [
            FrameDef("attack", entry=["attack.js"], surfaces=[{"type": "tab", "label": "Attack"}], **common),
            FrameDef("flood", entry=["flood.js"], surfaces=[{"type": "tab", "label": "Flood"}], **common),
            FrameDef("spin", entry=["spin.js"], surfaces=[{"type": "tab", "label": "Spin"}], **common),
        ]
