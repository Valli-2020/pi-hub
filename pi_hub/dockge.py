"""pi_hub.dockge — list and control Dockge stacks on one or more hosts.

Dockge has no REST API (only an authenticated socket.io protocol), so the hub
does what Dockge itself does: run ``docker compose`` in the stack directory,
locally or over SSH.  Dockge reads its state from Docker, so changes made here
show up there as well.

Endpoints come from ``dockge.endpoints`` in config.json::

    {"id": "pi",  "name": "Pi",  "stacks_dir": "/opt/stacks", "url": "http://192.168.1.10:5001"}
    {"id": "vps", "name": "VPS", "host_id": "vps", "url": "https://dockge.example.com"}

Without ``host_id`` the endpoint is this machine.  Without ``endpoints`` there
is a single local endpoint on ``dockge.stacks_dir``.

Public API
    dockge_enabled()                     → bool
    list_stacks()                        → list[dict]
    list_endpoints()                     → list[dict]
    start_action(endpoint, stack, action, user) → (dict, status)
    stack_logs(endpoint, stack, tail)    → (dict, status)
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from pi_hub import hosts, tasks
from pi_hub.config import find_host_by_id, get_config

log = logging.getLogger(__name__)

DEFAULT_STACKS_DIR = "/opt/stacks"
LOCAL_ENDPOINT_ID = "local"

LIST_TTL_S = 10.0
LIST_ERROR_TTL_S = 30.0   # an unreachable host must not cost an SSH timeout per poll
LIST_TIMEOUT_S = 12
LOGS_TIMEOUT_S = 20
ACTION_TIMEOUT_S = 900    # image pulls can take minutes

LOGS_DEFAULT_TAIL = 200
LOGS_MAX_TAIL = 2000
LOGS_MAX_BYTES = 256 * 1024

# Dockge only creates lower-case names; compose projects must start alnum.
_STACK_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_ENDPOINT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")
_DIR_RE = re.compile(r"/[A-Za-z0-9._/-]{1,200}")
_COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
_STATE_RE = re.compile(r"([a-z]+)\((\d+)\)")
_DIRS_MARKER = "@@PI_HUB_DIRS@@"

ACTIONS = ("start", "stop", "restart", "update")
_UP = "up -d --remove-orphans"
_ACTION_STEPS = {
    "start": [_UP],
    "stop": ["stop"],
    "restart": ["restart"],
    "update": ["pull", _UP],
}
_ACTION_LABELS = {"start": "Starting", "stop": "Stopping",
                  "restart": "Restarting", "update": "Updating"}

_lock = threading.Lock()
_cache: Dict[str, Dict[str, Any]] = {}
_refresh_locks: Dict[str, threading.Lock] = {}
_busy: set = set()


# ── Config ────────────────────────────────────────────────────────────────────


def _dockge_config() -> dict:
    return get_config().get("dockge", {}) or {}


def dockge_enabled() -> bool:
    """Honor an explicit ``dockge.enabled``, else auto-detect."""
    raw = _dockge_config()
    if isinstance(raw.get("enabled"), bool):
        return raw["enabled"]
    return bool(raw.get("endpoints") or raw.get("base_url")) or os.path.isdir(
        raw.get("stacks_dir", DEFAULT_STACKS_DIR))


def _web_url(value: Any) -> str:
    """Return *value* if it is a plain http(s) link safe to send to browsers."""
    url = str(value or "").strip()
    if not url.startswith(("http://", "https://")) or "@" in url:
        return ""
    return url


def _endpoints() -> List[dict]:
    """Return validated endpoints; invalid config entries are logged and skipped."""
    raw = _dockge_config()
    entries = raw.get("endpoints")
    if not isinstance(entries, list) or not entries:
        entries = [{"id": LOCAL_ENDPOINT_ID, "name": "This host",
                    "stacks_dir": raw.get("stacks_dir", DEFAULT_STACKS_DIR),
                    "url": raw.get("url", "")}]
    out: List[dict] = []
    seen: set = set()
    for e in entries:
        if not isinstance(e, dict):
            continue
        eid = str(e.get("id") or "")
        stacks_dir = str(e.get("stacks_dir") or DEFAULT_STACKS_DIR).rstrip("/")
        host_id = str(e.get("host_id") or "")
        host = find_host_by_id(host_id) if host_id else None
        if (not _ENDPOINT_RE.fullmatch(eid) or eid in seen
                or not _DIR_RE.fullmatch(stacks_dir) or ".." in stacks_dir
                or (host_id and host is None)):
            log.warning("dockge: ignoring invalid endpoint %r", eid)
            continue
        seen.add(eid)
        out.append({"id": eid, "name": str(e.get("name") or eid),
                    "stacks_dir": stacks_dir, "host": host,
                    "url": _web_url(e.get("url"))})
    return out


def _find_endpoint(endpoint_id: str) -> Optional[dict]:
    return next((e for e in _endpoints() if e["id"] == endpoint_id), None)


# ── Command execution ─────────────────────────────────────────────────────────


def _run(ep: dict, cmd: str, timeout: int) -> Dict[str, Any]:
    """Run *cmd* on the endpoint; same result shape as ``hosts.ssh_result``."""
    if ep["host"] is not None:
        return hosts.ssh_result(ep["host"], cmd, timeout=timeout)
    try:
        r = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": "timed out", "returncode": None}
    except OSError as exc:
        log.warning("dockge: local command failed: %s", exc)
        return {"ok": False, "stdout": "", "stderr": "could not run docker",
                "returncode": None}
    return {"ok": r.returncode == 0, "stdout": (r.stdout or "").strip(),
            "stderr": (r.stderr or "").strip(), "returncode": r.returncode}


def _compose(ep: dict, stack: str, args: str) -> str:
    directory = shlex.quote(f"{ep['stacks_dir']}/{stack}")
    return f"cd {directory} && docker compose {args}"


# ── Listing ───────────────────────────────────────────────────────────────────


def _list_command(ep: dict) -> str:
    base = shlex.quote(ep["stacks_dir"])
    patterns = " ".join(f"{base}/*/{name}" for name in _COMPOSE_FILES)
    return (
        "docker compose ls -a --format json || exit 3; "
        f"echo; echo {_DIRS_MARKER}; "
        f'for f in {patterns}; do if [ -f "$f" ]; then echo "$f"; fi; done'
    )


def _parse_state(status: str) -> Tuple[str, int, int]:
    """Turn ``"running(2), exited(1)"`` into ``("partial", 2, 3)``."""
    counts = {state: int(n) for state, n in _STATE_RE.findall(status.lower())}
    running = counts.get("running", 0)
    total = sum(counts.values())
    if total and running == total:
        return "running", running, total
    if running:
        return "partial", running, total
    return "exited", 0, total


def _parse_listing(ep: dict, stdout: str) -> List[dict]:
    """Merge ``docker compose ls`` with the compose files found on disk."""
    head, _, tail = stdout.partition(_DIRS_MARKER)
    projects = json.loads(head.strip() or "[]") or []
    root = ep["stacks_dir"]

    stacks: Dict[str, dict] = {}
    for path in tail.split():
        directory = os.path.dirname(path)
        name = os.path.basename(directory)
        if os.path.dirname(directory) == root and _STACK_RE.fullmatch(name):
            stacks[name] = {"name": name, "status": "inactive", "running": 0,
                            "total": 0, "managed": True}

    for project in projects:
        config_file = str(project.get("ConfigFiles") or "").split(",")[0]
        directory = os.path.dirname(config_file)
        status, running, total = _parse_state(str(project.get("Status") or ""))
        in_stacks_dir = os.path.dirname(directory) == root
        name = os.path.basename(directory) if in_stacks_dir else str(project.get("Name") or "?")
        managed = in_stacks_dir and name in stacks
        stacks[name] = {"name": name, "status": status, "running": running,
                        "total": total, "managed": managed}

    for stack in stacks.values():
        stack["endpoint"] = ep["id"]
        stack["endpoint_name"] = ep["name"]
    return sorted(stacks.values(), key=lambda s: s["name"])


def _fetch(ep: dict) -> Dict[str, Any]:
    result = _run(ep, _list_command(ep), LIST_TIMEOUT_S)
    if not result["ok"]:
        log.warning("dockge: listing %s failed: %s", ep["id"], result["stderr"][:200])
        return {"stacks": [], "error": "Host unreachable or Docker unavailable"}
    try:
        return {"stacks": _parse_listing(ep, result["stdout"]), "error": ""}
    except (ValueError, TypeError, AttributeError):
        log.exception("dockge: unexpected listing from %s", ep["id"])
        return {"stacks": [], "error": "Unexpected response from Docker"}


def _snapshot(ep: dict, force: bool = False) -> Dict[str, Any]:
    """Return the cached listing, refreshing once per endpoint at a time."""
    eid = ep["id"]
    with _lock:
        lock = _refresh_locks.setdefault(eid, threading.Lock())
    with lock:
        now = time.monotonic()
        with _lock:
            cached = _cache.get(eid)
        if cached and not force and now < cached["expires"]:
            return cached
        data = _fetch(ep)
        ttl = LIST_ERROR_TTL_S if data["error"] else LIST_TTL_S
        data["expires"] = time.monotonic() + ttl
        with _lock:
            _cache[eid] = data
        return data


def _snapshots() -> List[Tuple[dict, Dict[str, Any]]]:
    eps = _endpoints()
    if not eps:
        return []
    with ThreadPoolExecutor(max_workers=len(eps)) as pool:
        return list(zip(eps, pool.map(_snapshot, eps)))


def list_stacks() -> List[dict]:
    """Return the stacks of every reachable endpoint."""
    if not dockge_enabled():
        return []
    return [dict(s) for _, snap in _snapshots() for s in snap["stacks"]]


def list_endpoints() -> List[dict]:
    """Return ``{id, name, url, online, error}`` for each endpoint."""
    if not dockge_enabled():
        return []
    return [{"id": ep["id"], "name": ep["name"], "url": ep["url"],
             "online": not snap["error"], "error": snap["error"]}
            for ep, snap in _snapshots()]


# ── Control ───────────────────────────────────────────────────────────────────


def cap_target(endpoint_id: str, stack: str) -> str:
    """Capability target of a stack (same shape as ``instance:vmid``)."""
    return f"{endpoint_id}:{stack}"


def task_name(endpoint_id: str, stack: str) -> str:
    return f"stack_{endpoint_id}_{stack}"


def _resolve(endpoint_id: str, stack: str,
             force: bool) -> Tuple[Optional[dict], Optional[dict], Optional[Tuple[dict, int]]]:
    """Return ``(endpoint, stack record, error response)`` for a control request."""
    if not dockge_enabled():
        return None, None, ({"error": "Dockge is disabled"}, 404)
    ep = _find_endpoint(endpoint_id)
    if ep is None or not _STACK_RE.fullmatch(stack):
        return None, None, ({"error": "Stack not found"}, 404)
    snap = _snapshot(ep, force=force)
    if snap["error"]:
        return None, None, ({"success": False, "error": snap["error"]}, 502)
    record = next((s for s in snap["stacks"] if s["name"] == stack), None)
    if record is None:
        return None, None, ({"error": "Stack not found"}, 404)
    if not record["managed"]:
        return None, None, ({"error": f"{stack} is outside the stacks directory"}, 403)
    return ep, record, None


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1][:300] if lines else "docker compose failed"


def _run_action(ep: dict, stack: str, action: str, steps: List[str], key: str) -> None:
    name = task_name(ep["id"], stack)
    try:
        for args in steps:
            tasks.set_task_status(name, "running", f"docker compose {args.split()[0]}")
            result = _run(ep, _compose(ep, stack, args), ACTION_TIMEOUT_S)
            if not result["ok"]:
                detail = _last_line(result["stderr"])
                log.warning("dockge: %s %s/%s failed: %s", action, ep["id"], stack, detail)
                tasks.set_task_status(name, "error", f"{action} failed: {detail}")
                return
        tasks.set_task_status(name, "done", f"{stack}: {action} finished")
    except Exception:
        log.exception("dockge: %s %s/%s crashed", action, ep["id"], stack)
        tasks.set_task_status(name, "error", f"{action} failed")
    finally:
        with _lock:
            _busy.discard(key)
            _cache.pop(ep["id"], None)


def start_action(endpoint_id: str, stack: str, action: str,
                 user: str = "") -> Tuple[dict, int]:
    """Validate and launch *action* in the background; poll ``/api/task/<task>``."""
    if action not in ACTIONS:
        return {"error": "Unknown action"}, 400
    ep, record, err = _resolve(endpoint_id, stack, force=True)
    if err:
        return err
    assert ep is not None and record is not None

    steps = list(_ACTION_STEPS[action])
    if action == "update" and record["status"] not in ("running", "partial"):
        steps = ["pull"]  # refresh images without starting a stopped stack

    key = cap_target(endpoint_id, stack)
    with _lock:
        if key in _busy:
            return {"success": False, "error": f"{stack} is busy"}, 409
        _busy.add(key)

    name = task_name(endpoint_id, stack)
    tasks.set_task_status(name, "running", _ACTION_LABELS[action])
    log.info("dockge: %s on %s by %s", action, key, user or "?")
    threading.Thread(target=_run_action, args=(ep, stack, action, steps, key),
                     name=f"dockge-{key}", daemon=True).start()
    return {"success": True, "task": name,
            "message": f"{_ACTION_LABELS[action]} {stack}"}, 202


def stack_logs(endpoint_id: str, stack: str, tail: int = LOGS_DEFAULT_TAIL) -> Tuple[dict, int]:
    """Return the last *tail* log lines of a stack."""
    ep, _, err = _resolve(endpoint_id, stack, force=False)
    if err:
        return err
    assert ep is not None
    lines = max(1, min(int(tail), LOGS_MAX_TAIL))
    result = _run(ep, _compose(ep, stack, f"logs --no-color --tail {lines}"), LOGS_TIMEOUT_S)
    if not result["ok"]:
        return {"success": False, "error": _last_line(result["stderr"])}, 502
    return {"success": True, "logs": result["stdout"][-LOGS_MAX_BYTES:]}, 200
