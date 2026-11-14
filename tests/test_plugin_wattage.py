"""wattage plugin: JSON path, schema, polling and badges against a stub server.

Run: python3 tests/test_plugin_wattage.py
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import sys
import threading

sys.path.insert(0, ".")

spec = importlib.util.spec_from_file_location("wattage", "pi_hub_plugins/wattage/__init__.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)

FAILED = []


def check(name, cond, detail=""):
    print(("  ok  " if cond else "FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redir":
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.end_headers()
            return
        body = json.dumps({"meters": [{"power": 42.5}]}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


srv = http.server.HTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % srv.server_port


class Ctx:
    def __init__(self):
        self.cfg = {}

    def get_config(self):
        return self.cfg

    def get_hosts(self):
        return [{"id": "nas-1", "name": "NAS"}, {"id": "pc", "name": "PC"}]


print("extract")
check("dict+list path", w.extract({"a": [{"b": "3.5"}]}, "a.0.b") == 3.5)
for bad in (({"a": 1}, "b"), ({"a": True}, "a"), ({"a": [1]}, "a.5")):
    try:
        w.extract(*bad)
        check("bad path rejected %r" % (bad,), False)
    except (KeyError, IndexError, ValueError):
        check("bad path rejected %r" % (bad,), True)

print("plugin")
p = w.Wattage()
ctx = Ctx()
p.load(ctx)
names = [f["name"] for f in p.get_config_schema()]
check("schema fields sanitised", "url_nas_1" in names and "path_pc" in names, str(names))

ctx.cfg.update({"url_nas_1": base + "/x", "path_nas_1": "meters.0.power",
                "url_pc": base + "/redir", "path_pc": "a"})
p.poll_all()
out = p.p_watts()
check("good reading", out["nas-1"]["text"] == "42.5 W" and out["nas-1"]["tone"] == "info", str(out))
check("redirect refused", out["pc"]["text"] == "— W" and out["pc"]["tone"] == "muted", str(out))
ctx.cfg["warn_w"] = 40
check("warn tone", p.p_watts()["nas-1"]["tone"] == "warn")
ctx.cfg["url_pc"] = ""
p.poll_all()
check("unmonitored host dropped", "pc" not in p.p_watts())
srv.shutdown()

print("FAILED: %s" % FAILED if FAILED else "all passed")
sys.exit(1 if FAILED else 0)
