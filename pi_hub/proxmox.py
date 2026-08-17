"""pi_hub.proxmox — Proxmox container management via HTTP API.

Replaces the SSH-based ``pct`` approach with direct Proxmox API calls.
Uses :mod:`pi_hub.config` for configuration and token resolution (S2).
Implements lock-guarded caching with exponential backoff on failure (B8),
keyed PER Proxmox INSTANCE (v7 multi-instance).

Security
--------
S2
    Token is resolved via ``config.proxmox_token(instance_id)`` — it is
    **never** read from a plain config dict.  An assertion guard ensures
    the token does not appear in **any** return value.
S4
    ``vmid.isdigit()`` + action allowlist (start / stop / shutdown / reboot).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import ssl
import threading
import time
import urllib.request
from typing import Any, Dict, Optional

from pi_hub.config import has_proxmox, proxmox_host, proxmox_instances, \
    proxmox_node, proxmox_token

# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

REQUEST_TIMEOUT: float = 4.0        # seconds (B8)
SUCCESS_TTL: float = 5.0            # seconds — positive-cache lifetime
ALLOWED_ACTIONS = frozenset({"start", "stop", "shutdown", "reboot"})  # S4

# Exponential backoff sequence for the shared negative cache (seconds).
# Capped at the last element (60 s).
_BACKOFF_SEQUENCE: tuple[float, ...] = (5.0, 10.0, 30.0, 60.0)


# ═══════════════════════════════════════════════════════════════════════════════
# SSL context — self-signed LAN certificate with optional pinning
# ═══════════════════════════════════════════════════════════════════════════════

# Optional pin: SHA-256 fingerprint of the Proxmox TLS certificate
# (hex, colons optional).  When set, the peer certificate MUST match —
# MITM/ARP-spoofing can otherwise read the API token in cleartext.
# Source: env PROXMOX_CERT_FINGERPRINT, else config proxmox[].cert_fingerprint.
_fingerprint_cache: Dict[str, str] = {}


def _pinned_fingerprint(instance_id: str = "") -> str:
    """Return the configured cert fingerprint for *instance_id* (or '')."""
    env = os.environ.get("PROXMOX_CERT_FINGERPRINT", "").strip().lower()
    if env:
        return env.replace(":", "")
    if instance_id not in _fingerprint_cache:
        fp = ""
        try:
            from pi_hub.config import get_config
            pm = get_config().get("proxmox", [])
            if isinstance(pm, list):
                for i in pm:
                    if i.get("id") == instance_id:
                        fp = str(i.get("cert_fingerprint") or "").strip().lower()
                        break
            elif isinstance(pm, dict):
                fp = str(pm.get("cert_fingerprint") or "").strip().lower()
        except Exception:
            fp = ""
        _fingerprint_cache[instance_id] = fp.replace(":", "")
    return _fingerprint_cache[instance_id]


def _ssl_context(instance_id: str = "") -> ssl.SSLContext:
    """SSL context for Proxmox API calls.

    With a configured fingerprint, the real pin check happens in
    :class:`_PinnedHTTPSConnection`; this context is then unused for the
    handshake (see :func:`_open`).  Without a fingerprint, the default
    verified context (hostname + CA chain) is used — self-signed LAN
    certs fail loudly instead of silently downgrading (no more CERT_NONE).
    """
    if _pinned_fingerprint(instance_id):
        return _PinnedContext(instance_id)
    return ssl.create_default_context()


class _PinnedContext:
    """Marker context: signals :func:`_open` to use pinning."""

    def __init__(self, instance_id: str):
        self.instance_id = instance_id


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that pins the peer certificate's SHA-256.

    ``connect()`` performs the TLS handshake with verification disabled,
    then compares the presented certificate's fingerprint against the
    configured pin and refuses to proceed on mismatch.  This makes
    ARP/DNS-spoofing useless even with a self-signed LAN cert.
    """

    def __init__(self, host, port=None, timeout=None, fingerprint: str = ""):
        super().__init__(host, port, timeout=timeout)
        self._pin = fingerprint

    def connect(self):
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        tls.check_hostname = False
        tls.verify_mode = ssl.CERT_NONE
        sock = socket.create_connection((self.host, self.port), self.timeout)
        try:
            self.sock = tls.wrap_socket(sock, server_hostname=self.host)
            der = self.sock.getpeercert(binary_form=True)
            if not der:
                raise ssl.SSLError("no peer certificate presented")
            digest = hashlib.sha256(der).hexdigest()
            if digest != self._pin:
                raise ssl.SSLError(
                    f"certificate fingerprint mismatch "
                    f"(expected {self._pin[:16]}…, got {digest[:16]}…)")
        except Exception:
            sock.close()
            raise


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """URL-opener handler that routes to pinned connections."""

    def __init__(self, fingerprint: str):
        super().__init__()
        self._pin = fingerprint

    def https_open(self, req):
        from urllib.parse import urlsplit
        parts = urlsplit(req.full_url)
        host = parts.hostname
        port = parts.port or 8006
        # do_open calls http_class(host) — bind everything else via
        # closure so the pinned connection is constructed exactly once.
        def make_conn(h=None, fp=self._pin, **kw):
            return _PinnedHTTPSConnection(host, port, timeout=8.0,
                                          fingerprint=fp)
        return self.do_open(make_conn, req)


