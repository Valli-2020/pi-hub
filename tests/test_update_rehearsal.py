"""Update rehearsal: run the *7.7.0* updater (the code an installed hub really
executes) against a tarball of HEAD, on a throw-away 7.7.0 install.

Proves that a 7.7.0 hub can take this release: version parity, compile check,
completeness check, swap, and that the new plugin modules arrive.  Nothing here
touches the network or a real hub.

Run: python3 tests/test_update_rehearsal.py
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, ".")

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


if subprocess.run(["git", "rev-parse", "-q", "--verify", "v7.7.0"], capture_output=True).returncode:
    print("SKIP  tag v7.7.0 not present in this clone")
    sys.exit(0)

version = [l for l in git("show", "HEAD:pi_hub/__init__.py").decode().splitlines()
           if l.startswith("__version__")][0].split("=")[1].strip().strip("\"'")
tag = "v" + version

with tempfile.TemporaryDirectory() as tmp:
    # the updater exactly as 7.7.0 shipped it
    src = os.path.join(tmp, "updater_770.py")
    with open(src, "wb") as f:
        f.write(git("show", "v7.7.0:pi_hub/updater.py"))
    spec = importlib.util.spec_from_file_location("updater_770", src)
    up = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(up)

    # a 7.7.0 install with user data next to it
    root = os.path.join(tmp, "hub")
    os.makedirs(root)
    with tarfile.open(fileobj=io.BytesIO(git("archive", "v7.7.0"))) as tf:
        tf.extractall(root, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    for name, body in (("config.json", '{"hosts": []}'), ("users.json", "{}"), ("sessions.json", "{}")):
        with open(os.path.join(root, name), "w") as f:
            f.write(body)
    os.makedirs(os.path.join(root, "pi_hub_plugins", "keepme"))
    with open(os.path.join(root, "pi_hub_plugins", "keepme", "__init__.py"), "w") as f:
        f.write("# user plugin\n")
    before_old = open(os.path.join(root, "pi_hub", "__init__.py")).read()

    # the release tarball GitHub would serve for the tag
    tarball = os.path.join(tmp, "release.tar.gz")
    with open(tarball, "wb") as f:
        f.write(git("archive", "--format=tar.gz", f"--prefix=pi-hub-{version}/", "HEAD"))

    def fake_stream(url, dest, cap):
        shutil.copy(tarball, dest)
        return None

    up._urlopen_stream = fake_stream
    os.environ["BACKUP_ROOT"] = os.path.join(tmp, "backups")

    print("stage")
    err = up._download_and_stage(root, tag)
    check("7.7.0 updater accepts the release (%s)" % tag, err is None, str(err))
    if err is None:
        print("swap")
        err = up._swap(root, tag)
        check("swap succeeds", err is None, str(err))
        for rel in ("pi_hub/plugins/frame.py", "pi_hub/plugins/contrib.py", "pi_hub/plugins/events.py",
                    "pi_hub/plugins/manager.py", "web/index.html", "PLUGINS.md"):
            check(rel + " installed", os.path.isfile(os.path.join(root, rel)))
        check("version is now " + version, ('__version__ = "%s"' % version) in
              open(os.path.join(root, "pi_hub", "__init__.py")).read())
        check("user data untouched", all(os.path.isfile(os.path.join(root, n))
                                         for n in ("config.json", "users.json", "sessions.json",
                                                   "pi_hub_plugins/keepme/__init__.py")))
        check("a backup of the old code exists", os.path.isdir(os.path.join(tmp, "backups")))
        check("the old code really was 7.7.0", '"7.7.0"' in before_old)

        print("boot")
        r = subprocess.run([sys.executable, "-c",
                            "import sys; sys.path.insert(0,'.'); import pi_hub.server, pi_hub.plugins.frame, "
                            "pi_hub.plugins.contrib, pi_hub.plugins.events; print('imported')"],
                           cwd=root, capture_output=True, text=True)
        check("the updated tree imports cleanly", "imported" in r.stdout, r.stderr[-300:])

print()
if FAILED:
    print("FAILED:", ", ".join(FAILED))
    sys.exit(1)
print("ALL CHECKS PASSED")
