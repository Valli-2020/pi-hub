"""pi_hub.hosts — SSH, GRUB, dual-boot state machine.

Imports from ``pi_hub.config`` and ``pi_hub.net``; does NOT reimplement
config loading or network primitives.  Proxmox code lives in
``pi_hub/proxmox.py`` (P4), not here.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from pi_hub.config import Host, find_host_by_id, get_hosts
from pi_hub.net import POOL, parallel_map, ping, send_wol, tcp_ping  # noqa: F401 — re-exported

# ── Constants ─────────────────────────────────────────────────────────────────

SSH_CONNECT_TIMEOUT_S = 5
SSH_CMD_TIMEOUT_S = 15
SSH_OPTS = [
    "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT_S}",
    "-o", "StrictHostKeyChecking=no",
    "-o", "BatchMode=yes",
]

DEFAULT_SSH_USER = "root"
DEFAULT_SHUTDOWN_CMD = "shutdown -h now"
WINDOWS_SHUTDOWN_CMD = 'powershell -Command "Stop-Computer -Force"'

GRUBENV_PATH = "/boot/grub/grubenv"

# Dual-boot states
MACHINE_OFF = "MACHINE_OFF"
DEBIAN_RUNNING = "DEBIAN_RUNNING"
WINDOWS_RUNNING = "WINDOWS_RUNNING"
UNKNOWN = "UNKNOWN"

# ── Memoisation helpers ──────────────────────────────────────────────────────

_grub_next_cache: Dict[str, tuple[float, Optional[str]]] = {}
_grub_next_lock = threading.Lock()
GRUB_NEXT_TTL = 30.0  # seconds

_stale_clear_ts: Dict[str, float] = {}
_stale_clear_lock = threading.Lock()
STALE_CLEAR_COOLDOWN = 300.0  # 5 minutes

PING_RESULT_TIMEOUT = 3.0  # seconds (for dual-boot ICMP futures)


def _host_key(host: Host) -> str:
    """Stable cache key for a host (prefer id, fall back to ip)."""
    return host.get("id") or host.get("ip", "")


# ── SSH helpers ───────────────────────────────────────────────────────────────

_SSH_USER_RE = re.compile(r"[A-Za-z0-9._-]{1,32}")
_SSH_HOST_RE = re.compile(r"[A-Za-z0-9._:-]{1,255}")


def _ssh_target(host: Host) -> Optional[str]:
    """Build the ``user@host`` SSH target for *host*, or None if unusable.

    Both halves are validated.  ``ssh`` reads any argument starting with
    ``-`` as an option, so an ``ssh_user`` of ``-oProxyCommand=…`` would
    turn the target into a local command execution — argument injection,
    not shell injection, and therefore not fixed by quoting.  A strict
    character class is the fix.
    """
    user = str(host.get("ssh_user", DEFAULT_SSH_USER) or DEFAULT_SSH_USER)
    target = str(host.get("ssh_host", host.get("ip", "")) or "")
    if not _SSH_USER_RE.fullmatch(user) or not _SSH_HOST_RE.fullmatch(target):
        return None
    return f"{user}@{target}"


def _ssh_run(
    host: Host,
    cmd: str,
    timeout: int = SSH_CMD_TIMEOUT_S,
) -> Optional["subprocess.CompletedProcess[str]"]:
    """Run *cmd* via SSH in batch mode; return the process or ``None`` on error.

    ``timeout`` is the *total* SSH timeout in seconds.  The short default
    (``SSH_CMD_TIMEOUT_S``) suits power actions and probes; callers that run
    long-lived commands (e.g. ``apt-get upgrade`` inside a container) must
    pass a much larger value — the remote command would otherwise be killed
    mid-transaction, leaving the target in an inconsistent state.
    """
    try:
        target = _ssh_target(host)
        if target is None:
            return None
        return subprocess.run(
            ["ssh"] + SSH_OPTS + [target, cmd],
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None


def ssh_cmd(host: Host, cmd: str) -> bool:
    """Run *cmd* via SSH.  Returns ``True`` on success."""
    r = _ssh_run(host, cmd)
    return r is not None and r.returncode == 0


def ssh_output(host: Host, cmd: str, timeout: int = SSH_CMD_TIMEOUT_S) -> Optional[str]:
    """Run *cmd* via SSH and return stdout, or ``None`` on failure.

    Pass a larger ``timeout`` for commands that may run longer than
    ``SSH_CMD_TIMEOUT_S`` (the default), e.g. package upgrades.
    """
    r = _ssh_run(host, cmd, timeout=timeout)
    if r is not None and r.returncode == 0:
        return r.stdout.strip()
    return None


def ssh_result(
    host: Host,
    cmd: str,
    timeout: int = SSH_CMD_TIMEOUT_S,
) -> Dict[str, Any]:
    """Run *cmd* via SSH and return a structured result.

    Unlike :func:`ssh_output` (which discards everything on a non-zero exit),
    this returns ``stdout``/``stderr``/``returncode`` for either outcome so
    callers can surface the real failure reason.  ``ok`` is ``False`` when
    SSH itself failed (``returncode`` is ``None``) or the exit code is
    non-zero.

    Returns ``{"ok": bool, "stdout": str, "stderr": str, "returncode": int|None}``.
    """
    r = _ssh_run(host, cmd, timeout=timeout)
    if r is None:
        return {"ok": False, "stdout": "", "stderr": "ssh failed", "returncode": None}
    return {
        "ok": r.returncode == 0,
        "stdout": (r.stdout or "").strip(),
        "stderr": (r.stderr or "").strip(),
        "returncode": r.returncode,
    }


def send_shutdown(host: Host) -> bool:
    """Shut down *host* via SSH.  Returns ``True`` on success.

    Windows hosts prefer PowerShell ``Stop-Computer -Force``, falling back
    to the configured ``ssh_shutdown_cmd``.
    """
    if host.get("type") == "windows":
        cmd = host.get("ssh_shutdown_cmd", "")
        if cmd and ("powershell" in cmd.lower() or cmd.startswith("Stop-Computer")):
            return ssh_cmd(host, cmd)
        if ssh_cmd(host, WINDOWS_SHUTDOWN_CMD):
            return True
        return ssh_cmd(host, cmd) if cmd else False
    return ssh_cmd(host, host.get("ssh_shutdown_cmd", DEFAULT_SHUTDOWN_CMD))


# ── ssh_action (S4 — strict allowlist, no caller-string interpolation) ────────

# The ONLY commands reachable via ssh_action.  "resboot" is intentionally
# absent — it was a dead typo in V2 and must not be resurrected.
_SSH_ACTION_CMDS: Dict[str, str] = {
    "shutdown": "poweroff",
    "poweroff": "poweroff",
    "reboot": "reboot",
}


def ssh_action(host_id: str, action: str) -> Dict[str, Any]:
    """Execute a safe SSH action on *host_id*.

    S4: strict allowlist — only ``shutdown``, ``poweroff``, and ``reboot``
    are permitted.  Windows hosts are shut down via ``powershell -Command
    "Stop-Computer -Force"``.  Caller-supplied strings are NEVER interpolated
    into the remote command — the command is always looked up from the
    hard-coded map.
    """
    action_lower = (action or "").lower()
    remote_cmd = _SSH_ACTION_CMDS.get(action_lower)
    if not remote_cmd:
        return {"success": False, "error": f"Unknown action: {action}"}

    host = find_host_by_id(host_id)
    if not host:
        return {"success": False, "error": f"Unknown host: {host_id}"}

    # Windows shutdown: PowerShell Stop-Computer (more reliable over SSH)
    if host.get("type") == "windows" and action_lower in ("shutdown", "poweroff"):
        remote_cmd = host.get("ssh_shutdown_cmd", WINDOWS_SHUTDOWN_CMD)

    if ssh_cmd(host, remote_cmd):
        return {"success": True, "message": f"{action} sent to {host_id}"}
    return {"success": False, "error": f"SSH command failed for {host_id}"}


# ── GRUB helpers ──────────────────────────────────────────────────────────────

def get_grub_entries(host: Host) -> Dict[str, str]:
    """Return the host's GRUB entry map (``grub_entries`` or legacy ``grub_reboot``)."""
    return host.get("grub_entries") or host.get("grub_reboot") or {}


