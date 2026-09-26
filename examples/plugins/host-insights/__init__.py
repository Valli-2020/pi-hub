"""host-insights — Plugin API v2 demo (reads only).

Shows how a plugin edits the standard tabs through contribution slots:
badges on host cards, a column in the Containers table, a header pill, a
banner, a Settings card, a tab and scoped CSS.  All payloads are plain
JSON nodes; the core validates and renders them (no HTML, no JS).
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Any

from pi_hub.plugins.base import (
    Contribution,
    Plugin,
    RouteDef,
    TaskDef,
)

_HOUR = 3600.0


class HostInsights(Plugin):
    name = "host-insights"
    version = "1.0.0"
    description = "Latency / flapping badges, CPU column, header pill and overview tab"
    min_core_version = "8.0.0"
    plugin_api_version = 2
    capabilities = ["hosts.read", "proxmox.read", "ui.slots", "ui.header",
                    "ui.settings", "ui.tab", "ui.style"]
    events = ["host.state"]

    # ── lifecycle ────────────────────────────────────────────────────────

    def load(self, ctx) -> None:
        self.ctx = ctx
        self._lock = threading.Lock()
        self._lat: dict[str, dict[str, Any]] = {}     # host id -> {ms, up, ts}
        self._flips: dict[str, list[float]] = {}      # host id -> flip timestamps
        self._cpu: dict[str, list[tuple[float, float]]] = {}  # "inst:vmid" -> [(ts, cpu)]
        cfg = ctx.get_config()
        for k, v in (("probe_port", 22), ("timeout", 1.0), ("warn_ms", 80), ("bad_ms", 250)):
            cfg.setdefault(k, v)

    def get_config_schema(self):
        return [
            {"name": "probe_port", "label": "Probe port", "type": "number", "min": 1, "max": 65535,
             "help": "TCP port used for the latency probe (default 22)."},
            {"name": "timeout", "label": "Timeout (s)", "type": "number", "min": 0.2, "max": 10},
            {"name": "warn_ms", "label": "Warn above (ms)", "type": "number", "min": 1, "max": 10000},
            {"name": "bad_ms", "label": "Bad above (ms)", "type": "number", "min": 1, "max": 10000},
        ]

    # ── data collection (background task) ────────────────────────────────

    def _probe(self, host: dict) -> dict:
        cfg = self.ctx.get_config()
        t0 = time.perf_counter()
        try:
            with socket.create_connection((host["ip"], int(cfg["probe_port"])),
                                          timeout=float(cfg["timeout"])):
                pass
            return {"up": True, "ms": round((time.perf_counter() - t0) * 1000, 1), "ts": time.time()}
        except OSError:
            return {"up": False, "ms": None, "ts": time.time()}

    def probe_all(self) -> None:
        for h in self.ctx.get_hosts():
            res = self._probe(h)
            with self._lock:
                old = self._lat.get(h["id"])
                self._lat[h["id"]] = res
                if old is not None and old["up"] != res["up"]:
                    self._flips.setdefault(h["id"], []).append(res["ts"])
        self._sample_containers()

    def _sample_containers(self) -> None:
        try:
            data = self.ctx.get_proxmox_containers()
        except Exception:
            return
        now = time.time()
        with self._lock:
            for c in (data or {}).get("containers", []) or []:
                if c.get("status") != "running" or c.get("cpu") is None:
                    continue
                key = "%s:%s" % (c.get("instance", ""), c.get("vmid"))
                hist = self._cpu.setdefault(key, [])
                hist.append((now, float(c["cpu"])))
                del hist[:-40]

    def get_tasks(self):
        return [TaskDef("probe", self.probe_all, interval=20)]

    def on_event(self, name, payload) -> None:
        if name == "host.state":
            with self._lock:
                self._flips.setdefault(str(payload.get("host_id", "")), []).append(time.time())

    # ── helpers ──────────────────────────────────────────────────────────

    def _flaps(self, host_id: str) -> int:
        cut = time.time() - _HOUR
        with self._lock:
            fl = [t for t in self._flips.get(host_id, []) if t >= cut]
            self._flips[host_id] = fl
            return len(fl)

    def _tone(self, ms: float | None) -> str:
        cfg = self.ctx.get_config()
        if ms is None:
            return "bad"
        return "bad" if ms >= float(cfg["bad_ms"]) else "warn" if ms >= float(cfg["warn_ms"]) else "ok"

    # ── contributions ────────────────────────────────────────────────────

    def get_contributions(self):
        return [
            Contribution("hosts.card.badges", "latency", self.p_latency, poll=15, order=10),
            Contribution("hosts.card.badges", "flap", self.p_flap, poll=30, order=20),
            Contribution("containers.column", "cpu5", self.p_cpu, poll=30, label="CPU 5m avg"),
            Contribution("header.pill", "flaps", self.p_pill, poll=30),
            Contribution("hosts.top", "banner", self.p_banner, poll=15),
            Contribution("settings.card", "about", self.p_about, poll=60, label="Host insights"),
            Contribution("tab", "overview", self.p_tab, poll=15, label="Insights",
                         icon_svg='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
                                  'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
                                  '<path d="M3 12h4l3-8 4 16 3-8h4"/></svg>'),
            Contribution("style", "css", static={"css": ".ph-badge{letter-spacing:0}"}),
        ]

    def p_latency(self, session=None):
        out = {}
        with self._lock:
            snap = dict(self._lat)
        for hid, r in snap.items():
            out[hid] = {"type": "badge", "text": "%s ms" % r["ms"] if r["up"] else "down",
                        "tone": self._tone(r["ms"]), "title": "TCP connect to the probe port"}
        return out

    def p_flap(self, session=None):
        out = {}
        for h in self.ctx.get_hosts():
            n = self._flaps(h["id"])
            if n >= 3:
                out[h["id"]] = {"type": "badge", "text": "flapping ×%d" % n, "tone": "warn",
                                "title": "state changes in the last hour"}
        return out

    def p_cpu(self, session=None):
        out = {}
        cut = time.time() - 300
        with self._lock:
            hist = {k: [c for (t, c) in v if t >= cut] for k, v in self._cpu.items()}
        for key, vals in hist.items():
            if vals:
                pct = sum(vals) / len(vals) * 100
                out[key] = {"type": "gauge", "pct": pct, "text": "%.1f%%" % pct}
        return out

    def p_pill(self, session=None):
        n = sum(self._flaps(h["id"]) for h in self.ctx.get_hosts())
        return {"type": "badge", "text": "%d flaps/h" % n if n else "stable", "tone": "warn" if n else "ok",
                "title": "host state changes in the last hour"}

    def _summary(self):
        with self._lock:
            snap = list(self._lat.values())
        ups = [r for r in snap if r["up"]]
        avg = sum(r["ms"] for r in ups) / len(ups) if ups else None
        return len(ups), len(snap), avg

    def p_banner(self, session=None):
        up, total, avg = self._summary()
        if not total:
            return {"type": "text", "text": "host-insights: probing…", "tone": "muted"}
        return {"type": "stack", "dir": "row", "children": [
            {"type": "stat", "label": "reachable", "value": "%d/%d" % (up, total),
             "tone": "ok" if up == total else "warn"},
            {"type": "stat", "label": "avg latency", "value": "—" if avg is None else "%.0f" % avg, "unit": "ms"},
        ]}

    def p_about(self, session=None):
        c = self.ctx.get_config()
        return {"type": "kv", "rows": [["probe port", c["probe_port"]], ["warn / bad", "%s / %s ms" % (c["warn_ms"], c["bad_ms"])],
                                        ["reads only", True]]}

    def p_tab(self, session=None):
        hosts = self.ctx.get_hosts()
        with self._lock:
            snap = dict(self._lat)
        rows = []
        for h in hosts:
            r = snap.get(h["id"])
            rows.append({
                "_id": h["id"], "name": h["name"], "ip": h["ip"],
                "latency": ({"type": "badge", "text": "%s ms" % r["ms"] if r["up"] else "down", "tone": self._tone(r["ms"])}
                            if r else "…"),
                "flaps": self._flaps(h["id"]),
            })
        return {"type": "stack", "children": [
            {"type": "stack", "dir": "row", "children": [
                {"type": "button", "label": "Probe all now", "action": "probe", "style": "primary"},
                {"type": "button", "label": "Thresholds…", "action": "thresholds", "caps": ["admin"], "fields": [
                    {"name": "warn_ms", "label": "Warn above (ms)", "type": "number", "default": self.ctx.get_config()["warn_ms"]},
                    {"name": "bad_ms", "label": "Bad above (ms)", "type": "number", "default": self.ctx.get_config()["bad_ms"]},
                ]},
            ]},
            {"type": "table",
             "columns": [{"key": "name", "label": "Host"}, {"key": "ip", "label": "IP", "mono": True},
                         {"key": "latency", "label": "Latency"}, {"key": "flaps", "label": "Flaps/h", "align": "right"}],
             "rows": rows,
             "row_actions": [{"label": "Probe", "action": "probe"}]},
        ]}

    # ── routes ───────────────────────────────────────────────────────────

    def get_routes(self):
        return [RouteDef("POST", "/probe", self.r_probe),
                RouteDef("POST", "/thresholds", self.r_thresholds, caps=["admin"])]

    def r_probe(self, session, body):
        only = body.get("row") or body.get("key") or ""
        hosts = [h for h in self.ctx.get_hosts() if not only or h["id"] == only]
        for h in hosts:
            res = self._probe(h)
            with self._lock:
                self._lat[h["id"]] = res
        return {"message": "Probed %d host(s)" % len(hosts)}

    def r_thresholds(self, session, body):
        try:
            warn, bad = float(body.get("warn_ms")), float(body.get("bad_ms"))
        except (TypeError, ValueError):
            return {"error": "warn_ms and bad_ms must be numbers"}, 400
        if not 0 < warn < bad:
            return {"error": "need 0 < warn < bad"}, 400
        cfg = self.ctx.get_config()
        cfg["warn_ms"], cfg["bad_ms"] = warn, bad
        self.ctx.save_config()
        return {"message": "Thresholds saved"}