_opener_cache: Dict[str, urllib.request.OpenerDirector] = {}


def _open(url: str, token: str, timeout: float, instance_id: str = "",
          data: Optional[bytes] = None) -> Any:
    """Open *url* with the right TLS mode (pinned / verified)."""
    fp = _pinned_fingerprint(instance_id)
    headers = {"Authorization": token}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    if fp:
        if fp not in _opener_cache:
            _opener_cache[fp] = urllib.request.build_opener(
                _PinnedHTTPSHandler(fp))
        return _opener_cache[fp].open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout,
                                  context=ssl.create_default_context())


# ═══════════════════════════════════════════════════════════════════════════════
# Cache (B8) — lock-guarded, module-level, keyed per instance id (v7)
# ═══════════════════════════════════════════════════════════════════════════════

_cache_lock = threading.Lock()

# Positive cache: last successful container listing, per instance id.
_containers_cache: Dict[str, Dict[str, Any]] = {}
_containers_cache_ts: Dict[str, float] = {}

# Negative cache: per instance id, exponential backoff on repeated failure
# so we don't re-dial a dead host.
_failure_count: Dict[str, int] = {}
_failure_ts: Dict[str, float] = {}
_failure_error: Dict[str, str] = {}


def _current_backoff(instance_id: str) -> float:
    """Backoff duration for the current consecutive-failure count."""
    idx = _failure_count.get(instance_id, 0) - 1  # 0-based index into _BACKOFF_SEQUENCE
    if idx < 0:
        return 0.0
    return _BACKOFF_SEQUENCE[min(idx, len(_BACKOFF_SEQUENCE) - 1)]


def _cached_error(instance_id: str) -> Optional[Dict[str, Any]]:
    """Return the cached error response if still in backoff, else *None*.

    Caller must hold ``_cache_lock``.
    """
    if _failure_count.get(instance_id, 0) == 0:
        return None
    if (time.monotonic() - _failure_ts.get(instance_id, 0.0)) < _current_backoff(instance_id):
        return {"success": False, "error": _failure_error.get(instance_id, "Proxmox unreachable")}
    return None


def _record_success(instance_id: str) -> None:
    """Clear the negative cache after a successful API call.

    Caller must hold ``_cache_lock``.
    """
    _failure_count.pop(instance_id, None)
    _failure_ts.pop(instance_id, None)
    _failure_error.pop(instance_id, None)


def _record_failure(instance_id: str, err_msg: str) -> None:
    """Record a failure and bump the backoff counter.

    Caller must hold ``_cache_lock``.
    """
    _failure_count[instance_id] = _failure_count.get(instance_id, 0) + 1
    _failure_ts[instance_id] = time.monotonic()
    _failure_error[instance_id] = err_msg


# ═══════════════════════════════════════════════════════════════════════════════
# Token safety (S2)
# ═══════════════════════════════════════════════════════════════════════════════

def _assert_token_not_leaked(data: Any, token: str) -> None:
    """Crash if *token* appears anywhere in *data*.

    This is a hard invariant — a leak means we accidentally serialized the
    token into a user-facing response.  Deliberately NOT an ``assert``:
    ``python -O`` strips assertions, and an invariant that disappears under
    an interpreter flag is not an invariant.
    """
    if not token:
        return
    payload = data if isinstance(data, str) else json.dumps(data)
    if token in payload:
        raise RuntimeError("SECURITY: Proxmox token leaked in return value")