def grub_reboot(host: Host, entry: str) -> bool:
    """Set the GRUB one-shot boot entry via ``grub-reboot``.

    The entry comes from config (``grub_entries``) and is free text — a
    GRUB menu title legitimately contains spaces and parentheses, and may
    contain an apostrophe.  It is therefore quoted with :func:`shlex.quote`
    instead of being pasted between hand-written quotes.
    """
    return ssh_cmd(host, f"grub-reboot {shlex.quote(str(entry))}")


def _grub_get_next_raw(host: Host) -> Optional[str]:
    """Query grubenv for ``next_entry`` (uncached)."""
    return ssh_output(
        host,
        f"grub-editenv {GRUBENV_PATH} list | grep ^next_entry | cut -d= -f2-",
    )


def grub_get_next(host: Host) -> Optional[str]:
    """Return the current ``grubenv`` next_entry, or ``None``.

    PERF B4c — memoised with a 30 s TTL so repeated calls inside the same
    polling window don't each trigger a blocking SSH session.
    """
    now = time.monotonic()
    key = _host_key(host)
    with _grub_next_lock:
        cached = _grub_next_cache.get(key)
        if cached is not None and (now - cached[0]) < GRUB_NEXT_TTL:
            return cached[1]

    result = _grub_get_next_raw(host)
    with _grub_next_lock:
        _grub_next_cache[key] = (time.monotonic(), result)
    return result


