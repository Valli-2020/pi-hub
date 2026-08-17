"""Pi Hub update mechanism — GitHub release check + verified in-place apply.

Stdlib only.  Owner/repo are module constants — never request-configurable
(no SSRF, no arbitrary-code install).  Deny-by-default extraction,
bounded expansion, compile-check before swap, same-filesystem atomic swap
with rollback, version parity read by regex (no exec of downloaded code),
restart via ``os._exit`` (the systemd unit carries ``Restart=always``).

Flow::

    check()   → cache GitHub latest release (30 min) + compare versions
    apply()   → download tarball → verify → stage → compile-check →
                backup → swap → exit

Core-only module (NOT a plugin since v7.5.0).  The update mechanism
lives in core so it can self-heal: a plugin failure can't break the
update path.  Routes are wired directly in ``pi_hub/routes.py``.
"""

from __future__ import annotations

import json
import os
import posixpath
import py_compile
import re
import shutil
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

OWNER = "Valli-2020"
REPO = "pi-hub"
API_LATEST = f"https://api.github.com/repos/{OWNER}/{REPO}/releases/latest"
CODELOAD_BASE = f"https://codeload.github.com/{OWNER}/{REPO}/tar.gz/refs/tags/"
CODELOAD_HOST = "codeload.github.com"
MAX_TARBALL = 10 * 1024 * 1024        # compressed cap, enforced in the read loop
MAX_EXPANDED = 20 * 1024 * 1024       # extracted cap across all members (gzip ~1000x)
MIN_FREE = 50 * 1024 * 1024           # disk_usage precheck headroom
CACHE_SECONDS = 30 * 60               # GitHub unauth limit = 60 req/hr
UA = "pi-hub-updater/7.1"
TIMEOUT = 8

# Tag: v7.1.0 / 7.1.0 / v7.1.0-rc1 — anything else is unusable.  The
# pre-release suffix is restricted to [0-9A-Za-z.] so a tag can never smuggle
# URL path separators into the codeload URL (defense in depth; the tag is
# also quoted before interpolation).
_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.]+)?$")
# Read-only version parity check — NEVER import/exec the downloaded tree.
_VERSION_RE = re.compile(rb"^__version__\s*=\s*['\"]([^'\"]+)['\"]", re.M)

# Deny-by-default extraction allowlist.  Everything else in the release
# tarball (config.json, users.json, sessions.json, icons.json, pyc, docs…)
# is skipped unconditionally — no preserve list that could be typo'd.
_ALLOWED = {
    "run.py",
    "web/index.html",
    "web/logo.svg",
    # Docs — shipped so installed hubs carry the current guides.
    "PLUGINS.md",
    "README.md",
    "DEPLOY.md",
}

_lock = threading.Lock()
_cache: dict[str, Any] = {
    "checked_at": None,       # epoch seconds
    "result": None,           # {"latest_version", "changelog", "published_at", "update_available"}
    "last_error": None,
}
_checking = {"flag": False}   # one forced check in flight at a time
_apply_running = {"flag": False, "tag": None}


# ── helpers ─────────────────────────────────────────────────────────────

def _version() -> str:
    """Installed version — read from the *running* package (own code)."""
    from pi_hub import __version__  # local import: avoids load-order issues
    return __version__


