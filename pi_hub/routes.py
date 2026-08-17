"""pi_hub.routes — HTTP API dispatch against the frozen CONTRACT.md.

Every handler returns a (data, status_code) tuple; pi_hub/server.py
serialises it as JSON.  No handler reads raw query-string parameters
for actions —all routing is path-segment based (V2 style).
"""

from __future__ import annotations

import json as _json
import ipaddress as _ipaddress
import os as _os
import re as _re
import threading as _threading
from copy import deepcopy as _deepcopy
from typing import Any, Dict, List, Tuple

from pi_hub import auth
from pi_hub import config
from pi_hub import dockge
from pi_hub import hosts
from pi_hub import net
from pi_hub import proxmox
from pi_hub import scanner
from pi_hub import tasks
from pi_hub import updater

# ── Paths ────────────────────────────────────────────────────────────────────
_PKG_DIR = _os.path.dirname(_os.path.abspath(__file__))
_WEB_DIR = _os.path.join(_os.path.dirname(_PKG_DIR), "web")

# ── Type aliases (compatible with root routes.py callers) ────────────────────
Params = Dict[str, List[str]]
Response = Tuple[Any, int]

# ── Icons.json preload (lazy, cached) ────────────────────────────────────────
_icons_cache: Dict[str, Any] | None = None


def _load_icons() -> Dict[str, Any]:
    """Load icons.json from web/ — cached after first read."""
    global _icons_cache
    if _icons_cache is not None:
        return _icons_cache  # type: ignore[return-type]
    try:
        with open(_os.path.join(_WEB_DIR, "icons.json"), "rb") as f:
            _icons_cache = _json.loads(f.read())
    except Exception:
        _icons_cache = {}
    return _icons_cache


# ═══════════════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════════════


def _check_services() -> Dict[str, str]:
    """Probe every configured service — HTTP GET first, TCP fallback.

    Results cached 8 s so the 10 s poll issues one probe set.
    Returns ``{service_name: "online"|"offline"|"unknown"}``.
    """
    import time as _time, urllib.request as _urllib

    services = config.get_config().get("services", [])
    if not services:
        return {}

    now = _time.monotonic()
    if _check_services._cache_time and (now - _check_services._cache_time) < 8:
        return _check_services._cache

    def _probe(svc: Dict[str, Any]) -> Tuple[str, str]:
        name = svc.get("name", "")
        url = svc.get("url", "")

        # HTTP probe first
        if url and url.startswith("http"):
            health_path = svc.get("health_path", "/")
            probe_url = url.rstrip("/") + "/" + health_path.lstrip("/")
            try:
                req = _urllib.Request(probe_url, method="GET")
                with _urllib.urlopen(req, timeout=3) as resp:
                    if 200 <= resp.status < 400 or resp.status in (401, 403):
                        return (name, "online")
            except Exception:
                pass

        # TCP fallback for non-HTTP services (shared endpoint parse, #21)
        host, port = scanner.service_endpoint(svc)
        if host and port:
            return (name, "online" if net.tcp_ping(host, port) else "offline")
        return (name, "unknown")

    results = net.parallel_map(_probe, services, timeout=4.0)
    out = {name: status for r in (results or []) if r for name, status in [r]}
    _check_services._cache = out
    _check_services._cache_time = now
    return out

_check_services._cache: Dict[str, str] = {}
_check_services._cache_time = 0.0



def _handle_wake(host_id: str) -> Response:
    """WOL broadcast for *host_id*.  Validates against config registry first."""
    host = config.find_host_by_id(host_id)
    if not host:
        return {"success": False, "error": f"Unknown host: {host_id}"}, 400
    mac = host.get("mac", "")
    if not mac:
        return {"success": False, "error": "No MAC address configured"}, 400
    ok = net.send_wol(mac, host.get("ip", ""))
    return {
        "success": ok,
        "message": f"WOL sent to {host_id}" if ok else "WOL failed",
    }, 200


def _handle_dualboot_switch(host_id: str, body: Dict[str, Any]) -> Response:
    """GRUB one-shot + reboot to *target* (windows|debian)."""
    host = config.find_host_by_id(host_id)
    if not host:
        return {"error": f"Unknown host: {host_id}"}, 400

    target = (body.get("target") if isinstance(body, dict) else "") or ""
    if target not in ("windows", "debian"):
        return {"error": "target must be 'windows' or 'debian'"}, 400

    entries = hosts.get_grub_entries(host)
    entry = entries.get(target)
    if not entry:
        return {
            "success": False,
            "error": f"No '{target}' GRUB entry configured",
        }, 400

    if not hosts.grub_reboot(host, entry):
        return {"success": False, "error": "grub-reboot failed"}, 200

    if not hosts.ssh_cmd(host, "reboot"):
        return {"success": False, "error": "Reboot command failed"}, 200

    return {"success": True, "message": f"Booting {target}"}, 200


def _handle_start_windows(host_id: str) -> Response:
    """Boot-into-Windows sequence (background daemon thread).

    *host_id* MUST be the dual-boot Linux host — the cap check in the
    dispatcher verified it, and we resolve through it so a caller-chosen
    id can never target a different machine than the one they were
    granted.
    """
    linux = config.find_host_by_id(host_id)
    if not linux or linux.get("type") != "linux" or not linux.get("dual_boot_peer"):
        return {"error": "Not a dual-boot host"}, 400
    tasks.start_boot_windows(linux)
    return {
        "success": True,
        "message": "Boot sequence started",
        "task": tasks.TASK_BOOT_WINDOWS,
    }, 200


