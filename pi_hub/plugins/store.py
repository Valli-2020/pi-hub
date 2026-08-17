"""Pi Hub plugin store — GitHub sources, discovery, install, lifecycle.

The store layer sits on top of the existing plugin system
(:mod:`pi_hub.plugins.manager`).  An admin adds a GitHub repo as a
*plugin source*; the store scans its releases for ``pihub-plugin.json``
manifest assets, and discovered plugins can be installed / enabled /
disabled / uninstalled from the Settings UI.

Security
--------
- Only github.com source URLs are accepted (SSRF guard).
- ``asset_url`` is resolved server-side from the scan cache — the
  client only ever sends ``(source_id, name, version)``.
- Tarball extraction is deny-by-default: file-only members, no symlink
  / device / ``..`` paths, expansion capped, py_compile gate before
  anything lands in ``pi_hub_plugins/``.
- ``py_compile`` is a broken-archive check, NOT a security gate.  The
  trust model is: the admin curates sources.
- Both JSON stores are written atomically (tmp + fsync + os.replace).
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
from typing import Any, Dict, List, Optional, Tuple

# ═══════════════════════════════════════════════════════════════════════════════
# Paths & constants
# ═══════════════════════════════════════════════════════════════════════════════

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))                     # pi_hub/plugins/../.. = repo root
SOURCES_PATH = os.path.join(_REPO_ROOT, "plugins_sources.json")
PLUGINS_ROOT = os.path.join(_REPO_ROOT, "pi_hub_plugins")

# Manifest asset name inside a GitHub release.
MANIFEST_ASSET = "pihub-plugin.json"

# Download / extraction limits (mirrors updater.py philosophy).
MAX_TARBALL = 5 * 1024 * 1024        # compressed cap, read-loop enforced
MAX_EXPANDED = 10 * 1024 * 1024      # extracted cap across all members
TIMEOUT = 8
UA = "pi-hub-plugin-store/7.2"

# Extraction allowlist — deny-by-default: NOTHING else is extracted.
_ALLOWED = {
    "__init__.py",
    "config.json",
}
_ALLOWED_PREFIXES = ("static/",)

_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# Plugin names must not be "." or ".." (or any all-dots string) — those
# are path components, and os.path.join(PLUGINS_ROOT, "..") would escape
# the plugins root into the repo root, letting uninstall/install rmtree
# the whole hub.
_PLUGIN_NAME_RE = re.compile(r"^(?!\.{1,64}$)[A-Za-z0-9._-]{1,64}$")

_lock = threading.Lock()
_cache: Dict[str, Any] = {"sources": []}   # loaded lazily; write-through

# Serializes install/uninstall/enable/disable mutations of the plugins
# tree and the staging area — ThreadingHTTPServer can otherwise interleave
# two installs of the same name on shared .staging-<name> paths.
_install_lock = threading.Lock()


# ═══════════════════════════════════════════════════════════════════════════════
# Sources store (plugins_sources.json)
# ═══════════════════════════════════════════════════════════════════════════════


def _load_sources() -> Dict[str, Any]:
    with _lock:
        try:
            with open(SOURCES_PATH) as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("sources"), list):
                _cache.update(data)
                return _cache
        except (OSError, ValueError):
            pass
        _cache["sources"] = []
        return _cache


def _save_sources() -> bool:
    """Atomically persist the sources store (0600)."""
    with _lock:
        try:
            tmp = SOURCES_PATH + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(_cache, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, SOURCES_PATH)
        except OSError:
            return False
    return True


def list_sources() -> List[Dict[str, Any]]:
    """Return every configured source (with cached scan results)."""
    return _load_sources()["sources"]


def _find_source(source_id: str) -> Optional[Dict[str, Any]]:
    return next((s for s in list_sources() if s.get("id") == source_id), None)


def _parse_github_url(url: str) -> Tuple[str, str] | None:
    """Parse a github.com repo URL → (owner, repo).  None if not allowed.

    Accepts ``https://github.com/<owner>/<repo>`` (with or without
    trailing ``.git`` / path segments).  Anything else — other hosts,
    non-https, raw.githubusercontent, codeload — is rejected.
    """
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if p.scheme != "https" or (p.netloc.lower() != "github.com"):
        return None
    parts = [s for s in p.path.split("/") if s]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    repo = repo[:-4] if repo.endswith(".git") else repo
    if not re.match(r"^[A-Za-z0-9_.-]{1,64}$", owner) or \
            not re.match(r"^[A-Za-z0-9_.-]{1,64}$", repo):
        return None
    return owner, repo


def add_source(url: str) -> Tuple[Dict[str, Any] | None, str | None]:
    """Validate + append a plugin source.  Returns (source, error)."""
    parsed = _parse_github_url(str(url or "").strip())
    if not parsed:
        return None, "url must be https://github.com/<owner>/<repo>"
    owner, repo = parsed
    for s in list_sources():
        if s.get("owner") == owner and s.get("repo") == repo:
            return None, "source already added"
    source = {
        "id": f"{owner}-{repo}",
        "owner": owner,
        "repo": repo,
        "url": f"https://github.com/{owner}/{repo}",
        "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "last_scanned": None,
        "status": "active",
        "plugins": [],          # cached scan results for this source
    }
    _load_sources()["sources"].append(source)
    if not _save_sources():
        _load_sources()["sources"].pop()
        return None, "could not write plugins_sources.json"
    return source, None


def remove_source(source_id: str) -> Tuple[bool, str]:
    """Remove a source and uninstall every plugin that came from it."""
    src = _find_source(source_id)
    if not src:
        return False, f"unknown source: {source_id}"
    # Uninstall plugins whose provenance points here (files stay only if
    # they were also installed from elsewhere — not possible today).
    for pl in list(src.get("plugins", [])):
        if pl.get("installed"):
            uninstall(str(pl.get("name", "")))
    # Re-read AFTER uninstall — uninstall() reloads + rewrites the cache,
    # so a stale list reference would silently resurrect the source.
    sources = _load_sources()["sources"]
    sources[:] = [s for s in sources if s.get("id") != source_id]
    if not _save_sources():
        return False, "could not write plugins_sources.json"
    return True, f"source '{source_id}' removed"


# ═══════════════════════════════════════════════════════════════════════════════
# GitHub scan
# ═══════════════════════════════════════════════════════════════════════════════


def _gh_get(url: str) -> Tuple[Any, str | None]:
    """GET a GitHub API URL.  Returns (json, error).

    Uses ``GITHUB_TOKEN`` env var when set — required for private plugin
    repos and raises the unauthenticated rate limit (60 req/hr).
    """
    headers = {"User-Agent": UA,
               "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read(1 << 20)), None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None, "not found"
        if e.code == 403:
            return None, "GitHub rate limit (403) — try again later"
        return None, f"GitHub HTTP {e.code}"
    except Exception as e:
        return None, f"network: {e}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects automatically — the caller walks them
    manually so credentials can be scrubbed on cross-host hops."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None      # abort automatic handling; caller sees the 3xx


def _gh_asset(owner: str, repo: str, asset_id: int) -> Tuple[bytes, str | None]:
    """Download a release asset by API id (raw bytes).

    Uses the API endpoint (``/releases/assets/<id>``) with octet-stream
    accept — this is the ONLY reliable way to fetch assets of PRIVATE
    repos (``browser_download_url`` 404s without a signed redirect).

    Security:
    - redirects are followed MANUALLY (automatic following disabled via
      ``_NoRedirect``), and the ``Authorization`` header is stripped when
      the redirect target host differs from api.github.com — urllib's
      default handler would otherwise copy the full header set and leak
      ``GITHUB_TOKEN`` to release-assets.githubusercontent.com / S3.
    - the body is read with a cap (MAX_TARBALL + slack) instead of an
      unbounded ``r.read()``, so a multi-GB asset can't OOM the Pi.
    """
    headers = {"User-Agent": UA,
               "Accept": "application/octet-stream"}
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{int(asset_id)}"
    cap = MAX_TARBALL + (1 << 20)          # allow a little slack over the cap
    opener = urllib.request.build_opener(_NoRedirect)
    _REDIRECT_CODES = (301, 302, 303, 307, 308)
    try:
        req = urllib.request.Request(url, headers=headers)
        try:
            r = opener.open(req, timeout=TIMEOUT)
        except urllib.error.HTTPError as e:
            if e.code not in _REDIRECT_CODES:
                raise
            r = e                      # first hop — loop below handles it
        hops = 0
        # With _NoRedirect the opener RAISES HTTPError on 3xx instead of
        # returning a response — catch redirects on the hop and re-issue
        # manually so credentials can be scrubbed cross-host.
        while r.status in _REDIRECT_CODES:
            loc = r.headers.get("Location", "")
            r.close()
            if not loc:
                return b"", "redirect without Location"
            hops += 1
            if hops > 5:                 # redirect loop — give up
                return b"", "too many redirects"
            next_url = urllib.parse.urljoin(r.geturl(), loc)
            next_host = urllib.parse.urlsplit(next_url).netloc.lower()
            if next_host == urllib.parse.urlsplit(url).netloc.lower():
                req = urllib.request.Request(next_url, headers=headers)
            else:
                # Cross-host: drop Authorization, keep the rest.
                safe = {k: v for k, v in headers.items()
                        if k.lower() != "authorization"}
                req = urllib.request.Request(next_url, headers=safe)
            try:
                r = opener.open(req, timeout=TIMEOUT)
            except urllib.error.HTTPError as e:
                if e.code in _REDIRECT_CODES:
                    r = e                      # another hop — loop continues
                else:
                    raise
        try:
            buf = bytearray()
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > cap:
                    return b"", "asset exceeds size limit"
            return bytes(buf), None
        finally:
            r.close()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return b"", "not found"
        if e.code == 403:
            return b"", "GitHub rate limit (403) — try again later"
        return b"", f"GitHub HTTP {e.code}"
    except Exception as e:
        return b"", f"network: {e}"


def _parse_manifest(data: Any) -> Dict[str, Any] | None:
    """Validate a pihub-plugin.json manifest dict.  Returns None on error."""
    if not isinstance(data, dict):
        return None
    name = str(data.get("name", "")).strip()
    version = str(data.get("version", "")).strip()
    if not _PLUGIN_NAME_RE.fullmatch(name):
        return None
    # fullmatch, not match — a trailing payload like
    # '1.0.0"><img src=x onerror=…>' must be rejected outright, not
    # stored and echoed back.
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        return None
    return {
        "name": name,
        "version": version,
        "description": str(data.get("description", "")).strip(),
        "min_core_version": str(data.get("min_core_version", "")).strip(),
        "plugin_api_version": int(data.get("plugin_api_version", 1) or 1),
        "author": str(data.get("author", "")).strip(),
        "license": str(data.get("license", "")).strip(),
    }


def scan_source(source_id: str, force: bool = False) -> Tuple[List[Dict[str, Any]], str | None]:
    """Scan a source's GitHub releases for plugin manifests.

    Returns (plugins, error).  Results are cached in
    ``plugins_sources.json`` under ``source["plugins"]``.  A scan is
    cheap (1 release-list request + 1 asset download per release) but
    still subject to the unauthenticated GitHub rate limit, so the cache
    is served unless *force* is set.
    """
    from pi_hub import __version__

    src = _find_source(source_id)
    if not src:
        return [], f"unknown source: {source_id}"
    if src.get("plugins") and not force:
        return list(src["plugins"]), None

    owner, repo = src["owner"], src["repo"]
    data, err = _gh_get(f"https://api.github.com/repos/{owner}/{repo}/releases?per_page=10")
    if err:
        return [], err
    if not isinstance(data, list):
        return [], "unexpected GitHub response"

    found: List[Dict[str, Any]] = []
    for rel in data:
        tag = str(rel.get("tag_name") or "")
        assets = rel.get("assets") or []
        manifest_asset = next(
            (a for a in assets if str(a.get("name")) == MANIFEST_ASSET), None)
        if not manifest_asset or not manifest_asset.get("id"):
            continue
        raw, merr = _gh_asset(owner, repo, int(manifest_asset["id"]))
        if merr:
            continue                                # unreadable manifest → skip release
        try:
            mdata = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        manifest = _parse_manifest(mdata)
        if not manifest:
            continue
        # The code tarball is the *other* asset in the release.
        tarball_asset = next(
            (a for a in assets if str(a.get("name")) != MANIFEST_ASSET
             and str(a.get("name")).endswith((".tar.gz", ".tgz"))), None)
        if not tarball_asset or not tarball_asset.get("id"):
            continue
        manifest["source_id"] = source_id
        manifest["release_tag"] = tag
        manifest["asset_id"] = int(tarball_asset["id"])
        # Core-version + ABI compatibility flags (server-side decision).
        manifest["core_compatible"] = _core_compatible(
            manifest.get("min_core_version", ""), __version__)
        found.append(manifest)

    # Deduplicate by plugin name — keep only the highest semver.  Older
    # releases still exist on GitHub (for rollback), but the store UI
    # must not present them as separate installable plugins.
    latest: Dict[str, Dict[str, Any]] = {}
    for manifest in found:
        name = manifest.get("name", "")
        ver = _semver_tuple(manifest.get("version", "")) or (0, 0, 0)
        prev = latest.get(name)
        if prev is None:
            latest[name] = manifest
            continue
        prev_ver = _semver_tuple(prev.get("version", "")) or (0, 0, 0)
        if ver > prev_ver:
            latest[name] = manifest
    found = list(latest.values())

    # Preserve the installed/enabled flags from the previous cache —
    # but ONLY if the installed version is still the latest.  If a newer
    # release appeared since the last scan, it is NOT yet installed even
    # if an older release of the same plugin is on disk; the UI must
    # show it as "available" with an Install button.
    prev_plugins: Dict[str, Dict[str, Any]] = {}
    for p in src.get("plugins", []):
        n = p.get("name", "")
        if n not in prev_plugins:
            prev_plugins[n] = p
        else:
            old_installed = prev_plugins[n].get("installed") is True
            new_installed = p.get("installed") is True
            if not old_installed and new_installed:
                prev_plugins[n] = p
    for manifest in found:
        old = prev_plugins.get(manifest.get("name"))
        if old and old.get("version") == manifest.get("version"):
            if old.get("installed") is not None:
                manifest["installed"] = old["installed"]
            if old.get("enabled") is not None:
                manifest["enabled"] = old["enabled"]

    src["plugins"] = found
    src["last_scanned"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not _save_sources():
        return found, "scan ok, but could not persist cache"
    return found, None


def _semver_tuple(v: str) -> Tuple[int, int, int] | None:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", v or "")
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _core_compatible(need: str, have: str) -> bool:
    """True if *need* (min_core_version) <= *have* (running core)."""
    a, b = _semver_tuple(need), _semver_tuple(have)
    return a is not None and b is not None and a <= b


# ═══════════════════════════════════════════════════════════════════════════════
# Install / uninstall / enable / disable
# ═══════════════════════════════════════════════════════════════════════════════


def _plugins_manifest_path() -> str:
    return os.path.join(PLUGINS_ROOT, "plugins.json")


def _read_manifest() -> List[str]:
    try:
        with open(_plugins_manifest_path()) as f:
            return [str(e) for e in json.load(f).get("enabled", [])
                    if isinstance(e, str)]
    except (OSError, ValueError):
        return []


def _write_manifest(enabled: List[str]) -> bool:
    os.makedirs(PLUGINS_ROOT, exist_ok=True)
    try:
        tmp = _plugins_manifest_path() + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"enabled": enabled}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _plugins_manifest_path())
    except OSError:
        return False
    return True


def _mark_enabled(name: str, enabled: bool) -> None:
    for s in _load_sources()["sources"]:
        for pl in s.get("plugins", []):
            if pl.get("name") == name:
                pl["enabled"] = enabled


def enable(name: str) -> Tuple[bool, str]:
    """Add *name* to plugins.json and load it (runtime enable)."""
    if not _PLUGIN_NAME_RE.fullmatch(name or ""):
        return False, "invalid plugin name"
    enabled = _read_manifest()
    if name in enabled:
        return True, f"plugin '{name}' already enabled"
    enabled.append(name)
    if not _write_manifest(enabled):
        return False, "could not write plugins.json"
    from pi_hub.plugins import get_manager
    try:
        get_manager().load_plugin(name)
    except Exception as e:
        # Roll back the manifest entry so state stays honest.
        _write_manifest([n for n in enabled if n != name])
        return False, f"plugin failed to load: {e}"
    _mark_enabled(name, True)
    _save_sources()
    return True, f"plugin '{name}' enabled"


def disable(name: str) -> Tuple[bool, str]:
    """Remove *name* from plugins.json and unload it (runtime disable)."""
    if not _PLUGIN_NAME_RE.fullmatch(name or ""):
        return False, "invalid plugin name"
    enabled = _read_manifest()
    if name not in enabled:
        return True, f"plugin '{name}' already disabled"
    enabled = [n for n in enabled if n != name]
    if not _write_manifest(enabled):
        return False, "could not write plugins.json"
    from pi_hub.plugins import get_manager
    get_manager().unload_plugin(name)
    _mark_enabled(name, False)
    _save_sources()
    return True, f"plugin '{name}' disabled"


def _plugin_dir(name: str) -> str:
    """Absolute plugin directory under PLUGINS_ROOT, with a containment
    check: the resolved path MUST stay inside PLUGINS_ROOT (defense in
    depth on top of the name regex — a crafted name must never escape
    into the repo root."""
    d = os.path.realpath(os.path.join(PLUGINS_ROOT, name))
    root = os.path.realpath(PLUGINS_ROOT)
    if d != root and not d.startswith(root + os.sep):
        raise ValueError(f"plugin path escapes plugins root: {name}")
    return d


def _extract_member(tf: tarfile.TarFile, member: tarfile.TarInfo,
                    dest_dir: str, budget: List[int]) -> Tuple[int, str | None]:
    """Extract one member if allowlisted.  Returns (bytes_written, error).

    Rejects: non-regular files (symlinks/devices/dirs), any path with
    ``..`` or a leading slash, anything outside the allowlist.

    The archive's leading directory components are stripped to the
    plugin root: both ``pi_hub_plugins/<name>/...`` and ``<name>/...``
    layouts extract into ``dest_dir`` directly.
    """
    name = member.name.replace("\\", "/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if len(parts) < 2:
        return 0, None
    # Strip the top-level dir(s): "pi_hub_plugins/<name>/X" → "X",
    # "<name>/X" → "X".  Only the plugin's own files are allowed.
    rel_parts = list(parts)
    if rel_parts[0] == "pi_hub_plugins":
        rel_parts.pop(0)
    rel_parts.pop(0)                                  # plugin-name component
    rel = posixpath.join(*rel_parts) if rel_parts else ""
    if not rel:
        return 0, None
    if not member.isfile():
        return 0, None
    if ".." in rel.split("/") or rel.startswith("/"):
        return 0, "unsafe path in archive"
    allowed = rel in _ALLOWED or rel.startswith(_ALLOWED_PREFIXES)
    if not allowed:
        return 0, None
    out = os.path.join(dest_dir, *rel.split("/"))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    src = tf.extractfile(member)
    if src is None:
        return 0, None
    written = 0
    with open(out, "wb") as f:
        while True:
            chunk = src.read(64 * 1024)
            if not chunk:
                break
            written += len(chunk)
            budget[0] += len(chunk)
            if budget[0] > MAX_EXPANDED:
                return -1, "archive expands beyond the size limit"
            f.write(chunk)
    os.chmod(out, 0o644)
    return written, None


def _find_in_archive(tf: tarfile.TarFile, dest_dir: str, budget: List[int]) -> str | None:
    """Extract the plugin files from a tarball.  Returns error or None."""
    for member in tf.getmembers():
        _, err = _extract_member(tf, member, dest_dir, budget)
        if err:
            return err
    return None


def install(source_id: str, name: str, version: str) -> Tuple[bool, str]:
    """Install a plugin from a *cached scan result* (never client URLs).

    ``(source_id, name, version)`` must match an entry in the source's
    scan cache; the tarball URL is resolved server-side.  An existing
    install of the same name is replaced (upgrade path): disable → swap
    dir → (optionally re-enable by the caller).
    """
    with _install_lock:
        return _install_locked(source_id, name, version)


def _install_locked(source_id: str, name: str, version: str) -> Tuple[bool, str]:
    if not _PLUGIN_NAME_RE.fullmatch(name or ""):
        return False, "invalid plugin name"
    src = _find_source(source_id)
    if not src:
        return False, f"unknown source: {source_id}"
    cand = next((p for p in src.get("plugins", [])
                 if p.get("name") == name and p.get("version") == version), None)
    if not cand:
        return False, f"'{name}' v{version} not in scan cache — scan the source first"
    # Enforce the compatibility flags computed at scan time — the UI
    # shows a disabled Install button, but the API is the boundary.
    if cand.get("core_compatible") is False:
        return False, f"'{name}' v{version} requires a newer Pi Hub core"
    asset_id = cand.get("asset_id")
    if not asset_id:
        return False, "cached entry has no asset_id"

    target_dir = _plugin_dir(name)
    staging = os.path.join(PLUGINS_ROOT, f".staging-{name}")
    tmp_tarball = os.path.join(PLUGINS_ROOT, f".staging-{name}.tar.gz")

    # 1. Download + verify into staging FIRST.  The live install is not
    #    touched until the new tree is proven good (download, size cap,
    #    extraction, compile gate all happen below) — a failure at any
    #    step leaves the working plugin in place.
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)

    raw, err = _gh_asset(src["owner"], src["repo"], int(asset_id))
    if err:
        shutil.rmtree(staging, ignore_errors=True)
        return False, err
    if len(raw) > MAX_TARBALL:
        shutil.rmtree(staging, ignore_errors=True)
        return False, "tarball exceeds size limit"
    with open(tmp_tarball, "wb") as f:
        f.write(raw)

    budget = [0]
    try:
        with tarfile.open(tmp_tarball, "r:gz") as tf:
            err = _find_in_archive(tf, staging, budget)
            if err:
                shutil.rmtree(staging, ignore_errors=True)
                return False, err
    except (tarfile.TarError, OSError, EOFError) as e:
        shutil.rmtree(staging, ignore_errors=True)
        return False, f"tarball invalid: {e}"
    finally:
        try:
            os.remove(tmp_tarball)  # never leave the archive inside the plugin
        except FileNotFoundError:
            pass                    # a racing install already cleaned it up

    # 2. Compile gate — broken archives fail here, not at enable time.
    #    Also require a top-level __init__.py — a tarball with a
    #    version-prefixed top dir (myplugin-1.2.3/…) extracts nothing and
    #    would otherwise install as an empty, "successful" plugin.
    if not os.path.isfile(os.path.join(staging, "__init__.py")):
        shutil.rmtree(staging, ignore_errors=True)
        return False, "archive has no top-level __init__.py — check the tarball layout"
    for dirpath, _dirs, files in os.walk(staging):
        for f in files:
            if f.endswith(".py"):
                try:
                    py_compile.compile(os.path.join(dirpath, f), doraise=True)
                except py_compile.PyCompileError as e:
                    shutil.rmtree(staging, ignore_errors=True)
                    return False, f"plugin does not compile: {e}"

    # 3. Swap into place — only now touch the live install (upgrade
    #    path: disable old, move it aside, replace with the staged tree).
    if os.path.isdir(target_dir):
        disable(name)
        shutil.rmtree(target_dir, ignore_errors=True)
    try:
        os.replace(staging, target_dir)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        return False, "could not move plugin into place"

    # Mark the EXACT cached candidate as installed (multiple versions of
    # the same plugin can coexist in the cache — never flag them all).
    # Re-resolve AFTER disable()/rmtree: they reload + rewrite the cache,
    # so the original `cand` reference may be stale (same trap as
    # remove_source).
    fresh = _find_source(source_id)
    entry = None
    if fresh:
        entry = next((p for p in fresh.get("plugins", [])
                      if p.get("name") == name and p.get("version") == version), None)
    if entry is None:
        return False, "cached entry vanished during install"
    entry["installed"] = True
    entry["enabled"] = False          # deny-by-default: install ≠ enable
    _save_sources()
    return True, f"plugin '{name}' v{version} installed"


def uninstall(name: str) -> Tuple[bool, str]:
    """Disable + delete the plugin directory + clear provenance."""
    with _install_lock:
        return _uninstall_locked(name)


def _uninstall_locked(name: str) -> Tuple[bool, str]:
    if not _PLUGIN_NAME_RE.fullmatch(name or ""):
        return False, "invalid plugin name"
    disable(name)
    target_dir = _plugin_dir(name)
    if os.path.isdir(target_dir):
        shutil.rmtree(target_dir, ignore_errors=True)
    for s in _load_sources()["sources"]:
        for pl in s.get("plugins", []):
            if pl.get("name") == name:
                pl["installed"] = False
                pl["enabled"] = False
    _save_sources()
    return True, f"plugin '{name}' uninstalled"


def get_available(source_id: str) -> List[Dict[str, Any]]:
    """Cached scan results for a source (empty if never scanned)."""
    src = _find_source(source_id)
    return list(src.get("plugins", [])) if src else []