def _parse_tag(tag: str) -> tuple[int, int, int] | None:
    m = _TAG_RE.match(tag or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _install_root() -> str:
    """Install root: UPDATE_ROOT env override (sanity-checked) or the
    directory containing run.py (deployed layout: /home/pi/pi-hub/)."""
    override = os.environ.get("UPDATE_ROOT", "").strip()
    root = override or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    root = os.path.abspath(root)
    if not (os.path.isfile(os.path.join(root, "run.py"))
            and os.path.isfile(os.path.join(root, "pi_hub", "__init__.py"))):
        raise ValueError(f"install root {root} does not look like Pi Hub "
                         "(run.py + pi_hub/__init__.py missing)")
    return root


def _iso(epoch: float | None) -> str:
    if not epoch:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


# ── check ───────────────────────────────────────────────────────────────

def check(force: bool = False) -> dict[str, Any]:
    """Refresh the cached latest-release info (network OUTSIDE the lock).

    Never raises.  404 (no releases yet) = clean "no update", not an error.
    On any other failure the stale cache is kept and ``last_error`` set.
    At most one forced check runs at a time (in-flight marker); concurrent
    callers get the current cache instead of stampeding the GitHub rate
    limit.
    """
    with _lock:
        c = _cache
        if (not force and c["checked_at"]
                and time.time() - c["checked_at"] < CACHE_SECONDS):
            return c
        if force and _checking["flag"]:
            return c                       # another forced check is running
        _checking["flag"] = True
        c["last_error"] = None
    try:
        req = urllib.request.Request(
            API_LATEST,
            headers={"User-Agent": UA,
                     "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.loads(r.read(1 << 20))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            with _lock:
                c["result"] = None
                c["checked_at"] = time.time()
                c["last_error"] = None
            return c
        with _lock:
            c["last_error"] = f"GitHub HTTP {e.code}"
        return c
    except Exception as e:  # URLError / timeout / JSON …
        with _lock:
            c["last_error"] = f"network: {e}"
        return c
    finally:
        with _lock:
            _checking["flag"] = False

    tag = str(data.get("tag_name") or "").strip()
    ver = _parse_tag(tag)
    cur = _parse_tag(_version())
    # Strictly greater — a deleted/re-tagged release must never downgrade.
    available = bool(ver and cur and ver > cur)
    with _lock:
        c["result"] = {
            "latest_version": tag,
            "changelog": str(data.get("body") or ""),
            "published_at": str(data.get("published_at") or ""),
            "update_available": available,
        }
        c["checked_at"] = time.time()
        c["last_error"] = None
    return c


def status() -> dict[str, Any]:
    """API payload — always serves the cache, never triggers network I/O."""
    with _lock:
        c = _cache
        res = c["result"]
        return {
            "current_version": _version(),
            "latest_version": res["latest_version"] if res else None,
            "update_available": bool(res and res["update_available"]),
            "changelog": res["changelog"] if res else "",
            "published_at": res["published_at"] if res else "",
            "checked_at": _iso(c["checked_at"]),
            "last_error": c["last_error"],
            "busy": _apply_running["flag"],
        }


# ── apply ───────────────────────────────────────────────────────────────

def apply(expected_tag: str) -> tuple[dict[str, Any], int]:
    """Download the cached latest release, verify, swap, and exit.

    ``expected_tag`` must equal the CACHED latest release (TOCTOU guard:
    the admin approved that exact changelog).  The tag is validated against
    the semver regex before it is ever interpolated into a URL.
    """
    if not _TAG_RE.match(expected_tag or ""):
        return {"success": False, "error": "invalid release tag"}, 400
    # Single-flight check + claim in ONE lock section (atomic — a racing
    # second request cannot pass the check between release and re-acquire).
    with _lock:
        if _apply_running["flag"]:
            return {"success": False, "error": "update already in progress"}, 409
        res = _cache["result"]
        if not res or not res.get("latest_version"):
            return {"success": False,
                    "error": "no release known — check first"}, 409
        latest = res["latest_version"]
        if latest != expected_tag:
            return {"success": False,
                    "error": f"release changed (approved {expected_tag}, "
                             f"latest {latest}) — re-check"}, 409
        # Downgrade guard: the approved tag must be STRICTLY NEWER than
        # the running build.  check() only reports newer releases, but
        # the API accepts any expected_tag — a release deleted or
        # re-pointed backwards must never install an older, possibly
        # vulnerable build.
        new_ver = _parse_tag(latest)
        cur_ver = _parse_tag(_version())
        if not (new_ver and cur_ver and new_ver > cur_ver):
            return {"success": False,
                    "error": f"refusing downgrade ({latest} is not newer "
                             f"than {_version()})"}, 409
        _apply_running["flag"] = True
        _apply_running["tag"] = latest
    tag = latest
    try:
        root = _install_root()
    except ValueError as e:
        with _lock:
            _apply_running["flag"] = False
        return {"success": False, "error": str(e)}, 500
    try:
        st = shutil.disk_usage(root)
        if st.free < MIN_FREE:
            with _lock:
                _apply_running["flag"] = False
            return {"success": False,
                    "error": "not enough free disk space for the update"}, 500
    except OSError:
        pass
    try:
        err = _download_and_stage(root, tag)
        if err:
            with _lock:
                _apply_running["flag"] = False
            return {"success": False, "error": err}, 500
        err = _swap(root, tag)
        if err:
            with _lock:
                _apply_running["flag"] = False
            return {"success": False, "error": err}, 500
    except Exception as e:                  # safety net — never leave the
        with _lock:                         # flag set on a crash path
            _apply_running["flag"] = False
        return {"success": False, "error": f"update failed: {e}"}, 500
    # Success.  Keep the flag set so status()["busy"] stays true during the
    # restart window (re-entry guard).  PI_HUB_UPDATE_NOEXIT=1 keeps tests
    # alive and clears the flag so the harness can run another round.
    if os.environ.get("PI_HUB_UPDATE_NOEXIT") == "1":
        with _lock:
            _apply_running["flag"] = False
    else:
        threading.Timer(1.0, lambda: os._exit(0)).start()
    return {"success": True, "restarting": True, "version": tag}, 200


def _urlopen_stream(url: str, dest: str, cap: int) -> str | None:
    """Stream *url* to *dest* enforcing *cap* inside the read loop."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            final = r.geturl()
            final_host = urllib.parse.urlsplit(final).hostname or ""
            if final_host != CODELOAD_HOST:
                return f"download redirected to {final_host} — refused"
            size = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > cap:
                        return "release tarball exceeds size limit"
                    f.write(chunk)
    except urllib.error.HTTPError as e:
        return f"download failed: HTTP {e.code}"
    except Exception as e:
        return f"download failed: {e}"
    return None


def _extract_member(tf: tarfile.TarFile, member: tarfile.TarInfo,
                    dest_dir: str, top_dir: str, budget: list[int]) -> int:
    """Extract one member into dest_dir if it is on the allowlist.

    Returns bytes written (0 = skipped/rejected, -1 = budget exceeded).
    Rejects every member that is not a regular file, every path with '..'
    or a leading '/', every member outside the expected top-level dir, and
    anything outside the allowlist (deny-by-default).
    """
    name = member.name.replace("\\", "/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if len(parts) < 2 or parts[0] != top_dir:
        return 0                             # wrong top dir / absolute / ..
    rel = posixpath.join(*parts[1:])         # strip the <repo>-<tag>/ top dir
    if not member.isfile():
        return 0                             # symlink/hardlink/dev/dir: never
    if ".." in rel.split("/") or rel.startswith("/"):
        return 0
    allowed = rel in _ALLOWED or (
        rel.startswith("pi_hub/") and rel.count("/") == 1 and rel.endswith(".py")) or (
        rel.startswith("pi_hub/plugins/") and rel.count("/") == 2
        and rel.endswith(".py"))
    if not allowed:
        return 0
    out = os.path.join(dest_dir, *rel.split("/"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    src = tf.extractfile(member)
    if src is None:
        return 0
    written = 0
    with open(out, "wb") as f:
        while True:
            chunk = src.read(64 * 1024)
            if not chunk:
                break
            written += len(chunk)
            budget[0] += len(chunk)
            if budget[0] > MAX_EXPANDED:
                return -1
            f.write(chunk)
    # run.py keeps its exec bit; everything else is plain (modes/uid/gid in
    # the archive are never trusted).
    os.chmod(out, 0o755 if rel == "run.py" else 0o644)
    return written


def _download_and_stage(root: str, tag: str) -> str | None:
    """Fetch + verify + stage the release.  Returns an error string or None."""
    staging = os.path.join(root, ".pi-hub-staging", tag)
    shutil.rmtree(staging, ignore_errors=True)
    tree = os.path.join(staging, "tree")
    os.makedirs(tree, exist_ok=True)
    tarball = os.path.join(staging, "release.tar.gz")

    err = _urlopen_stream(
        CODELOAD_BASE + urllib.parse.quote(tag, safe=""), tarball, MAX_TARBALL)
    if err:
        shutil.rmtree(staging, ignore_errors=True)
        return err
    top_dir = f"{REPO}-{tag.lstrip('v')}"
    budget = [0]
    try:
        with tarfile.open(tarball, "r:gz") as tf:
            # Members are extracted via _extract_member() which enforces
            # file-only + allowlist + no-.. — equivalent to filter="data"
            # (which is 3.12-only; the Pi runs 3.11).
            for member in tf.getmembers():
                if _extract_member(tf, member, tree, top_dir, budget) == -1:
                    shutil.rmtree(staging, ignore_errors=True)
                    return "release expands beyond the size limit"
    except (tarfile.TarError, OSError, EOFError) as e:
        shutil.rmtree(staging, ignore_errors=True)
        return f"tarball invalid: {e}"

    # Version parity — read bytes, never execute.
    try:
        with open(os.path.join(tree, "pi_hub", "__init__.py"), "rb") as f:
            content = f.read()
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        return "release is missing pi_hub/__init__.py"
    m = _VERSION_RE.search(content)
    if not m:
        shutil.rmtree(staging, ignore_errors=True)
        return "release has no parseable __version__"
    if m.group(1).decode("utf-8", "replace") != tag.lstrip("v"):
        shutil.rmtree(staging, ignore_errors=True)
        return (f"release version {m.group(1).decode()} does not match "
                f"tag {tag} — refusing")
    for required in ("run.py", "web/index.html", "web/logo.svg"):
        if not os.path.isfile(os.path.join(tree, required)):
            shutil.rmtree(staging, ignore_errors=True)
            return f"release is missing {required}"

    # Compile-check the staged code BEFORE the point of no return — a
    # corrupt release must fail here, not as a systemd restart loop.
    try:
        for dirpath, _dirs, files in os.walk(tree):
            for f in files:
                if f.endswith(".py"):
                    py_compile.compile(os.path.join(dirpath, f),
                                       doraise=True)
        py_compile.compile(os.path.join(tree, "run.py"), doraise=True)
    except py_compile.PyCompileError as e:
        shutil.rmtree(staging, ignore_errors=True)
        return f"staged release does not compile: {e}"

    # Completeness check — the swap replaces whole directories, so any
    # .py module present in the LIVE tree must also be in the staged
    # tree.  Otherwise a release that (e.g.) adds pi_hub/<subpkg>/x.py
    # which the allowlist skips would silently DELETE it on swap and
    # boot into a systemd restart loop, past every rollback opportunity.
    missing = []
    for dirpath, _dirs, files in os.walk(os.path.join(root, "pi_hub")):
        for f in files:
            if not f.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, f), root)
            if not os.path.isfile(os.path.join(tree, rel)):
                missing.append(rel)
    if missing:
        shutil.rmtree(staging, ignore_errors=True)
        return (f"staged release missing {len(missing)} module(s) present "
                f"in the live tree (first: {missing[0]}) — refusing swap")
    return None


def _swap(root: str, tag: str) -> str | None:
    """Backup → atomic per-directory swap → rollback on failure → cleanup."""
    staging = os.path.join(root, ".pi-hub-staging", tag)
    tree = os.path.join(staging, "tree")
    # Backups live INSIDE the install root (.pi-hub-backups/), not in the
    # parent directory — /home/pi/pi-hub-backups worked, but an install
    # at /app would try to write /pi-hub-backups at the filesystem root
    # and fail (or worse, collide with another install).
    backup_dir = os.path.join(root, ".pi-hub-backups",
                              time.strftime("%Y%m%d-%H%M%S") +
                              f"-{os.getpid()}")

    pairs = [
        ("pi_hub", "pi_hub.old"),
        ("run.py", "run.py.old"),
        ("web/index.html", "web/index.html.old"),
        ("web/logo.svg", "web/logo.svg.old"),
        # Docs — extracted from the tarball AND swapped into place, so
        # installed hubs carry the current guides (they used to be
        # extracted but thrown away).
        ("PLUGINS.md", "PLUGINS.md.old"),
        ("README.md", "README.md.old"),
        ("DEPLOY.md", "DEPLOY.md.old"),
    ]
    swapped: list[tuple[str, str]] = []   # (original, backup_name) to restore

    def rollback() -> None:
        for orig, bak in reversed(swapped):
            new_path = os.path.join(root, orig)
            old_path = os.path.join(root, bak)
            if os.path.lexists(new_path):
                try:
                    if os.path.isdir(new_path) and not os.path.islink(new_path):
                        shutil.rmtree(new_path)
                    else:
                        os.remove(new_path)
                except OSError:
                    pass
            if os.path.lexists(old_path):
                try:
                    os.replace(old_path, new_path)
                except OSError:
                    pass

    try:
        os.makedirs(backup_dir, exist_ok=True)
        for rel in ("run.py", "pi_hub", "web/index.html", "web/logo.svg",
                    "PLUGINS.md", "README.md", "DEPLOY.md"):
            src = os.path.join(root, rel)
            if os.path.lexists(src):
                dst = os.path.join(backup_dir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.isdir(src) and not os.path.islink(src):
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
        # Stale .old salvage/cleanup: if the live file is missing and a .old
        # exists, the previous attempt died between the two replaces — the
        # .old IS the only copy; restore it instead of deleting it.
        for orig, bak in pairs:
            stale = os.path.join(root, bak)
            if os.path.lexists(stale):
                live = os.path.join(root, orig)
                if not os.path.lexists(live):
                    os.replace(stale, live)
                    continue
                if os.path.isdir(stale) and not os.path.islink(stale):
                    shutil.rmtree(stale)
                else:
                    os.remove(stale)
        # Swap — atomic per path via os.replace (same filesystem).
        # A path missing in the staged release is skipped — releases
        # may legitimately not contain every previously shipped path.
        for orig, bak in pairs:
            src = os.path.join(tree, orig)
            if not os.path.lexists(src):
                continue
            live = os.path.join(root, orig)
            if os.path.lexists(live):
                # Once the original is safely parked at <name>.old it MUST
                # be recorded — a failure on the second replace still
                # restores it.
                os.replace(live, os.path.join(root, bak))
                swapped.append((orig, bak))
            # Install fresh (also covers files missing in the old install).
            os.replace(src, live)
    except Exception as e:
        rollback()
        shutil.rmtree(staging, ignore_errors=True)
        return (f"swap failed (rolled back): {e} — backup kept in "
                f"{backup_dir}")

    # New code is live: drop the .old parks (the backup dir already holds
    # the old files), purge stale bytecode, drop staging, prune backups.
    for orig, bak in pairs:
        old = os.path.join(root, bak)
        if os.path.lexists(old):
            if os.path.isdir(old) and not os.path.islink(old):
                shutil.rmtree(old)
            else:
                os.remove(old)
    for dirpath, dirnames, _files in os.walk(root):
        if "__pycache__" in dirnames:
            # Only purge pycache inside the tree we manage (pi_hub, web,
            # pi_hub_plugins) — NOT the whole install root, which could
            # contain unrelated venvs/trees.
            rel = os.path.relpath(dirpath, root)
            if rel in ("pi_hub", "web", "pi_hub_plugins") or \
                    rel.startswith(os.path.join("pi_hub", "")) or \
                    rel.startswith(os.path.join("pi_hub_plugins", "")):
                shutil.rmtree(os.path.join(dirpath, "__pycache__"))
                dirnames.remove("__pycache__")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        bdir = os.path.dirname(backup_dir)
        if os.path.isdir(bdir):
            keep = sorted(os.listdir(bdir), reverse=True)[:3]
            for name in os.listdir(bdir):
                if name not in keep:
                    shutil.rmtree(os.path.join(bdir, name), ignore_errors=True)
    except OSError:
        pass
    return None
