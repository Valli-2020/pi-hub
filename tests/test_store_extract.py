"""store.py extraction allowlist: multi-file plugins, frame assets, limits.

Run: python3 tests/test_store_extract.py
"""

from __future__ import annotations

import io
import os
import sys
import tarfile
import tempfile

sys.path.insert(0, ".")

from pi_hub.plugins import store  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


def make(files: dict, prefix: str = "myplug/", extra=None) -> tarfile.TarFile:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, data in files.items():
            ti = tarfile.TarInfo(prefix + path)
            data = data if isinstance(data, bytes) else data.encode()
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        for ti in (extra or []):
            tf.addfile(ti)
    buf.seek(0)
    return tarfile.open(fileobj=buf, mode="r:gz")


def extract(files: dict, **kw):
    d = tempfile.mkdtemp()
    tf = make(files, **kw)
    err = store._find_in_archive(tf, d, [0])
    got = sorted(os.path.relpath(os.path.join(r, f), d) for r, _, fs in os.walk(d) for f in fs)
    return err, got


print("layout")
err, got = extract({"__init__.py": "x=1"})
check("plain plugin", err is None and got == ["__init__.py"], str((err, got)))
err, got = extract({"__init__.py": "x=1"}, prefix="pi_hub_plugins/myplug/")
check("pi_hub_plugins/<name>/ layout", err is None and got == ["__init__.py"], str((err, got)))

print("multi-file plugins")
err, got = extract({"__init__.py": "", "helpers.py": "", "pihub-plugin.json": "{}", "README.md": "", "LICENSE": "",
                    "pkg/__init__.py": "", "pkg/util.py": "", "config.json": "{}"})
check("top-level modules, manifest, docs, one subpackage", err is None and got == sorted(
    ["__init__.py", "helpers.py", "pihub-plugin.json", "README.md", "LICENSE", "pkg/__init__.py", "pkg/util.py", "config.json"]), str((err, got)))
err, got = extract({"__init__.py": "", "pkg/sub/deep.py": "", "pkg/data.txt": "", "notes.txt": "", "setup.cfg": "", "bad-name.py": "",
                    "9x.py": "", "static/__init__.py": ""})
check("deeper trees / other types / non-identifier names are skipped",
      err is None and got == ["__init__.py", "static/__init__.py"], str((err, got)))

print("junk")
err, got = extract({"__init__.py": "", ".env": "SECRET=1", ".git/config": "", "__pycache__/x.cpython-311.pyc": "", "mod.pyc": "",
                    "static/.hidden.js": "", "static/ok.js": ""})
check("dotfiles, __pycache__ and .pyc never extract", err is None and got == ["__init__.py", "static/ok.js"], str((err, got)))

print("frame assets")
err, got = extract({"__init__.py": "", "static/frame/main.js": "1", "static/frame/a.css": "", "static/frame/i.svg": "<svg/>",
                    "static/frame/p.png": "x", "static/frame/d.json": "{}", "static/frame/f.woff2": "x", "static/frame/sub/x.js": ""})
check("inert frame types extract (subdirs too)", err is None and len(got) == 8, str((err, got)))
for bad in ("static/frame/index.html", "static/frame/x.wasm", "static/frame/x.svgz", "static/frame/x.mjs", "static/frame/noext",
            "static/frame/x.JS.html"):
    err, _ = extract({"__init__.py": "", bad: "x"})
    check("frame type rejected: " + bad, err is not None, str(err))
err, got = extract({"__init__.py": "", "static/frame/UPPER.JS": "1"})
check("extension check is case-insensitive", err is None, str(err))
err, _ = extract({"__init__.py": "", "static/frame/big.js": "x" * (store.MAX_FRAME_FILE + 1)})
check("frame file over 512 KB rejected", err is not None, str(err))
err, _ = extract({"__init__.py": "", "static/frame/ok.js": "x" * store.MAX_FRAME_FILE})
check("frame file at exactly 512 KB accepted", err is None, str(err))
err, _ = extract({"__init__.py": "", **{"static/frame/f%d.js" % i: "x" * (store.MAX_FRAME_FILE - 1) for i in range(5)}})
check("frame total over 2 MB rejected", err is not None, str(err))
err, got = extract({"__init__.py": "", "static/big.bin": "x" * (store.MAX_FRAME_FILE + 10)})
check("non-frame static keeps the old rules (any type/size within the global cap)", err is None and "static/big.bin" in got, str(err))

print("limits and hostile archives")
err, _ = extract({"__init__.py": "", **{"static/f%d.txt" % i: "" for i in range(store.MAX_PLUGIN_FILES + 1)}})
check("more than 300 files rejected", err is not None, str(err))
err, _ = extract({"__init__.py": "", **{"static/f%d.txt" % i: "" for i in range(store.MAX_PLUGIN_FILES - 1)}})
check("300 files accepted", err is None, str(err))
err, got = extract({"__init__.py": "", "../evil.py": "x", "static/../../evil.py": "x"})
check("path traversal never writes outside", (err is not None or got == ["__init__.py"]) and not os.path.exists("/tmp/evil.py"), str((err, got)))
sym = tarfile.TarInfo("myplug/link.py")
sym.type = tarfile.SYMTYPE
sym.linkname = "/etc/passwd"
err, got = extract({"__init__.py": ""}, extra=[sym])
check("symlinks are skipped", err is None and got == ["__init__.py"], str((err, got)))
err, got = extract({"/abs.py": "x"}, prefix="")
check("absolute member paths are not written", err is not None or got == [], str((err, got)))

print("classifier")
for rel, want in [("__init__.py", True), ("a.py", True), ("a/b.py", True), ("a/b/c.py", False), ("static/a/b/c.txt", True),
                  ("static/frame/a.html", None), ("x.txt", False), (".x.py", False), ("a/.b.py", False), ("Makefile", False)]:
    ok, e, _ = store._classify(rel)
    check("classify %s" % rel, (want is None and e is not None) or (want is not None and ok == want and e is None), str((ok, e)))

print()
if FAILED:
    print("FAILED:", ", ".join(FAILED))
    sys.exit(1)
print("ALL CHECKS PASSED")