def _handle_start_debian(host_id: str) -> Response:
    """Boot-into-Debian sequence (background daemon thread).

    *host_id* MUST be the dual-boot Linux host.
    """
    linux = config.find_host_by_id(host_id)
    if not linux or linux.get("type") != "linux" or not linux.get("dual_boot_peer"):
        return {"error": "Not a dual-boot host"}, 400
    win = config.find_host_by_id(linux.get("dual_boot_peer"))
    if not win:
        return {"error": "Dual-boot peer not configured"}, 400

    state = hosts.get_dualboot_state(linux)
    if state == hosts.WINDOWS_RUNNING:
        tasks.start_boot_debian(linux, win)
        return {
            "success": True,
            "message": "Boot sequence started",
            "task": tasks.TASK_BOOT_DEBIAN,
        }, 200
    if state == hosts.DEBIAN_RUNNING:
        return {"success": True, "message": "Debian is already running"}, 200

    # Machine off — just WOL (Debian is GRUB default)
    net.send_wol(linux["mac"], linux.get("ip", ""))
    return {"success": True, "message": "WOL sent, booting Debian"}, 200


def _handle_grub_reset(host_id: str) -> Response:
    """Clear GRUB next_entry (V4 endpoint, restored for v5)."""
    host = config.find_host_by_id(host_id)
    if not host:
        return {"error": f"Unknown host: {host_id}"}, 400
    ok = hosts.grub_reset(host)
    return {
        "success": ok,
        "message": "next_entry cleared" if ok else "Reset failed",
    }, 200


# ═══════════════════════════════════════════════════════════════════════════════
# Admin config editing (v6.2 — "add" via web UI, admin only)
# ═══════════════════════════════════════════════════════════════════════════════

_HOST_CAPABILITIES = ("wake", "shutdown", "reboot", "grub_switch", "dualboot")
_SERVICE_NAME_RE = _re.compile(r"[A-Za-z0-9._-]{1,32}")
_SERVICE_SOURCES = ("dockge", "nginx", "manual")
_config_edit_lock = _threading.Lock()   # serializes read-modify-write on config.json


def _edit_config() -> Dict[str, Any]:
    """Return a DEEP COPY of the current config for mutation.

    Never mutate the dict get_config() returns — it is the live cache.
    Callers must hold _config_edit_lock around read-modify-write.
    """
    return _deepcopy(config.get_config())


def _handle_add_host(body: Dict[str, Any]) -> Response:
    """Append a host to config.json (ADMIN ONLY — guard enforces)."""
    if not isinstance(body, dict):
        return {"error": "JSON body required"}, 400
    host_id = str(body.get("id", "")).strip()
    name = str(body.get("name", "")).strip()
    ip = str(body.get("ip", "")).strip()
    mac = str(body.get("mac", "")).strip()
    htype = str(body.get("type", "linux")).strip()

    if not _re.fullmatch(r"[A-Za-z0-9._-]{1,32}", host_id):
        return {"error": "invalid id (letters, digits, . _ - only, max 32)"}, 400
    if not name:
        return {"error": "name is required"}, 400
    try:
        _ipaddress.ip_address(ip)
    except ValueError:
        return {"error": "invalid ip"}, 400
    if not _re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac):
        return {"error": "invalid mac (XX:XX:XX:XX:XX:XX)"}, 400
    if htype not in ("linux", "windows"):
        return {"error": "type must be linux or windows"}, 400

    with _config_edit_lock:
        cfg = _edit_config()
        hosts_cfg = cfg.setdefault("hosts", [])
        if any(h.get("id") == host_id for h in hosts_cfg):
            return {"error": f"host '{host_id}' already exists"}, 400

        host: Dict[str, Any] = {
            "id": host_id, "name": name, "ip": ip, "mac": mac, "type": htype,
        }
        os_label = str(body.get("os_label", "")).strip()
        if os_label:
            host["os_label"] = os_label
        icon = str(body.get("icon", "")).strip()
        if icon:
            host["icon"] = icon
        caps = body.get("capabilities")
        if caps is not None:
            if not isinstance(caps, list) or not all(
                isinstance(c, str) and c in _HOST_CAPABILITIES for c in caps
            ):
                return {"error": "invalid capabilities"}, 400
            host["capabilities"] = caps

        hosts_cfg.append(host)
        if not config.save_config(cfg):
            return {"error": "could not write config.json"}, 500
    return {"success": True, "message": f"Host '{host_id}' added"}, 200


def _handle_add_service(body: Dict[str, Any]) -> Response:
    """Append a service to config.json (ADMIN ONLY — guard enforces)."""
    if not isinstance(body, dict):
        return {"error": "JSON body required"}, 400
    name = str(body.get("name", "")).strip()
    url = str(body.get("url", "")).strip()
    # v7 (#16): service names are URL path segments — same regex as host ids
    if not _SERVICE_NAME_RE.fullmatch(name):
        return {"error": "invalid name (letters, digits, . _ - only, max 32)"}, 400
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"error": "url must start with http:// or https://"}, 400

    with _config_edit_lock:
        cfg = _edit_config()
        services = cfg.setdefault("services", [])
        if any(s.get("name") == name for s in services):
            return {"error": f"service '{name}' already exists"}, 400

        svc: Dict[str, Any] = {"name": name, "url": url}
        icon = str(body.get("icon", "")).strip()
        if icon:
            svc["icon"] = icon
        group = str(body.get("group", "")).strip()
        if group:
            svc["group"] = group
        host = str(body.get("host", "")).strip()
        if host:
            svc["host"] = host
        source = str(body.get("source", "manual")).strip()
        if source in _SERVICE_SOURCES:
            svc["source"] = source
        sp = body.get("status_port")
        if sp is not None:
            try:
                sp = int(sp)
                if not 1 <= sp <= 65535:
                    raise ValueError
            except (TypeError, ValueError):
                return {"error": "status_port must be an integer 1-65535"}, 400
            svc["status_port"] = sp

        services.append(svc)
        if not config.save_config(cfg):
            return {"error": "could not write config.json"}, 500
    return {"success": True, "message": f"Service '{name}' added"}, 200