def grub_reset(host: Host) -> bool:
    """Clear ``next_entry`` and reset the GRUB default to entry 0."""
    return ssh_cmd(
        host,
        f"grub-set-default 0 && grub-editenv {GRUBENV_PATH}"
        " unset next_entry 2>/dev/null; grub-editenv list",
    )


def grub_clear_stale(host: Host) -> bool:
    """Clear a stale ``next_entry`` if one exists.

    Call this when Debian is detected running to prevent GRUB from
    booting Windows again after an interrupted one-shot switch.

    Returns ``True`` if a stale entry was cleared.
    """
    if grub_get_next(host):
        ssh_cmd(
            host,
            f"grub-editenv {GRUBENV_PATH} unset next_entry && grub-set-default 0",
        )
        # Invalidate the cache so the next read picks up the cleared state.
        key = _host_key(host)
        with _grub_next_lock:
            _grub_next_cache.pop(key, None)
        return True
    return False


def maybe_clear_stale_grub(host: Host) -> bool:
    """Rate-limited version of ``grub_clear_stale``.

    PERF B4b — callers that still want automatic clearing (e.g. task poll
    loops) should use this instead of the unbounded ``grub_clear_stale``.
    Gated at most once per 5 minutes per host.

    Returns ``True`` if clearing ran (even if nothing was stale).
    """
    now = time.monotonic()
    key = _host_key(host)
    with _stale_clear_lock:
        if now - _stale_clear_ts.get(key, 0) < STALE_CLEAR_COOLDOWN:
            return False
    grub_clear_stale(host)
    with _stale_clear_lock:
        _stale_clear_ts[key] = now
    return True


# ── Dual-boot helpers ─────────────────────────────────────────────────────────

def get_peer(host: Host) -> Optional[Host]:
    """Return the dual_boot_peer host object, or ``None``."""
    peer_id = host.get("dual_boot_peer")
    if not peer_id:
        return None
    peer = find_host_by_id(peer_id)
    return peer if peer else None




def get_dualboot_state(host: Host) -> Optional[str]:
    """Detect which OS is running on a dual-boot machine.

    Returns ``MACHINE_OFF``, ``DEBIAN_RUNNING``, ``WINDOWS_RUNNING``,
    ``UNKNOWN``, or ``None`` if the host has no dual-boot peer.

    PERF B4a — the two ICMP pings run concurrently via ``net.POOL``
    instead of serially.

    PERF B4b — this function is **read-only**; it does NOT call
    ``grub_clear_stale``.  Callers that need stale-entry cleanup must
    call ``maybe_clear_stale_grub`` separately.
    """
    peer = get_peer(host)
    if not peer:
        return None

    linux = host if host.get("type") == "linux" else peer
    win = peer if linux is host else host

    linux_ip = linux.get("ip", "")
    win_ip = win.get("ip", "")
    if not linux_ip or not win_ip:
        return UNKNOWN            # incomplete pair — never raise into the caller

    # Concurrent ICMP pings (ICMP is required for Windows detection —
    # Windows firewalls routinely drop TCP/22).
    #
    # A failure here must stay local: this runs inside get_all_status(),
    # so an exception would take down the status of EVERY host, not just
    # this pair.
    f_linux = POOL.submit(ping, linux_ip)
    f_win = POOL.submit(ping, win_ip)

    try:
        linux_up = f_linux.result(timeout=PING_RESULT_TIMEOUT)
        win_up = f_win.result(timeout=PING_RESULT_TIMEOUT)
    except Exception:
        return UNKNOWN

    if not linux_up and not win_up:
        return MACHINE_OFF
    if linux_up and not win_up:
        return DEBIAN_RUNNING
    if win_up and not linux_up:
        return WINDOWS_RUNNING
    return UNKNOWN


# ── Parallel status probe (PERF B6) ───────────────────────────────────────────

