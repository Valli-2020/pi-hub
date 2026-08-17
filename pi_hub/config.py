"""pi_hub.config — v5 secure config loader.

Reads config.json from the repo root (next to this package).
Every consumer must call get_config() / get_hosts() — no module-level
CFG snapshot.  Reloads are cheap: st_mtime-based, cached, at most once
per second.  public_config() is the ONLY dict that /api/config may
serve (no tokens, MACs, SSH credentials).
"""

import json
import os
import threading
import time
import warnings
from typing import Any, Dict, List

# ── Paths ────────────────────────────────────────────────────────────────────
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PKG_DIR)  # pi_hub/..  = repo root
CONFIG_PATH = os.path.join(_REPO_ROOT, "config.json")
SECRETS_PATH = os.path.join(_REPO_ROOT, "secrets.json")

# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_PORT = 8898
DEFAULT_PROXMOX_NODE = "keller"
DEFAULT_PROXMOX_SSH_USER = "root"

Host = Dict[str, Any]

# ── Cached config (mtime-based, thread-safe) ────────────────────────────────
_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {}
_cache_mtime: float = 0.0
_cache_ts: float = 0.0  # last parse time (monotonic), for 1 Hz throttle
_MIN_RELOAD_INTERVAL = 1.0  # seconds

# ── Plugin config edit lock — serializes plugin config.json writes ───────────
_config_edit_lock = threading.Lock()

CONFIG_VERSION = 7