def _handle_update_host(host_id: str, body: Dict[str, Any]) -> Response:
    """Update a host in config.json (ADMIN).  PATCH semantics: only fields
    present in *body* change; the id itself is immutable."""
    if not isinstance(body, dict):
        return {"error": "JSON body required"}, 400
    with _config_edit_lock:
        cfg = _edit_config()
        hosts_cfg = cfg.get("hosts", [])
        idx = next((i for i, h in enumerate(hosts_cfg) if h.get("id") == host_id), None)
        if idx is None:
            return {"error": f"Unknown host: {host_id}"}, 404
        cur = hosts_cfg[idx]

        name = str(body.get("name", "")).strip()
        if "name" in body:
            if not name:
                return {"error": "name is required"}, 400
            cur["name"] = name
        if "ip" in body:
            ip = str(body.get("ip", "")).strip()
            try:
                _ipaddress.ip_address(ip)
            except ValueError:
                return {"error": "invalid ip"}, 400
            cur["ip"] = ip
        if "mac" in body:
            mac = str(body.get("mac", "")).strip()
            if not _re.fullmatch(r"([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", mac):
                return {"error": "invalid mac (XX:XX:XX:XX:XX:XX)"}, 400
            cur["mac"] = mac
        if "type" in body:
            htype = str(body.get("type", "")).strip()
            if htype not in ("linux", "windows"):
                return {"error": "type must be linux or windows"}, 400
            cur["type"] = htype
        for field in ("os_label", "icon", "ssh_user", "ssh_shutdown_cmd"):
            if field in body:
                v = str(body.get(field, "")).strip()
                if v:
                    cur[field] = v
                else:
                    cur.pop(field, None)
        if "capabilities" in body:
            caps = body.get("capabilities")
            if caps is None:
                cur.pop("capabilities", None)
            else:
                if not isinstance(caps, list) or not all(
                    isinstance(c, str) and c in _HOST_CAPABILITIES for c in caps
                ):
                    return {"error": "invalid capabilities"}, 400
                cur["capabilities"] = caps
        if "dual_boot_peer" in body:
            peer = str(body.get("dual_boot_peer", "")).strip()
            if not peer:
                cur.pop("dual_boot_peer", None)
            elif peer == host_id:
                return {"error": "dual_boot_peer cannot be the host itself"}, 400
            elif not any(h.get("id") == peer for h in hosts_cfg):
                return {"error": f"unknown dual_boot_peer: {peer}"}, 400
            else:
                cur["dual_boot_peer"] = peer
        if "grub_entries" in body:
            ge = body.get("grub_entries")
            if ge is None:
                cur.pop("grub_entries", None)
            elif not isinstance(ge, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in ge.items()
            ):
                return {"error": "grub_entries must be a string map"}, 400
            else:
                cur["grub_entries"] = ge

        if not config.save_config(cfg):
            return {"error": "could not write config.json"}, 500
    return {"success": True, "message": f"Host '{host_id}' updated"}, 200


def _handle_delete_host(host_id: str) -> Response:
    """Delete a host (ADMIN).  Cleans up dual_boot_peer refs AND strips the
    id from every user's caps (v7 #15 — a re-used id must not resurrect
    old grants)."""
    with _config_edit_lock:
        cfg = _edit_config()
        hosts_cfg = cfg.get("hosts", [])
        idx = next((i for i, h in enumerate(hosts_cfg) if h.get("id") == host_id), None)
        if idx is None:
            return {"error": f"Unknown host: {host_id}"}, 404
        hosts_cfg.pop(idx)
        for h in hosts_cfg:
            if h.get("dual_boot_peer") == host_id:
                h.pop("dual_boot_peer", None)
        if not config.save_config(cfg):
            return {"error": "could not write config.json"}, 500
    auth.strip_caps_target(host_id)
    auth.strip_caps_prefix(host_id + ":")
    return {"success": True, "message": f"Host '{host_id}' deleted"}, 200