# ═══════════════════════════════════════════════════════════════════════════════
# Low-level HTTP helpers (per instance, v7)
# ═══════════════════════════════════════════════════════════════════════════════

def _friendly(exc: Exception, host: str) -> str:
    """Turn a connection error into a message that names the real host."""
    err = str(exc)
    if any(kw in err for kw in ("timed out", "Name or service", "Connection",
                                "refused", "getaddrinfo", "Network is unreachable")):
        return f"Proxmox unreachable — no answer from {host or 'the configured host'}"
    return err


def _api_get(instance_id: str, path: str) -> Dict[str, Any]:
    """GET *path* from the Proxmox JSON API of *instance_id*.

    Raises :class:`RuntimeError` with a user-friendly message on failure.
    """
    host = proxmox_host(instance_id)
    token = proxmox_token(instance_id)
    url = f"https://{host}:8006{path}"
    req = urllib.request.Request(url, headers={"Authorization": token})
    try:
        with _open(url, token, REQUEST_TIMEOUT, instance_id) as r:
            return json.loads(r.read().decode())
    except Exception as exc:
        raise RuntimeError(_friendly(exc, host)) from exc


def _api_post(instance_id: str, path: str) -> Dict[str, Any]:
    """POST to *path* on the Proxmox JSON API of *instance_id*.

    Raises :class:`RuntimeError` with a user-friendly message on failure.
    """
    host = proxmox_host(instance_id)
    token = proxmox_token(instance_id)
    url = f"https://{host}:8006{path}"
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={"Authorization": token, "Content-Type": "application/json"},
    )
    try:
        with _open(url, token, REQUEST_TIMEOUT, instance_id, data=b"{}") as r:
            return json.loads(r.read().decode())
    except Exception as exc:
        raise RuntimeError(_friendly(exc, host)) from exc


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_instance(instance_id: str) -> Dict[str, Any]:
    """Fetch the container list for ONE instance (cache + backoff included).

    Returns ``{"success": True, "containers": [...]}`` or an error dict —
    never raises.
    """
    if not has_proxmox(instance_id):
        return {"success": False, "error": "Proxmox disabled in config"}

    token = proxmox_token(instance_id)
    if not token:
        return {"success": False, "error": "Missing Proxmox API token"}

    node = proxmox_node(instance_id)
    if not node:
        # Fail loudly: an unknown node returns an EMPTY list from the API,
        # not an error, so guessing here would show "0 containers" and look
        # perfectly healthy.
        return {"success": False,
                "error": f"instance '{instance_id or 'default'}': no node name "
                         "configured — set \"node\" to the Proxmox node"}

    # ── Cache checks (inside lock) ──
    with _cache_lock:
        # Negative cache: skip the HTTP call while in backoff.
        cached = _cached_error(instance_id)
        if cached is not None:
            _assert_token_not_leaked(cached, token)
            return cached

        # Positive cache: return fresh-enough container list.
        ts = _containers_cache_ts.get(instance_id, 0.0)
        if instance_id in _containers_cache and (time.monotonic() - ts) < SUCCESS_TTL:
            result = _containers_cache[instance_id].copy()
            _assert_token_not_leaked(result, token)
            return result

    # ── Fetch (no lock during I/O) ──
    try:
        node = proxmox_node(instance_id)
        data = _api_get(instance_id, f"/api2/json/nodes/{node}/lxc")
        containers = [
            {
                "vmid": str(ct.get("vmid", "?")),
                "name": ct.get("name", ct.get("hostname", "?")),
                "status": ct.get("status", "unknown"),
                "cpu": ct.get("cpu", 0),
                "mem": ct.get("mem", 0),
                "maxmem": ct.get("maxmem", 0),
                # PVE delivers tags as a semicolon-separated string (v7)
                "tags": [t for t in str(ct.get("tags", "")).split(";") if t],
                "instance": instance_id,
            }
            for ct in data.get("data", [])
        ]
        # Numeric vmid sort (non-digit VMIDs sort to the end).
        containers.sort(key=lambda c: int(c["vmid"]) if c["vmid"].isdigit() else 999)

        result: Dict[str, Any] = {"success": True, "containers": containers}

        with _cache_lock:
            _containers_cache[instance_id] = result
            _containers_cache_ts[instance_id] = time.monotonic()
            _record_success(instance_id)
    except Exception as exc:
        result = {"success": False, "error": str(exc)}
        with _cache_lock:
            _record_failure(instance_id, str(exc))

    _assert_token_not_leaked(result, token)
    return result


