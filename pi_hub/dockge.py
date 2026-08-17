"""pi_hub.dockge — Dockge stack listing with filesystem+HTTP fallback.

PERF B12
    Filesystem scans are cached on ``stacks_dir`` st_mtime; re-scan only
    on change.  HTTP fallback has a 15 s positive TTL and a 30 s negative
    TTL so an unreachable Dockge instance does not cost 3 s per poll.
    Both caches are lock-guarded.

Public API
    dockge_enabled() → bool
    list_stacks()    → list[dict]   (each dict: ``{"name", "status"}``)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path

from .config import get_config

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_STACKS_DIR = "/opt/stacks"
DEFAULT_BASE_URL = "http://127.0.0.1:5001"
HTTP_TTL = 15.0           # positive-cache lifetime (seconds)
HTTP_NEGATIVE_TTL = 30.0  # negative-cache lifetime  (seconds)
HTTP_TIMEOUT = 3.0        # connect + read timeout

# ── Shared cache ──────────────────────────────────────────────────────────────

_cache_lock = threading.Lock()

# -- filesystem --
_fs_cache: list[dict] | None = None   # cached listing (None = uninitialised)
_fs_mtime: float = 0.0                # st_mtime that produced _fs_cache

# -- HTTP --
_http_cache: list[dict] | None = None  # cached listing (None = uninitialised)
_http_expires: float = 0.0             # monotonic deadline for _http_cache

# -- startup log once --
_startup_logged: bool = False


# ── Public helpers ────────────────────────────────────────────────────────────


def dockge_enabled() -> bool:
    """Return ``True`` when Dockge *could* produce a listing.

    Respects an explicit ``dockge.enabled`` in config.json (including
    ``false`` to force-disable).  Otherwise returns ``True`` when either
    ``stacks_dir`` exists or ``base_url`` is configured.
    """
    cfg = get_config()
    dockge: dict = cfg.get("dockge", {}) or {}

    # Explicit toggle
    if dockge.get("enabled") is False:
        return False
    if dockge.get("enabled") is True:
        return True

    # Auto-detect
    stacks_dir: str = dockge.get("stacks_dir", DEFAULT_STACKS_DIR)
    base_url: str = dockge.get("base_url", "")
    return os.path.isdir(stacks_dir) or bool(base_url)


def list_stacks() -> list[dict]:
    """Return Dockge stacks, filesystem first, HTTP as fallback.

    Each dict has keys ``name`` (str) and ``status`` (str, e.g.
    ``"managed"``, ``"running"``, ``"exited"``).
    """
    _log_startup()

    cfg = get_config()
    dockge: dict = cfg.get("dockge", {}) or {}
    stacks_dir: str = dockge.get("stacks_dir", DEFAULT_STACKS_DIR)
    base_url: str = dockge.get("base_url", DEFAULT_BASE_URL)

    # 1. Filesystem scan -------------------------------------------------------
    fs_result = _scan_filesystem(stacks_dir)
    if fs_result:
        # Non-empty result — short-circuit, no HTTP needed
        return fs_result

    # 2. HTTP fallback (even when fs_result is an empty list) ------------------
    http_result = _fetch_http(base_url)
    if http_result is not None:
        return http_result

    # 3. Neither path produced stacks — return whatever fs gave (likely []) ----
    return fs_result if fs_result is not None else []


# ── Internal: startup logging ─────────────────────────────────────────────────


def _log_startup() -> None:
    """Log once which Dockge path is configured."""
    global _startup_logged
    if _startup_logged:
        return
    _startup_logged = True

    cfg = get_config()
    dockge: dict = cfg.get("dockge", {}) or {}
    stacks_dir: str = dockge.get("stacks_dir", DEFAULT_STACKS_DIR)
    base_url: str = dockge.get("base_url", "")

    fs_available = os.path.isdir(stacks_dir)

    if fs_available:
        log.info("dockge: scanning stacks_dir=%s", stacks_dir)
    elif base_url:
        log.info("dockge: HTTP fallback to %s/api/stacks", base_url)
    else:
        log.info("dockge: not configured (no stacks_dir, no base_url)")


# ── Internal: filesystem scan ─────────────────────────────────────────────────


def _scan_filesystem(stacks_dir: str) -> list[dict] | None:
    """Scan *stacks_dir* for directories containing ``compose.y*ml``.

    Returns
    -------
    list[dict] or None
        ``None`` means the directory is absent / unreadable.
        An empty list means the directory exists but contains no compose
        directories.
    """
    global _fs_cache, _fs_mtime

    # Stat the directory -------------------------------------------------------
    try:
        st = os.stat(stacks_dir)
        mtime = st.st_mtime
    except OSError:
        return None  # absent — no cache possible

    # Fast path: mtime unchanged → return cached copy --------------------------
    with _cache_lock:
        if _fs_cache is not None and mtime == _fs_mtime:
            return list(_fs_cache)

    # Re-scan ------------------------------------------------------------------
    out: list[dict] = []
    try:
        for entry in Path(stacks_dir).iterdir():
            if entry.is_dir() and any(entry.glob("compose.y*ml")):
                out.append({"name": entry.name, "status": "managed"})
    except OSError:
        pass  # transient read error — treat as empty

    with _cache_lock:
        _fs_cache = out
        _fs_mtime = mtime

    return out


# ── Internal: HTTP fallback ───────────────────────────────────────────────────


def _fetch_http(base_url: str) -> list[dict] | None:
    """Fetch stacks from the Dockge REST API.

    Returns
    -------
    list[dict] or None
        ``None`` means the request failed / timed out (negative-cached for
        ``HTTP_NEGATIVE_TTL`` seconds).  An empty list means the API
        returned zero stacks.
    """
    global _http_cache, _http_expires

    now = time.monotonic()

    # Fast path: cached (positive or negative) ---------------------------------
    with _cache_lock:
        if _http_cache is not None and now < _http_expires:
            return list(_http_cache)

    # Fetch --------------------------------------------------------------------
    try:
        req = urllib.request.Request(
            f"{base_url}/api/stacks",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())

        # Normalise to a list of {name, status}
        items: list = data if isinstance(data, list) else data.get("stacks", [])
        out: list[dict] = [
            {
                "name": it.get("name", "?"),
                "status": (it.get("status") or "unknown").lower(),
            }
            for it in items
        ]

        with _cache_lock:
            _http_cache = out
            _http_expires = now + HTTP_TTL

        return out

    except Exception:
        # Negative-cache the failure so absent Dockge doesn't cost 3 s/poll
        with _cache_lock:
            _http_cache = []          # empty list = "tried, got nothing"
            _http_expires = now + HTTP_NEGATIVE_TTL

        return None