def _handle_update_service(name: str, body: Dict[str, Any]) -> Response:
    """Update a service in config.json (ADMIN).  PATCH semantics (#16)."""
    if not isinstance(body, dict):
        return {"error": "JSON body required"}, 400
    if not _SERVICE_NAME_RE.fullmatch(name):
        return {"error": "service name cannot be addressed via URL (special "
                        "chars) — edit config.json directly"}, 400
    with _config_edit_lock:
        cfg = _edit_config()
        services = cfg.get("services", [])
        idx = next((i for i, s in enumerate(services) if s.get("name") == name), None)
        if idx is None:
            return {"error": f"Unknown service: {name}"}, 404
        cur = services[idx]

        if "name" in body:
            new_name = str(body.get("name", "")).strip()
            if not _SERVICE_NAME_RE.fullmatch(new_name):
                return {"error": "invalid name (letters, digits, . _ - only, max 32)"}, 400
            if new_name != name and any(s.get("name") == new_name for s in services):
                return {"error": f"service '{new_name}' already exists"}, 400
            cur["name"] = new_name
        if "url" in body:
            url = str(body.get("url", "")).strip()
            if not (url.startswith("http://") or url.startswith("https://")):
                return {"error": "url must start with http:// or https://"}, 400
            cur["url"] = url
        for field in ("icon", "group", "host"):
            if field in body:
                v = str(body.get(field, "")).strip()
                if v:
                    cur[field] = v
                else:
                    cur.pop(field, None)
        if "source" in body:
            source = str(body.get("source", "")).strip()
            if source not in _SERVICE_SOURCES:
                return {"error": "source must be dockge, nginx or manual"}, 400
            cur["source"] = source
        if "status_port" in body:
            sp = body.get("status_port")
            if sp is None:
                cur.pop("status_port", None)
            else:
                try:
                    sp = int(sp)
                    if not 1 <= sp <= 65535:
                        raise ValueError
                except (TypeError, ValueError):
                    return {"error": "status_port must be an integer 1-65535"}, 400
                cur["status_port"] = sp
        if "status_host" in body:
            v = str(body.get("status_host", "")).strip()
            if v:
                cur["status_host"] = v
            else:
                cur.pop("status_host", None)
        if "health_path" in body:
            v = str(body.get("health_path", "")).strip()
            if v:
                cur["health_path"] = v
            else:
                cur.pop("health_path", None)

        if not config.save_config(cfg):
            return {"error": "could not write config.json"}, 500
    return {"success": True, "message": f"Service '{name}' updated"}, 200


def _handle_delete_service(name: str) -> Response:
    """Delete a service (ADMIN)."""
    if not _SERVICE_NAME_RE.fullmatch(name):
        return {"error": "service name cannot be addressed via URL (special "
                        "chars) — edit config.json directly"}, 400
    with _config_edit_lock:
        cfg = _edit_config()
        services = cfg.get("services", [])
        idx = next((i for i, s in enumerate(services) if s.get("name") == name), None)
        if idx is None:
            return {"error": f"Unknown service: {name}"}, 404
        services.pop(idx)
        if not config.save_config(cfg):
            return {"error": "could not write config.json"}, 500
    return {"success": True, "message": f"Service '{name}' deleted"}, 200


def _handle_apply_scan(body: Dict[str, Any]) -> Response:
    """Add scanned candidates to config.json (ADMIN).

    Explicit confirmation = this call.  nginx candidates carry no name and
    must be named by the admin here; auto-add never applies to them (#20).
    """
    if not isinstance(body, dict):
        return {"error": "JSON body required"}, 400
    cands = body.get("candidates")
    if not isinstance(cands, list) or not cands:
        return {"error": "candidates must be a non-empty list"}, 400
    added: List[str] = []
    with _config_edit_lock:
        cfg = _edit_config()
        services = cfg.setdefault("services", [])
        for cand in cands:
            if not isinstance(cand, dict):
                return {"error": "invalid candidate"}, 400
            name = str(cand.get("name", "")).strip()
            url = str(cand.get("url", "")).strip()
            if not _SERVICE_NAME_RE.fullmatch(name):
                return {"error": f"candidate '{name}': invalid name (letters, "
                                "digits, . _ - only, max 32)"}, 400
            if not (url.startswith("http://") or url.startswith("https://")):
                return {"error": f"candidate '{name}': url must start with "
                                "http:// or https://"}, 400
            if any(s.get("name") == name for s in services):
                continue                      # duplicate name → skip silently
            svc: Dict[str, Any] = {"name": name, "url": url}
            host = str(cand.get("host", "")).strip()
            if host:
                svc["host"] = host
            source = str(cand.get("source", "manual")).strip()
            if source in _SERVICE_SOURCES:
                svc["source"] = source
            sp = cand.get("port")
            if sp is not None:
                try:
                    sp = int(sp)
                    if 1 <= sp <= 65535:
                        svc["status_port"] = sp
                except (TypeError, ValueError):
                    pass
            services.append(svc)
            added.append(name)
        if not added:
            return {"success": True, "message": "No new services",
                    "added": []}, 200
        if not config.save_config(cfg):
            return {"error": "could not write config.json"}, 500
    return {"success": True, "message": f"Added {len(added)} service(s)",
            "added": added}, 200


# ═══════════════════════════════════════════════════════════════════════════════
# Route dispatch
# ═══════════════════════════════════════════════════════════════════════════════


def _require_cap(session: dict | None, action: str, target: str) -> Response | None:
    """Return a 403 response if *session* lacks *action* on *target*, else None."""
    if auth.can_act(session, action, target):
        return None
    return {"error": f"Forbidden: no '{action}' permission on '{target}'"}, 403


