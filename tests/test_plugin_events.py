"""events.py: subscription caps, delivery, isolation, change detection
(host.state / container.state), watcher gating, core emit points.

Run: python3 tests/test_plugin_events.py
"""

from __future__ import annotations

import sys
import threading
import time

sys.path.insert(0, ".")

from pi_hub.plugins import events  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


events._ensure_watcher = lambda: None          # never probe real hosts from a test
events._reset_for_tests()

print("subscription")
got: list = []
granted = events.subscribe("p1", ["host.state", "scan.complete", "nope"], lambda e, p: got.append((e, p)),
                           {"hosts.read"})
check("only events whose capability is held are granted", granted == ["host.state"], str(granted))
check("has_subscribers reflects it", events.has_subscribers("host.state") and not events.has_subscribers("scan.complete"))
check("has_subscribers() without args", events.has_subscribers())
check("config.changed needs no capability",
      events.subscribe("p1", ["config.changed"], lambda e, p: got.append((e, p)), set()) == ["config.changed"])

print("delivery")
events.emit("host.state", {"host_id": "h1", "old": "up", "new": "down"})
events.emit("scan.complete", {"candidates": 3})            # not subscribed → dropped
events.emit("unknown.event", {})                           # unknown → dropped
events.drain()
check("subscribed event arrives with payload", got == [("host.state", {"host_id": "h1", "old": "up", "new": "down"})], str(got))

print("isolation")
seen: list = []


def boom(e, p):
    raise RuntimeError("plugin bug")


events.subscribe("bad", ["config.changed"], boom, set())
events.subscribe("good", ["config.changed"], lambda e, p: seen.append(p), set())
events.emit("config.changed", {"n": 1})
events.drain()
check("a raising plugin does not stop the others", seen == [{"n": 1}], str(seen))
events.emit("config.changed", {"n": 2})
events.drain()
check("dispatcher survives an exception", seen == [{"n": 1}, {"n": 2}], str(seen))

events.unsubscribe("bad")
slow_done = threading.Event()
events.subscribe("slow", ["config.changed"], lambda e, p: slow_done.wait(2), set())
t0 = time.monotonic()
for _ in range(50):
    events.emit("config.changed", {})
check("emit never blocks on a slow plugin", time.monotonic() - t0 < 0.5)
slow_done.set()
events.drain(5)

print("payload isolation")
box: list = []
events._reset_for_tests()
events.subscribe("a", ["config.changed"], lambda e, p: (p.update(mut=1), box.append(p)), set())
events.subscribe("b", ["config.changed"], lambda e, p: box.append(dict(p)), set())
events.emit("config.changed", {"x": 1})
events.drain()
check("each subscriber gets its own copy", {"x": 1} in box, str(box))

print("unsubscribe")
events.unsubscribe("a")
events.unsubscribe("b")
check("no subscribers left", not events.has_subscribers())

print("queue bound")
events._reset_for_tests()
block = threading.Event()
events.subscribe("stuck", ["config.changed"], lambda e, p: block.wait(3), set())
for _ in range(events._QUEUE_MAX + 200):
    events.emit("config.changed", {})
check("full queue drops instead of raising/growing", events._queue.qsize() <= events._QUEUE_MAX)
block.set()
events.drain(5)

print("host.state detection")
events._reset_for_tests()
hs: list = []
events.subscribe("w", ["host.state"], lambda e, p: hs.append(p), {"hosts.read"})
events.note_hosts({"a": {"online": True}, "b": {"online": False}})
events.drain()
check("first observation only seeds the baseline", hs == [], str(hs))
events.note_hosts({"a": {"online": True}, "b": {"online": False}})
events.drain()
check("no change → no event", hs == [])
events.note_hosts({"a": {"online": False}, "b": {"online": True}})
events.drain()
check("changes emit old/new", sorted((p["host_id"], p["old"], p["new"]) for p in hs) ==
      [("a", "up", "down"), ("b", "down", "up")], str(hs))
events._reset_for_tests()
events.note_hosts({"a": {"online": True}})
events.subscribe("w", ["host.state"], lambda e, p: hs.append(p), {"hosts.read"})
hs.clear()
events.note_hosts({"a": {"online": False}})
events.drain()
check("baseline is not recorded while nobody listens", hs == [], str(hs))

print("container.state detection")
events._reset_for_tests()
cs: list = []
events.subscribe("w", ["container.state"], lambda e, p: cs.append(p), {"proxmox.read"})
events.note_containers("keller", [{"vmid": "100", "name": "jf", "status": "running"}])
events.note_containers("keller", [{"vmid": "100", "name": "jf", "status": "stopped"},
                                  {"vmid": "101", "name": "new", "status": "running"}])
events.drain()
check("container transition emitted once, new container is baseline only",
      cs == [{"instance": "keller", "vmid": "100", "name": "jf", "old": "running", "new": "stopped"}], str(cs))
events.note_containers("other", [{"vmid": "100", "name": "x", "status": "running"}])
events.note_containers("other", [{"vmid": "100", "name": "x", "status": "running"}])
events.drain()
check("instances are tracked separately", len(cs) == 1)

print("core emit points")
events._reset_for_tests()
cfg: list = []
events.subscribe("w", ["config.changed", "scan.complete"], lambda e, p: cfg.append((e, p)), {"services.read"})
from pi_hub import config as cfgmod  # noqa: E402
import os, tempfile  # noqa: E402,E401
orig = cfgmod.CONFIG_PATH
try:
    with tempfile.TemporaryDirectory() as d:
        cfgmod.CONFIG_PATH = os.path.join(d, "config.json")
        ok = cfgmod.save_config({"hosts": []})
    events.drain()
finally:
    cfgmod.CONFIG_PATH = orig
    cfgmod._cache = {}
    cfgmod._cache_mtime = 0.0
check("save_config emits config.changed", ok and ("config.changed", {}) in cfg, str(cfg))

import inspect  # noqa: E402
from pi_hub import hosts, proxmox, scanner  # noqa: E402
check("hosts.get_all_status feeds note_hosts", "plugin_events.note_hosts" in inspect.getsource(hosts.get_all_status))
check("proxmox fetch feeds note_containers", "plugin_events.note_containers" in inspect.getsource(proxmox._fetch_instance))
check("scanner emits scan.complete", 'plugin_events.emit("scan.complete"' in inspect.getsource(scanner.run_scan))

print("watcher gating")
started: list = []
events._ensure_watcher = lambda: started.append(1)
events._reset_for_tests()
events.subscribe("x", ["config.changed"], lambda e, p: None, set())
check("config.changed alone does not start the watcher", not started)
events.subscribe("x", ["host.state"], lambda e, p: None, {"hosts.read"})
check("host.state subscription starts the watcher", started == [1])

events._reset_for_tests()
print()
if FAILED:
    print("FAILED:", ", ".join(FAILED))
    sys.exit(1)
print("ALL CHECKS PASSED")