def fetch_proxmox_containers(instance_id: str = "") -> Dict[str, Any]:
    """Fetch the container list from one or ALL configured instances.

    ``instance_id == ""`` → merge every configured instance (each container
    record carries its ``instance``).  Positive cache per instance reused
    for *SUCCESS_TTL* seconds; per-instance negative cache with backoff.

    Returns
    -------
    dict
        ``{"success": True, "containers": [{vmid, name, status, cpu, mem,
        maxmem, tags, instance}, ...]}`` or ``{"success": False, ...}``.
    """
    if instance_id:
        return _fetch_instance(instance_id)

    instances = proxmox_instances()
    if not instances:
        return {"success": False, "error": "Proxmox disabled in config"}

    # Merge all instances; per-instance failures are surfaced as entries.
    merged: Dict[str, Any] = {"success": True, "containers": [], "errors": []}
    for inst in instances:
        iid = str(inst.get("id", ""))
        res = _fetch_instance(iid)
        if res.get("success"):
            merged["containers"].extend(res["containers"])
        else:
            merged["errors"].append({"instance": iid, "error": res.get("error", "")})
    merged["containers"].sort(
        key=lambda c: (str(c.get("instance", "")), int(c["vmid"]) if c["vmid"].isdigit() else 999)
    )
    if merged["errors"] and not merged["containers"]:
        merged["success"] = False
        merged["error"] = "; ".join(e["error"] for e in merged["errors"])
    return merged


def pve_action(vmid: str, action: str, instance_id: str = "") -> Dict[str, Any]:
    """Send a container action via the Proxmox API.

    POST ``/nodes/{node}/lxc/{vmid}/status/{action}`` on *instance_id*
    ('' → default instance).

    Parameters
    ----------
    vmid:
        Container VMID — must be digits only (S4).
    action:
        One of ``start``, ``stop``, ``shutdown``, ``reboot`` (S4).
    instance_id:
        Proxmox instance id; '' resolves the default instance.  An unknown
        explicit id → error (never silently acts on the default instance).

    Returns
    -------
    dict
        ``{"success": True, "message": "Container 100 start"}``
        or ``{"success": False, "error": "…"}``.
    """
    if not has_proxmox(instance_id):
        return {"success": False, "error": "Proxmox disabled in config"}
    if instance_id and not any(
        p.get("id") == instance_id for p in proxmox_instances()
    ):
        return {"success": False, "error": f"Unknown Proxmox instance: {instance_id}"}

    # ── S4: strict validation ──
    if not vmid.isdigit() or action not in ALLOWED_ACTIONS:
        return {"success": False, "error": f"Invalid container action: {vmid}/{action}"}

    token = proxmox_token(instance_id)
    if not token:
        return {"success": False, "error": "Missing Proxmox API token"}

    node = proxmox_node(instance_id)
    if not node:
        return {"success": False,
                "error": f"instance '{instance_id or 'default'}': no node name "
                         "configured — set \"node\" to the Proxmox node"}

    # NOTE: the read-path backoff is deliberately NOT consulted here.  A
    # container action is an explicit click, and refusing it because a
    # status poll failed a moment ago is exactly wrong when someone is
    # trying to restart a stuck container.

    # ── Execute (no lock during I/O) ──
    try:
        data = _api_post(instance_id, f"/api2/json/nodes/{node}/lxc/{vmid}/status/{action}")
        if data.get("data"):
            result: Dict[str, Any] = {
                "success": True,
                "message": f"Container {vmid} {action}",
            }
        else:
            result = {"success": False, "error": "API returned no data"}
        with _cache_lock:
            _record_success(instance_id)
    except Exception as exc:
        result = {"success": False, "error": str(exc)}
        with _cache_lock:
            _record_failure(instance_id, str(exc))

    _assert_token_not_leaked(result, token)
    return result