def handle_get(path: str, params: Params, session: dict | None = None) -> Response:  # noqa: ARG001
    """Dispatch a GET API request; return (data, status_code).

    Uses V2-style path-segment matching.  Every route listed in
    CONTRACT.md is implemented; nothing from V4 query-string endpoints
    survives.
    """
    # ── Plugin routes (delegated) ─────────────────────────────────────────
    if path.startswith("/api/plugin/"):
        from pi_hub.plugins import get_manager
        result = get_manager().dispatch("GET", path, session)
        if result is not None:
            return result
        return {"error": "Not found"}, 404

    # ── Exact-path routes (fast path) ─────────────────────────────────────
    if path == "/api/health":
        return {"ok": True}, 200

    if path == "/api/auth/me":
        if session:
            rec = auth.fresh_user(session.get("user", ""))
            return {
                "username": session["user"],
                "role": (rec or session).get("role", "viewer"),
                "caps": (rec or {}).get("caps", {}),
            }, 200
        return {"error": "Unauthorized"}, 401

    if path == "/api/auth/users":
        # ADMIN ONLY (guard enforces); never expose hashes
        users = auth.load_users()
        return [
            {"username": u, "role": r.get("role", "viewer"),
             "caps": r.get("caps", {}),
             "created": r.get("created", "")}
            for u, r in sorted(users.items())
        ], 200

    if path == "/api/config":
        # SECURITY S1 — NEVER serve raw config
        return config.public_config(), 200

    if path == "/api/config/full":
        # ADMIN ONLY (classify #1).  Tokens are {set, source} info only (#6).
        return config.admin_config(), 200

    if path == "/api/icons":
        # Backward-compat alias for the static icons.json
        return _load_icons(), 200

    if path == "/api/hosts":
        # Static host registry (CLI use) — filtered through public allowlist:
        # no MACs, no ssh_user, no grub_entries
        return [
            config.public_host(h)
            for h in config.get_hosts()
        ], 200

    if path == "/api/hosts/status":
        # Rich record per host (parallel TCP/22 probe)
        return hosts.get_all_status(), 200

    if path == "/api/hosts/dualboot":
        # DEPRECATED — alias for /api/hosts/status.
        # Kept one release for cached frontends.  Migrate callers to
        # /api/hosts/status which carries the full rich record including
        # dualboot_state.
        return hosts.get_all_status(), 200

    if path == "/api/status":
        # ── SEMANTIC COLLISION ──────────────────────────────────────────
        # root routes.py:29 mapped /api/status → hosts.get_all_status()
        # (HOSTS).  V2 maps it → check_services() (SERVICES).  The
        # frontend polls /api/status expecting service liveness, so V2
        # wins.  Host status is at /api/hosts/status.
        return _check_services(), 200

    if path == "/api/proxmox/containers":
        return proxmox.fetch_proxmox_containers(), 200

    if path == "/api/dockge/stacks":
        return dockge.list_stacks(), 200

    if path == "/api/update/status":
        # ADMIN ONLY (classify).  Serves the cached release check — never
        # triggers network I/O on a GET.
        return updater.status(), 200

    if path == "/api/plugins/list":
        # Any authenticated user can see plugin status.  Served by the
        # plugin manager (which delegates to enabled plugins).
        from pi_hub.plugins import get_manager
        return get_manager().get_status(), 200

    if path == "/api/config/plugins/sources":
        # Plugin store sources + cached scan results (ADMIN — classify).
        from pi_hub.plugins import store
        return store.list_sources(), 200

    # ── Prefix routes ────────────────────────────────────────────────────
    if path.startswith("/api/task/"):
        name = path[len("/api/task/"):].rstrip("/")
        t = tasks.get_task_status(name)
        if t:
            return t, 200
        return {"status": "not_found"}, 404

    return {"error": "Not found"}, 404


def _resolve_container_target(instance_id: str, vmid: str) -> tuple[str, str]:
    """Resolve the container action target (v7 #8).

    Returns (instance_id, cap_target).  An EMPTY instance_id resolves to
    the default instance's id — the cap target is ALWAYS ``instance:vmid``
    and is built in exactly this one place, so the legacy alias and the
    canonical instanced route can never disagree.
    """
    if not instance_id:
        instance_id = config.proxmox_default_instance().get("id", "")
    return instance_id, f"{instance_id}:{vmid}"


def _handle_container_action(instance_id: str, vmid: str, action: str,
                             session: dict | None) -> Response:
    """Cap-checked container action for *instance_id* ('' → default)."""
    inst_id, target = _resolve_container_target(instance_id, vmid)
    err = _require_cap(session, "containers", target)
    if err:
        return err
    return proxmox.pve_action(vmid, action, inst_id), 200


