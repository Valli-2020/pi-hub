"""Release completeness: every file the running app needs is on the updater's
extraction allowlist, so a hub that self-updates gets all of it.

Frontend and frame JS therefore live inside web/index.html and pi_hub/plugins/*.py
(there is no way to ship a new file type through the 7.x updater).

Run: python3 tests/test_release_allowlist.py
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, ".")

from pi_hub import updater  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


tracked = git("ls-files").decode().split("\n")
tracked = [t for t in tracked if t]
archive = git("archive", "--prefix=pi-hub-9.9.9/", "HEAD")

extracted: set[str] = set()
with tempfile.TemporaryDirectory() as d, tarfile.open(fileobj=io.BytesIO(archive)) as tf:
    budget = [0]
    for m in tf.getmembers():
        updater._extract_member(tf, m, d, "pi-hub-9.9.9", budget)
    for dp, _dn, fn in os.walk(d):
        for f in fn:
            extracted.add(os.path.relpath(os.path.join(dp, f), d).replace(os.sep, "/"))

print("runtime files ship")
runtime = [t for t in tracked
           if (t.startswith("pi_hub/") and t.endswith(".py"))
           or t in ("run.py", "web/index.html", "web/logo.svg", "PLUGINS.md", "README.md", "DEPLOY.md")]
missing = [t for t in runtime if t not in extracted]
check("every pi_hub/**.py, run.py, web assets and doc is extracted", not missing, str(missing))
for must in ("pi_hub/plugins/frame.py", "pi_hub/plugins/contrib.py", "pi_hub/plugins/events.py"):
    check(must + " ships", must in extracted)

print("nothing the app needs is left behind")
deep = [t for t in tracked if t.startswith("pi_hub/") and t.count("/") > 2]
check("no code below pi_hub/plugins/ (the updater would drop it)", not deep, str(deep))
# Deliberately not shipped: icons.json is per-hub data (the updater comment says so),
# CONTRACT.md is a developer note.  Anything else that appears here is a mistake.
NOT_SHIPPED = {"web/icons.json", "pi_hub/CONTRACT.md"}
odd = [t for t in tracked if (t.startswith("pi_hub/") or t.startswith("web/"))
       and t not in extracted and t not in NOT_SHIPPED]
check("no tracked file under pi_hub/ or web/ is skipped", not odd, str(odd))

print("nothing private ships")
leak = sorted(e for e in extracted if any(x in e for x in ("config.json", "users.json", "sessions.json",
                                                            "secrets", "plugin_state", "tests/", "examples/")))
check("no config, secrets or tests in the extracted set", not leak, str(leak))

print()
if FAILED:
    print("FAILED:", ", ".join(FAILED))
    sys.exit(1)
print("ALL CHECKS PASSED")