# Request-scoped set to track which host IDs have already been probed for
# dual-boot state within the current get_all_status() call (PERF B4d).
_dualboot_state_request_cache: Dict[str, Optional[str]] = {}
_dualboot_state_request_lock = threading.Lock()


def _probe_host_basic(host: Host) -> Dict[str, Any]:
    """Probe a single host for basic liveness (TCP/22).

    Called via ``parallel_map``.  Returns a partial status dict — enriched
    by ``get_all_status`` in a second pass."""
    ip = host.get("ip", "")
    online = tcp_ping(ip) if ip else False
    return {
        "host_id": host.get("id", ip),
        "online": online,
        "name": host.get("name", host.get("id", "")),
        "ip": ip,
        "type": host.get("type", "linux"),
    }


def _build_status(
    host: Host,
    basic: Optional[Dict[str, Any]],
    online_by_id: Dict[str, bool],
) -> Dict[str, Any]:
    """Build the full RICH status record for a single host.

    *basic* is the result from ``_probe_host_basic`` (``None`` on timeout).
    """
    hid = host.get("id", "")
    stale = basic is None
    online = basic["online"] if basic else False

    info: Dict[str, Any] = {
        "online": online,
        "name": host.get("name", hid),
        "ip": host.get("ip", ""),
        "type": host.get("type", "linux"),
    }
    if stale:
        info["stale"] = True

    # ── Capabilities ──────────────────────────────────────────────────
    caps: List[str] = []
    if host.get("mac"):
        caps.append("wake")
    if host.get("type") in ("linux", "windows"):
        caps.append("shutdown")
    if host.get("type") == "linux":
        caps.append("reboot")
        if get_grub_entries(host):
            caps.append("grub_switch")
    peer_id = host.get("dual_boot_peer")
    if peer_id:
        caps.append("dualboot")
    info["capabilities"] = caps

    # ── Dual-boot enrichment ───────────────────────────────────────────
    if peer_id and host.get("type") == "linux":
        info["dual_boot_peer"] = peer_id
        info["peer_id"] = peer_id

        linux_online = online
        windows_online = online_by_id.get(peer_id, False)
        info["linux_online"] = linux_online
        info["windows_online"] = windows_online

        # Dual-boot state: either from request cache or computed fresh.
        # PERF B4d — only compute once per pair per request.
        with _dualboot_state_request_lock:
            state = _dualboot_state_request_cache.get(hid)
        if state is None:
            state = get_dualboot_state(host)
            with _dualboot_state_request_lock:
                _dualboot_state_request_cache[hid] = state
                # Also cache under the peer's id so it isn't recomputed.
                _dualboot_state_request_cache[peer_id] = state
        info["dualboot_state"] = state if state is not None else UNKNOWN

        # GRUB next for online Linux hosts
        if linux_online and get_grub_entries(host):
            info["grub_next"] = grub_get_next(host)

    # Windows side of a pair: hide the card, state is carried by Linux peer.
    if peer_id and host.get("type") == "windows":
        info["hide_card"] = True
        info["dual_boot_peer"] = peer_id
        info["peer_id"] = peer_id

    return info


def get_all_status() -> Dict[str, Dict[str, Any]]:
    """Return live status for every configured host.

    PERF B6 — probes all hosts concurrently via ``net.parallel_map``
    (TCP port 22, not ICMP).  Every configured host is ALWAYS present in
    the result; hosts that time out degrade to ``{online: false, stale: true}``.

    Each record carries the full RICH shape:
      online, name, ip, type, capabilities, dual_boot_peer,
      dualboot_state, grub_next, hide_card,
      + linux_online, windows_online, peer_id for dual-boot pairs.

    PERF B4d — dual-boot state is computed at most once per pair within
    a single request (no duplicate ICMP probing).
    """
    hosts = get_hosts()
    if not hosts:
        return {}

    # Pass 1 — probe all hosts concurrently for TCP/22 liveness
    basic_list = parallel_map(_probe_host_basic, hosts, timeout=2.0)

    # Build online_by_id from pass-1 results
    online_by_id: Dict[str, bool] = {}
    for idx, basic in enumerate(basic_list):
        hid = hosts[idx].get("id", "")
        online_by_id[hid] = basic["online"] if basic else False

    # Clear request-scoped dual-boot state cache (PERF B4d)
    global _dualboot_state_request_cache
    with _dualboot_state_request_lock:
        _dualboot_state_request_cache = {}

    # Pass 2 — build rich status records
    result: Dict[str, Dict[str, Any]] = {}
    for idx, host in enumerate(hosts):
        hid = host.get("id", "")
        result[hid] = _build_status(host, basic_list[idx], online_by_id)

    return result