def handle_post(path: str, params: Params, body: Dict[str, Any],
                session: dict | None = None, token: str = "",
                ip: str = "") -> Response:  # noqa: ARG001
    """Dispatch a POST API request; return (data, status_code).

    Every mutating route in CONTRACT.md is implemented.  All V4
    query-string endpoints (/api/grub-reboot, /api/dualboot/switch with
    ?id=…, etc.) are dropped.
    """
    parts = [p for p in path.split("/") if p]

    # Guard: reject non-api paths early
    if len(parts) < 2 or parts[0] != "api":
        return {"error": "Not found"}, 404

    # ── Plugin routes (delegated) ─────────────────────────────────────────
    if path.startswith("/api/plugin/"):
        from pi_hub.plugins import get_manager
        result = get_manager().dispatch("POST", path, session, body=body)
        if result is not None:
            return result
        return {"error": "Not found"}, 404

    # ── POST /api/auth/bootstrap  (public, first-run only) ─────────────────
    if path == "/api/auth/bootstrap":
        if not config.auth_enabled():
            return {"error": "Forbidden"}, 403
        if auth.users_state() not in ("missing", "empty"):
            return {"error": "Forbidden"}, 403
        if not auth.check_rate_limit(ip):
            return {"error": "Too many attempts, try again later"}, 429
        username = (body.get("username") if isinstance(body, dict) else "") or ""
        password = (body.get("password") if isinstance(body, dict) else "") or ""
        res = auth.bootstrap_admin(username, password)
        if not res["ok"]:
            auth.record_failure(ip)
            return {"error": res["error"]}, 400
        auth.record_success(ip)
        token = auth.issue_session(username, "admin")
        return {"token": token, "user": {
            "username": username, "role": "admin", "caps": {},
        }}, 200

    # ── POST /api/auth/login  (public) ────────────────────────────────────
    if path == "/api/auth/login":
        username = (body.get("username") if isinstance(body, dict) else "") or ""
        password = (body.get("password") if isinstance(body, dict) else "") or ""
        username = username.strip()
        if not auth.check_rate_limit(ip):
            return {"error": "Too many attempts, try again later"}, 429
        user = auth.authenticate(username, password)
        if not user:
            auth.record_failure(ip)
            return {"error": "Invalid credentials"}, 401
        auth.record_success(ip)
        token = auth.issue_session(username, user["role"])
        return {"token": token, "user": {
            "username": username, "role": user["role"],
            "caps": user.get("caps", {}),
        }}, 200

    # ── POST /api/auth/logout  (any authenticated user) ───────────────────
    if path == "/api/auth/logout":
        auth.revoke_session(token)
        return {"success": True}, 200

    # ── POST /api/auth/users  (admin — create user) ───────────────────────
    if path == "/api/auth/users":
        res = auth.add_user(
            (body.get("username", "") if isinstance(body, dict) else "") or "",
            (body.get("password", "") if isinstance(body, dict) else "") or "",
            (body.get("role", "viewer") if isinstance(body, dict) else "viewer") or "viewer",
            (body.get("caps") if isinstance(body, dict) else None),
        )
        if res["ok"]:
            return {"success": True, "message": "User created"}, 200
        return {"error": res["error"]}, 400

    # ── POST /api/auth/users/<name>/caps  (admin — set capability matrix) ─
    if (
        len(parts) == 5
        and parts[1] == "auth"
        and parts[2] == "users"
        and parts[4] == "caps"
    ):
        target = parts[3]
        caps = (body.get("caps") if isinstance(body, dict) else None)
        res = auth.set_caps(target, caps)
        if res["ok"]:
            return {"success": True,
                    "message": f"Capabilities updated for {target}",
                    "dropped": res.get("dropped", [])}, 200
        return {"error": res["error"]}, 400

    # ── POST /api/auth/users/<name>/password  (admin or self) ─────────────
    if (
        len(parts) == 5
        and parts[1] == "auth"
        and parts[2] == "users"
        and parts[4] == "password"
    ):
        target = parts[3]
        if (not session) or (session.get("role") != "admin" and session.get("user") != target):
            return {"error": "Forbidden"}, 403
        res = auth.change_password(
            target,
            (body.get("password", "") if isinstance(body, dict) else "") or "",
            session.get("user", ""),
        )
        if res["ok"]:
            return {"success": True, "message": "Password updated"}, 200
        return {"error": res["error"]}, 400

    # ── POST /api/config/hosts  (admin — add host) ────────────────────────
    if path == "/api/config/hosts":
        return _handle_add_host(body)

    # ── POST /api/config/services  (admin — add service) ──────────────────
    if path == "/api/config/services":
        return _handle_add_service(body)

    # ── POST /api/config/proxmox  (admin — replace instances, write tokens) ─
    if path == "/api/config/proxmox":
        if not isinstance(body, dict):
            return {"error": "JSON body required"}, 400
        instances = body.get("instances")
        if not isinstance(instances, list):
            return {"error": "instances must be a list"}, 400
        clean: List[Dict[str, Any]] = []
        seen: set = set()
        for inst in instances:
            if not isinstance(inst, dict):
                return {"error": "invalid instance"}, 400
            iid = str(inst.get("id", "")).strip()
            host = str(inst.get("host", "")).strip()
            if not _re.fullmatch(r"[A-Za-z0-9._-]{1,32}", iid):
                return {"error": "invalid instance id (letters, digits, . _ - only, max 32)"}, 400
            if iid in seen:
                return {"error": f"duplicate instance id: {iid}"}, 400
            seen.add(iid)
            if not host:
                return {"error": f"instance '{iid}': host is required"}, 400
            entry: Dict[str, Any] = {"id": iid, "host": host,
                                     "node": str(inst.get("node", "")).strip() or iid}
            if "enabled" in inst:
                entry["enabled"] = bool(inst["enabled"])
            ssh_user = str(inst.get("ssh_user", "")).strip()
            if ssh_user:
                entry["ssh_user"] = ssh_user
            clean.append(entry)
        old_ids = {str(p.get("id", "")) for p in config.proxmox_instances()}
        with _config_edit_lock:
            cfg = _edit_config()
            cfg["proxmox"] = clean
            if not config.save_config(cfg):
                return {"error": "could not write config.json"}, 500
        # v7 #4: instances that disappeared must not leave their container
        # caps behind (re-used id would resurrect old grants).
        gone = old_ids - {i["id"] for i in clean}
        for g in gone:
            auth.strip_caps_prefix(g + ":")
        # Tokens → secrets.json, write-only (blank = keep existing);
        # orphaned tokens of removed instances are pruned (v7 #8).
        write_tokens: Dict[str, str] = {}
        tokens = body.get("tokens")
        if isinstance(tokens, dict):
            for iid, tok in tokens.items():
                if isinstance(tok, str) and tok.strip():
                    write_tokens[str(iid)] = tok.strip()
        if write_tokens or gone:
            if not config.save_proxmox_tokens(write_tokens, drop=sorted(gone)):
                return {"success": True,
                        "warning": "instances saved, but secrets.json write failed"}, 200
        return {"success": True, "message": "Proxmox instances updated"}, 200

    # ── POST /api/config/meta  (admin — title, dockge, groups, scan) ──────
    if path == "/api/config/meta":
        if not isinstance(body, dict):
            return {"error": "JSON body required"}, 400
        with _config_edit_lock:
            cfg = _edit_config()
            title = str(body.get("title", "")).strip()
            if title:
                cfg["title"] = title
            dockge = body.get("dockge")
            if isinstance(dockge, dict):
                d = dict(cfg.get("dockge", {}) or {})
                if "enabled" in dockge:
                    d["enabled"] = bool(dockge["enabled"])
                if "base_url" in dockge:
                    d["base_url"] = str(dockge["base_url"]).strip()
                cfg["dockge"] = d
            groups = body.get("groups")
            if isinstance(groups, dict):
                cfg["groups"] = groups
            scan = body.get("scan")
            if isinstance(scan, dict):
                s = dict(cfg.get("scan", {}) or {})
                if "auto_add" in scan:
                    s["auto_add"] = bool(scan["auto_add"])
                if "sources" in scan:
                    srcs = []
                    if isinstance(scan["sources"], list):
                        for src in scan["sources"]:
                            if not isinstance(src, dict):
                                continue
                            stype = str(src.get("type", ""))
                            if stype not in scanner.SOURCE_TYPES:
                                continue
                            entry: Dict[str, Any] = {"type": stype}
                            if stype == "dockge-ssh":
                                host_id = str(src.get("host_id", "")).strip()
                                if not host_id or not config.find_host_by_id(host_id):
                                    continue        # registry check — never accept unknown hosts
                                entry["host_id"] = host_id
                                name = str(src.get("name", "")).strip()
                                if name:
                                    entry["name"] = name
                            srcs.append(entry)
                    s["sources"] = srcs
                cfg["scan"] = s
            if not config.save_config(cfg):
                return {"error": "could not write config.json"}, 500
        return {"success": True, "message": "Settings updated"}, 200

    # ── POST /api/config/scan  (admin — discover candidates, no writes) ────
    if path == "/api/config/scan":
        return scanner.run_scan(), 200

    # ── POST /api/config/scan/apply  (admin — add confirmed candidates) ────
    if path == "/api/config/scan/apply":
        return _handle_apply_scan(body)

    # ── POST /api/config/hosts/<id>  (admin — update host) ────────────────
    if len(parts) == 4 and parts[1] == "config" and parts[2] == "hosts":
        return _handle_update_host(parts[3], body)

    # ── POST /api/config/services/<name>  (admin — update service) ─────────
    if len(parts) == 4 and parts[1] == "config" and parts[2] == "services":
        return _handle_update_service(parts[3], body)

    # ── POST /api/hosts/<id>/<action>  (len=4) ───────────────────────────
    if len(parts) == 4 and parts[1] == "hosts":
        host_id, action = parts[2], parts[3]

        # Validate host_id against config registry FIRST (no side effects)
        if not config.find_host_by_id(host_id):
            return {"error": f"Unknown host: {host_id}"}, 400

        if action == "wake":
            err = _require_cap(session, "wake", host_id)
            if err:
                return err
            return _handle_wake(host_id)
        if action in ("reboot", "shutdown", "poweroff"):
            cap = "reboot" if action == "reboot" else "shutdown"
            err = _require_cap(session, cap, host_id)
            if err:
                return err
            return hosts.ssh_action(host_id, action), 200

        return {"error": f"Unknown action: {action}"}, 400

    # ── POST /api/hosts/<id>/dualboot/switch  (len=5) ────────────────────
    if (
        len(parts) == 5
        and parts[1] == "hosts"
        and parts[3] == "dualboot"
        and parts[4] == "switch"
    ):
        err = _require_cap(session, "dualboot", parts[2])
        if err:
            return err
        return _handle_dualboot_switch(parts[2], body)

    # ── POST /api/hosts/<id>/start/windows  (len=5) ─────────────────────
    if (
        len(parts) == 5
        and parts[1] == "hosts"
        and parts[3] == "start"
        and parts[4] == "windows"
    ):
        err = _require_cap(session, "dualboot", parts[2])
        if err:
            return err
        return _handle_start_windows(parts[2])

    # ── POST /api/hosts/<id>/start/debian  (len=5) ──────────────────────
    if (
        len(parts) == 5
        and parts[1] == "hosts"
        and parts[3] == "start"
        and parts[4] == "debian"
    ):
        err = _require_cap(session, "dualboot", parts[2])
        if err:
            return err
        return _handle_start_debian(parts[2])

    # ── POST /api/hosts/<id>/grub/reset  (len=5) ────────────────────────
    if (
        len(parts) == 5
        and parts[1] == "hosts"
        and parts[3] == "grub"
        and parts[4] == "reset"
    ):
        err = _require_cap(session, "dualboot", parts[2])
        if err:
            return err
        return _handle_grub_reset(parts[2])

    # ── POST /api/proxmox/<instance>/container/<vmid>/<action>  (v7, len=6) ─
    if (
        len(parts) == 6
        and parts[1] == "proxmox"
        and parts[3] == "container"
    ):
        return _handle_container_action(parts[2], parts[4], parts[5], session)

    # ── POST /api/proxmox/container/<vmid>/<action>  (legacy alias, len=5) ──
    # Aliases to the DEFAULT instance; the cap target is built in
    # _resolve_container_target, identical to the instanced route (#8).
    if (
        len(parts) == 5
        and parts[1] == "proxmox"
        and parts[2] == "container"
    ):
        return _handle_container_action("", parts[3], parts[4], session)

    # ── POST /api/update/check  (admin — force a GitHub release check) ────
    if path == "/api/update/check":
        updater.check(force=True)
        return updater.status(), 200

    # ── POST /api/update/apply  (admin — install the approved release) ────
    if path == "/api/update/apply":
        # HARDENED (#15): apply is the most destructive route in the app —
        # it replaces code and restarts the service.  Require a REAL
        # authenticated admin session even when auth is disabled in
        # config.json (the guard's synthetic "local" admin does not count).
        if (not session) or session.get("user") == "local":
            return {"error": "Forbidden — update requires an "
                             "authenticated admin session"}, 403
        expected = (str(body.get("expected_tag") or "")
                    if isinstance(body, dict) else "") or ""
        return updater.apply(expected)

    # ── Plugin store (ADMIN — classify() gates /api/config/*) ────────────
    # The plugin store installs + executes arbitrary Python from GitHub —
    # RCE-class.  Same hardening as /api/update/apply: a REAL
    # authenticated admin session is required even when auth is disabled
    # in config.json (the guard's synthetic "local" admin does not count).
    _PLUGIN_STORE_MUTATING = False
    if path in ("/api/config/plugins/sources",
                "/api/config/plugins/install") or (
            len(parts) == 6 and parts[1] == "config" and parts[2] == "plugins"
            and parts[3] == "sources" and parts[5] == "scan") or (
            len(parts) == 5 and parts[1] == "config" and parts[2] == "plugins"
            and parts[4] in ("enable", "disable")):
        _PLUGIN_STORE_MUTATING = True
    if _PLUGIN_STORE_MUTATING and (
            (not session) or session.get("user") == "local"):
        return {"error": "Forbidden — plugin management requires an "
                         "authenticated admin session"}, 403

    # POST /api/config/plugins/sources  (add a GitHub source)
    if path == "/api/config/plugins/sources":
        from pi_hub.plugins import store
        url = (str(body.get("url") or "") if isinstance(body, dict) else "") or ""
        source, err = store.add_source(url)
        if err or source is None:
            return {"error": err or "could not add source"}, 400
        return {"success": True, "message": f"Source '{source['id']}' added",
                "source": source}, 200

    # POST /api/config/plugins/sources/<id>/scan  (re-scan releases)
    if len(parts) == 6 and parts[1] == "config" and parts[2] == "plugins" \
            and parts[3] == "sources" and parts[5] == "scan":
        from pi_hub.plugins import store
        plugins, err = store.scan_source(parts[4], force=True)
        if err:
            return {"error": err}, 400
        return {"success": True, "plugins": plugins}, 200

    # POST /api/config/plugins/install  (install from scan cache)
    if path == "/api/config/plugins/install":
        from pi_hub.plugins import store
        if not isinstance(body, dict):
            return {"error": "JSON body required"}, 400
        source_id = str(body.get("source_id", "")).strip()
        name = str(body.get("name", "")).strip()
        version = str(body.get("version", "")).strip()
        ok, msg = store.install(source_id, name, version)
        if not ok:
            return {"error": msg}, 400
        return {"success": True, "message": msg}, 200

    # POST /api/config/plugins/<name>/enable  |  /disable
    if len(parts) == 5 and parts[1] == "config" and parts[2] == "plugins" \
            and parts[4] in ("enable", "disable"):
        from pi_hub.plugins import store
        name = parts[3]
        fn = store.enable if parts[4] == "enable" else store.disable
        ok, msg = fn(name)
        if not ok:
            return {"error": msg}, 400
        return {"success": True, "message": msg}, 200

    return {"error": "Not found"}, 404


