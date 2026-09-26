"""Frame host message validator (PHF in web/index.html) under node: directed
hostile cases plus 6000 generated messages.  A frame is untrusted, so the
validator is the boundary between plugin JS and the dashboard's DOM/API.

Run: python3 tests/test_bridge_validator.py   (needs node; skipped otherwise)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


node = shutil.which("node")
if not node:
    print("SKIP: node not found")
    sys.exit(0)

p = subprocess.run([node, "tests/bridge_validator_harness.js", "web/index.html"], capture_output=True, text=True, timeout=120)
check("harness ran", p.returncode == 0 and p.stdout.strip() != "", (p.stderr or p.stdout)[-500:])
try:
    res = json.loads(p.stdout.strip().splitlines()[-1])
except Exception as e:  # noqa: BLE001
    res = {"failures": [{"name": "harness output unreadable", "detail": str(e) + p.stdout[-300:]}]}
if "fail" in res:
    res = {"failures": [{"name": res["fail"], "detail": ""}]}
for f in res.get("failures", []):
    check(f["name"], False, f["detail"])
check("no validator failures", not res.get("failures"))
check("fuzzer reached the renderer input (accepted nodes > 0)", res.get("fuzzed_nodes", 0) > 0, str(res))
print("  accepted nodes across fuzz run:", res.get("fuzzed_nodes"))

print()
if FAILED:
    print("FAILED:", ", ".join(FAILED))
    sys.exit(1)
print("ALL CHECKS PASSED")