def _normalize(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """v7 migration + invariants, applied INSIDE the cache lock (never on the
    returned dict — it is the live shared object).

    - legacy v6 ``proxmox`` object ``{host, node, ...}`` → instance list
      ``[{id: <node>, ...}]``; marks ``_migrated_v6_proxmox`` so the first
      save writes a ``config.json.v6.bak`` and persists the new shape.
    - stamps ``config_version`` so rollbacks are detectable.
    """
    pm = cfg.get("proxmox")
    if isinstance(pm, dict) and pm.get("host"):
        node = str(pm.get("node") or DEFAULT_PROXMOX_NODE)
        # keep the legacy `token` key: proxmox_token() still resolves it as
        # the deprecated config fallback (v6 installs without secrets.json
        # store the token here). It never leaves the server: public_config /
        # admin_config use positive allowlists that exclude it.
        inst = dict(pm)
        inst.setdefault("id", node)
        cfg["proxmox"] = [inst]
        cfg["_migrated_v6_proxmox"] = True
    elif not isinstance(pm, list):
        cfg["proxmox"] = []
    cfg.setdefault("config_version", CONFIG_VERSION)
    return cfg


def load_config() -> Dict[str, Any]:
    """Load config.json; fall back to an empty config if missing/invalid."""
    try:
        with open(CONFIG_PATH) as f:
            return _normalize(json.load(f))
    except FileNotFoundError:
        return {"port": DEFAULT_PORT, "hosts": [], "proxmox": [],
                "config_version": CONFIG_VERSION}
    except Exception:
        # CORRUPT config → fail CLOSED: force auth on so the guard 503s
        return {"port": DEFAULT_PORT, "hosts": [], "proxmox": [],
                "config_version": CONFIG_VERSION,
                "auth": {"enabled": True}, "_load_error": True}


def get_config() -> Dict[str, Any]:
    """Return the current config dict (mtime-cached, thread-safe).

    Re-parses config.json only when its st_mtime changed, at most once
    per second.  Every other module must call this (or get_hosts())
    instead of caching a module-level snapshot.
    """
    global _cache, _cache_mtime, _cache_ts

    try:
        st = os.stat(CONFIG_PATH)
        mtime = st.st_mtime
    except OSError:
        mtime = 0.0

    now = time.monotonic()

    # Fast path: mtime hasn't changed, return cached
    if _cache and mtime == _cache_mtime:
        return _cache

    with _cache_lock:
        # Double-check inside the lock
        if _cache and mtime == _cache_mtime:
            return _cache

        # Throttle: never re-parse more than once per second
        elapsed = now - _cache_ts
        if _cache and elapsed < _MIN_RELOAD_INTERVAL:
            return _cache

        # Reload — normalization runs here, under the lock (v7: #10)
        _cache = load_config()
        _cache_mtime = mtime
        _cache_ts = time.monotonic()
        return _cache


def get_hosts() -> List[Host]:
    """Return the current host list (reads through get_config)."""
    return get_config().get("hosts", [])


def save_config(cfg: Dict[str, Any]) -> bool:
    """Atomically write config.json (0600) and invalidate the mtime cache.

    Used by the admin config routes.  On the first migrating save (legacy
    v6 proxmox shape seen) a timestamped backup of the OLD file is kept
    as ``config.json.v6.bak`` so a rollback to the v6 binary is possible.
    Returns False on I/O error.
    """
    migrated = bool(cfg.pop("_migrated_v6_proxmox", False))
    try:
        if migrated and not os.path.exists(CONFIG_PATH + ".v6.bak") and \
                os.path.exists(CONFIG_PATH):
            # copy the CURRENT (pre-write) v6 file before replacing it
            with open(CONFIG_PATH, "rb") as src, open(CONFIG_PATH + ".v6.bak", "wb") as dst:
                dst.write(src.read())
        tmp = CONFIG_PATH + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        return False
    global _cache, _cache_mtime
    with _cache_lock:
        _cache = {}
        _cache_mtime = 0.0
        # NOTE: _cache_ts intentionally left stale — the 1 Hz throttle is
        # gated on `if _cache`, which is now empty, so the next load passes.
    return True


# ── Host lookups ─────────────────────────────────────────────────────────────

def find_host_by_id(host_id: str) -> Host:
    """Return the host dict with the given id, or an empty dict."""
    return next((h for h in get_hosts() if h.get("id") == host_id), {})


def find_host_by_ip(ip: str) -> Host:
    """Return the host dict with the given ip, or an empty dict."""
    return next((h for h in get_hosts() if h.get("ip") == ip), {})


def find_host_by_mac(mac: str) -> Host:
    """Return the host dict with the given MAC (case-insensitive), or an empty dict."""
    mac = mac.lower()
    return next((h for h in get_hosts() if h.get("mac", "").lower() == mac), {})


# ── Proxmox helpers (v7 — multi-instance) ─────────────────────────────────

def proxmox_instances() -> List[Dict[str, Any]]:
    """Return the list of Proxmox instances (normalized by _normalize).

    Each entry: {id, enabled?, host, node?, ssh_user?, api_url?, ...}.
    """
    pm = get_config().get("proxmox", [])
    if isinstance(pm, list):
        return [p for p in pm if isinstance(p, dict)]
    return []


def proxmox_default_instance() -> Dict[str, Any]:
    """Return the first (default) instance, or {}."""
    insts = proxmox_instances()
    return insts[0] if insts else {}


def _instance_by_id(instance_id: str) -> Dict[str, Any]:
    """Resolve an instance id; ''/missing → default instance (never positional)."""
    if not instance_id:
        return proxmox_default_instance()
    return next((p for p in proxmox_instances() if p.get("id") == instance_id), {})


def has_proxmox(instance_id: str = "") -> bool:
    """True if the (default) Proxmox instance is configured and enabled."""
    inst = _instance_by_id(instance_id)
    return bool(inst.get("host") and inst.get("enabled", True))


# Snapshot at import for backward compat with code that reads HAS_PROXMOX
# as a module-level bool.  Internal v7 code should call has_proxmox().
HAS_PROXMOX: bool = has_proxmox()


def proxmox_host(instance_id: str = "") -> str:
    """Return the instance's Proxmox host, or ''."""
    return _instance_by_id(instance_id).get("host", "")


def proxmox_node(instance_id: str = "") -> str:
    """Return the instance's node name (default 'keller')."""
    return _instance_by_id(instance_id).get("node", DEFAULT_PROXMOX_NODE)


def proxmox_ssh_user(instance_id: str = "") -> str:
    """Return the SSH user for Proxmox commands (default 'root')."""
    return _instance_by_id(instance_id).get("ssh_user", DEFAULT_PROXMOX_SSH_USER)


def proxmox_api_url(instance_id: str = "") -> str:
    """Return the instance's Proxmox API URL (explicit or derived from host)."""
    inst = _instance_by_id(instance_id)
    host = inst.get("host", "")
    return inst.get("api_url", "") or (f"https://{host}:8006/api2/json" if host else "")


def proxmox_token(instance_id: str = "") -> str:
    """Resolve the Proxmox API token for *instance_id* (S2 — secure resolution).

    Resolution order (''/default instance):
      1. ``os.environ["PROXMOX_TOKEN"]`` (default instance only — one env var)
      2. ``secrets.json`` → ``{"proxmox_tokens": {<instance id>: "..."}}``
      3. legacy ``secrets.json`` → ``{"proxmox": {"token": "..."}}`` (default
         instance, deprecation warning)
      4. ``config.json`` → ``proxmox[i].token`` (deprecation warning — move
         to secrets.json)
    """
    inst = _instance_by_id(instance_id)
    inst_id = str(inst.get("id", ""))

    # 1. Environment variable (default instance only)
    if not instance_id:
        token = os.environ.get("PROXMOX_TOKEN", "")
        if token:
            return token

    # 2./3. secrets.json
    try:
        st = os.stat(SECRETS_PATH)
        if (st.st_mode & 0o777) != 0o600:
            warnings.warn(
                f"{SECRETS_PATH} should be mode 0600 (current: "
                f"{oct(st.st_mode & 0o777)}) — token may be world-readable"
            )
        with open(SECRETS_PATH) as f:
            secrets = json.load(f)
        tokens = secrets.get("proxmox_tokens", {}) or {}
        token = tokens.get(inst_id, "")
        if token:
            return token
        legacy = secrets.get("proxmox", {}).get("token", "")
        if legacy and not instance_id:
            warnings.warn(
                "secrets.json 'proxmox.token' is deprecated — use "
                "'proxmox_tokens' keyed by instance id",
                DeprecationWarning,
            )
            return legacy
    except FileNotFoundError:
        pass
    except Exception:
        pass  # malformed secrets.json → fall through

    # 4. config.json proxmox[i].token (deprecated)
    token = inst.get("token", "")
    if token:
        warnings.warn(
            "proxmox.token in config.json is deprecated — use the "
            "PROXMOX_TOKEN env var or secrets.json instead",
            DeprecationWarning,
        )
    return token


# ── Public config (S1) ───────────────────────────────────────────────────────

# Fields allowed in each host for public exposure
_PUBLIC_HOST_FIELDS = frozenset({
    "id", "name", "os_label", "type", "icon", "ip",
    "capabilities", "dual_boot_peer",
})

# Fields allowed in each service for public exposure
_PUBLIC_SERVICE_FIELDS = frozenset({"name", "url", "icon", "group",
                                    "host", "source"})

# Admin-only full-config allowlist (v7 #6 — positive fields, never a
# blacklist: anything not listed cannot leak into /api/config/full).
_FULL_HOST_FIELDS = frozenset({
    "id", "name", "os_label", "type", "icon", "ip", "mac",
    "capabilities", "dual_boot_peer", "grub_entries", "ssh_user",
    "ssh_shutdown_cmd",
})
_FULL_SERVICE_FIELDS = frozenset({
    "name", "url", "icon", "group", "status_port", "status_host",
    "health_path", "host", "source",
})
_FULL_INSTANCE_FIELDS = frozenset({
    "id", "enabled", "host", "node", "ssh_user", "api_url",
})


def public_host(host: Host) -> Dict[str, Any]:
    """Return a host dict with only safe fields."""
    return {k: v for k, v in host.items() if k in _PUBLIC_HOST_FIELDS}


def _public_service(svc: Dict[str, Any]) -> Dict[str, Any]:
    """Return a service dict with only safe fields."""
    return {k: v for k, v in svc.items() if k in _PUBLIC_SERVICE_FIELDS}


def public_config() -> Dict[str, Any]:
    """Return the ONLY dict that /api/config may serve (S1).

    Excluded: proxmox tokens (all instances), each host's mac / ssh_user /
              ssh_host / ssh_shutdown_cmd / grub_entries.

    Included: host.ip (frontend renders it in card meta).
    v7 dual-shape: ``proxmox`` keeps emitting the DEFAULT instance's
    {enabled, host} object (frontend compat through v7) AND
    ``proxmox_instances`` lists every instance.  The object key drops in v8.
    """
    cfg = get_config()

    # Hosts: public fields only
    hosts = [public_host(h) for h in cfg.get("hosts", [])]

    # Services: public fields only
    services = [_public_service(s) for s in cfg.get("services", [])]

    # Groups: pass through (labels only)
    groups = cfg.get("groups", {})

    # Dockge: expose only {enabled: bool}
    dockge_raw = cfg.get("dockge", {}) or {}
    dockge = {"enabled": bool(dockge_raw.get("enabled", bool(dockge_raw.get("base_url"))))}

    # Proxmox: default instance {enabled, host} + full instance list — NO tokens
    insts = proxmox_instances()
    default = insts[0] if insts else {}
    proxmox = {
        "enabled": bool(default.get("enabled", bool(default.get("host")))),
        "host": default.get("host", ""),
    }
    proxmox_instances_out = [
        {
            "id": p.get("id", ""),
            "host": p.get("host", ""),
            "enabled": bool(p.get("enabled", bool(p.get("host")))),
        }
        for p in insts
    ]

    return {
        "title": cfg.get("title", "Pi Hub"),
        "hosts": hosts,
        "services": services,
        "groups": groups,
        "dockge": dockge,
        "proxmox": proxmox,
        "proxmox_instances": proxmox_instances_out,
        "scan": {"auto_add": bool((cfg.get("scan") or {}).get("auto_add", False))},
    }


# ── Admin-only full config (v7 #6) ──────────────────────────────────────────

def proxmox_token_info(instance_id: str = "") -> dict:
    """Return ``{'set': bool, 'source': ...}`` for an instance's token.

    Source is one of 'env' | 'secrets' | 'secrets-legacy' | 'config' |
    'none'.  The token VALUE itself is never returned.
    """
    inst = _instance_by_id(instance_id)
    inst_id = str(inst.get("id", ""))
    if not instance_id and os.environ.get("PROXMOX_TOKEN"):
        return {"set": True, "source": "env"}
    try:
        with open(SECRETS_PATH) as f:
            secrets = json.load(f)
        if (secrets.get("proxmox_tokens", {}) or {}).get(inst_id):
            return {"set": True, "source": "secrets"}
        if not instance_id and (secrets.get("proxmox", {}) or {}).get("token"):
            return {"set": True, "source": "secrets-legacy"}
    except Exception:
        pass
    if inst.get("token"):
        return {"set": True, "source": "config"}
    return {"set": False, "source": "none"}


_secrets_lock = threading.Lock()   # serializes secrets.json read-modify-write (#7)


def save_proxmox_tokens(tokens: dict, drop: list | None = None) -> bool:
    """Set non-empty *tokens* ({instance_id: token}) in secrets.json.

    - The legacy ``proxmox.token`` value is migrated into
      ``proxmox_tokens[<default instance id>]`` UNCONDITIONALLY (v7 #7) —
      not only when an admin happens to type a token — so the deprecated
      read path dies with the first v7 save.
    - *drop* lists instance ids whose stored tokens are removed (orphan
      pruning when an instance is deleted — v7 #8).
    - Serialized with ``_secrets_lock`` (concurrent admin saves can no
      longer lose a token).  Returns False on I/O error.
    """
    with _secrets_lock:
        try:
            secrets: Dict[str, Any] = {}
            if os.path.exists(SECRETS_PATH):
                with open(SECRETS_PATH) as f:
                    secrets = json.load(f)
            if not isinstance(secrets, dict):
                secrets = {}
            pt = dict(secrets.get("proxmox_tokens", {}) or {})
            changed = False
            for k, v in tokens.items():
                if v and pt.get(k) != v:
                    pt[k] = v
                    changed = True
            for k in (drop or []):
                if pt.pop(k, None) is not None:
                    changed = True
            legacy = (secrets.get("proxmox", {}) or {}).get("token", "")
            default_id = proxmox_default_instance().get("id", "")
            if legacy and default_id and default_id not in pt:
                pt[default_id] = legacy
                changed = True
            if not changed:
                return True
            secrets["proxmox_tokens"] = pt
            secrets.pop("proxmox", None)        # legacy key consumed by migration
            tmp = SECRETS_PATH + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(secrets, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, SECRETS_PATH)
        except OSError:
            return False
    return True


def admin_config() -> Dict[str, Any]:
    """Admin-only full config for /api/config/full (v7 #6).

    Allowlist-filtered (tokens are replaced by {set, source} info — the
    values never leave the server).
    """
    cfg = get_config()
    hosts = [{k: h[k] for k in _FULL_HOST_FIELDS if k in h}
             for h in cfg.get("hosts", [])]
    services = [{k: s[k] for k in _FULL_SERVICE_FIELDS if k in s}
                for s in cfg.get("services", [])]
    insts = []
    for p in proxmox_instances():
        inst = {k: p[k] for k in _FULL_INSTANCE_FIELDS if k in p}
        inst["token"] = proxmox_token_info(str(p.get("id", "")))
        insts.append(inst)
    dockge_raw = cfg.get("dockge", {}) or {}
    return {
        "title": cfg.get("title", "Pi Hub"),
        "hosts": hosts,
        "services": services,
        "groups": cfg.get("groups", {}),
        # base_url deliberately omitted (v7 #11): it may carry credentials
        "dockge": {
            "enabled": bool(dockge_raw.get("enabled", bool(dockge_raw.get("base_url")))),
        },
        "proxmox_instances": insts,
        "scan": {"auto_add": bool((cfg.get("scan") or {}).get("auto_add", False))},
    }


# ── Auth config (v6) ─────────────────────────────────────────────────────────

def auth_config() -> Dict[str, Any]:
    """Return the auth sub-dict (default: disabled — backward compatible)."""
    return get_config().get("auth", {}) or {}


def auth_enabled() -> bool:
    """True when user/password auth is required for the API."""
    return bool(auth_config().get("enabled", False))