def handle_delete(path: str, params: Params,
                  session: dict | None = None, ip: str = "") -> Response:  # noqa: ARG001
    """Dispatch a DELETE API request (v6 auth user management)."""
    parts = [p for p in path.split("/") if p]

    # ── DELETE /api/auth/users/<name>  (admin) ────────────────────────────
    if (
        len(parts) == 4
        and parts[1] == "auth"
        and parts[2] == "users"
    ):
        res = auth.delete_user(parts[3])
        if res["ok"]:
            return {"success": True, "message": f"User {parts[3]} deleted"}, 200
        return {"error": res["error"]}, 400

    # ── DELETE /api/config/hosts/<id>  (admin) ─────────────────────────────
    if len(parts) == 4 and parts[1] == "config" and parts[2] == "hosts":
        return _handle_delete_host(parts[3])

    # ── DELETE /api/config/services/<name>  (admin) ────────────────────────
    if len(parts) == 4 and parts[1] == "config" and parts[2] == "services":
        return _handle_delete_service(parts[3])

    # ── Plugin store (ADMIN) ──────────────────────────────────────────────
    # Same real-admin hardening as the POST routes — uninstalling a
    # plugin removes code the hub executes, so a synthetic "local" admin
    # (auth disabled) must not be able to reach it.
    if len(parts) >= 4 and parts[1] == "config" and parts[2] == "plugins" \
            and ((not session) or session.get("user") == "local"):
        return {"error": "Forbidden — plugin management requires an "
                         "authenticated admin session"}, 403

    # DELETE /api/config/plugins/sources/<id>  (remove source + uninstall)
    if len(parts) == 5 and parts[1] == "config" and parts[2] == "plugins" \
            and parts[3] == "sources":
        from pi_hub.plugins import store
        ok, msg = store.remove_source(parts[4])
        if not ok:
            return {"error": msg}, 400
        return {"success": True, "message": msg}, 200

    # DELETE /api/config/plugins/<name>  (uninstall)
    if len(parts) == 4 and parts[1] == "config" and parts[2] == "plugins":
        from pi_hub.plugins import store
        ok, msg = store.uninstall(parts[3])
        if not ok:
            return {"error": msg}, 400
        return {"success": True, "message": msg}, 200

    return {"error": "Not found"}, 404
