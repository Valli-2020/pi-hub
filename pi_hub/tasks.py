"""pi_hub.tasks — Background task tracking and dual-boot sequences (run in daemon threads)."""
import threading
import time
from typing import Any, Dict, Optional

from pi_hub.config import Host
from pi_hub import net
from pi_hub import hosts

# ── Task names (polled via GET /api/task/{name}) ─────────────────────────────
TASK_BOOT_WINDOWS = "boot_windows"
TASK_BOOT_DEBIAN = "boot_debian"

# ── Timeouts / intervals ─────────────────────────────────────────────────────
BOOT_PING_TIMEOUT_S = 90
BOOT_PING_INTERVAL_S = 5
SHUTDOWN_PING_TIMEOUT_S = 60
SHUTDOWN_PING_INTERVAL_S = 3
SSH_READY_ATTEMPTS = 6
SSH_READY_INTERVAL_S = 5
WOL_SETTLE_DELAY_S = 5

# ── Task record expiry ───────────────────────────────────────────────────────
TASK_EXPIRY_S = 3600  # 1 hour — stale records are dropped on read

_task_lock = threading.Lock()
_tasks: Dict[str, Dict[str, Any]] = {}


# ── Task state ───────────────────────────────────────────────────────────────

def set_task_status(name: str, status: str, message: str = "") -> None:
    """Record the current status of a named background task."""
    with _task_lock:
        _tasks[name] = {"status": status, "message": message, "ts": time.time()}


def get_task_status(name: str) -> Optional[Dict[str, Any]]:
    """Return the last recorded status of a named task, or None.

    Records older than 1 hour are expired so /api/task/<name> cannot
    serve stale 'running' from a previous attempt.
    """
    with _task_lock:
        record = _tasks.get(name)
        if record is None:
            return None
        if time.time() - record["ts"] > TASK_EXPIRY_S:
            del _tasks[name]
            return None
        return record


# ── Ping helpers ─────────────────────────────────────────────────────────────

def _wait_for_ping(ip: str, timeout_s: int, interval_s: int,
                   up: bool = True, icmp: bool = False) -> bool:
    """Poll until the host is (un)reachable; return True if reached in time.

    When *icmp* is True, uses ``net.ping()`` (ICMP echo) — appropriate
    for detecting a Windows host that lacks SSH (dual-boot shutdown
    detection).  When *icmp* is False, uses ``net.tcp_ping()`` (port 22)
    — the canonical liveness check for Debian hosts.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if icmp:
            reachable = net.ping(ip)
        else:
            reachable = net.tcp_ping(ip)
        if reachable == up:
            return True
        time.sleep(interval_s)
    return False


# ── Boot sequences ───────────────────────────────────────────────────────────

def boot_into_windows_sequence(linux_host: Host) -> None:
    """WOL → wait for Debian → grub-reboot Windows → reboot.

    Every code path reaches a terminal status (done / timeout / error).
    """
    tname = TASK_BOOT_WINDOWS
    set_task_status(tname, "running", "WOL sent, waiting for Debian...")

    net.send_wol(linux_host["mac"], linux_host.get("ip", ""))

    # Wait for Debian on TCP port 22 (SSH) — V2: icmp=False
    if not _wait_for_ping(linux_host["ip"], BOOT_PING_TIMEOUT_S,
                          BOOT_PING_INTERVAL_S, up=True, icmp=False):
        set_task_status(tname, "timeout",
                        f"Debian not reachable after {BOOT_PING_TIMEOUT_S}s")
        return

    set_task_status(tname, "running", "Debian online, waiting for SSH...")
    for _ in range(SSH_READY_ATTEMPTS):
        if hosts.ssh_cmd(linux_host, "true"):
            break
        time.sleep(SSH_READY_INTERVAL_S)
    else:
        set_task_status(tname, "timeout", "SSH to Debian not ready")
        return

    set_task_status(tname, "running", "Setting GRUB next-entry, rebooting...")
    entry = hosts.get_grub_entries(linux_host).get("windows", "")
    if not entry:
        set_task_status(tname, "error", "No Windows GRUB entry configured")
        return

    # V2: clear any stale next_entry before setting the new one
    hosts.ssh_cmd(linux_host,
                  f"grub-editenv {hosts.GRUBENV_PATH} unset next_entry 2>/dev/null")
    if not hosts.grub_reboot(linux_host, entry):
        set_task_status(tname, "error", "grub-reboot failed")
        return
    if not hosts.ssh_cmd(linux_host, "reboot"):
        set_task_status(tname, "error", "Reboot command failed")
        return

    set_task_status(tname, "done", "Booting into Windows")


def boot_into_debian_sequence(linux_host: Host, win_host: Host) -> None:
    """Shutdown Windows → poll offline → WOL → boot Debian (GRUB default).

    Every code path reaches a terminal status (done / timeout / error).
    """
    tname = TASK_BOOT_DEBIAN
    set_task_status(tname, "running", "Shutting down Windows...")

    if not hosts.send_shutdown(win_host):
        set_task_status(tname, "error", "SSH shutdown to Windows failed")
        return

    # V2: wait for Windows to go DOWN via ICMP (icmp=True)
    if not _wait_for_ping(win_host["ip"], SHUTDOWN_PING_TIMEOUT_S,
                          SHUTDOWN_PING_INTERVAL_S, up=False, icmp=True):
        set_task_status(tname, "error",
                        f"Windows still online after {SHUTDOWN_PING_TIMEOUT_S}s")
        return

    time.sleep(WOL_SETTLE_DELAY_S)
    set_task_status(tname, "running", "WOL sent, waiting for Debian...")
    net.send_wol(linux_host["mac"], linux_host.get("ip", ""))

    # Wait for Debian on TCP port 22 (SSH) — V2: icmp=False
    if _wait_for_ping(linux_host["ip"], BOOT_PING_TIMEOUT_S,
                      BOOT_PING_INTERVAL_S, up=True, icmp=False):
        set_task_status(tname, "done", "Debian is online")
        return

    set_task_status(tname, "timeout", "Debian not reachable after WOL")


# ── Thread launchers ─────────────────────────────────────────────────────────

def start_boot_windows(linux_host: Host) -> None:
    """Launch the boot-into-Windows sequence in a background thread."""
    threading.Thread(target=boot_into_windows_sequence,
                     args=(linux_host,), daemon=True).start()


def start_boot_debian(linux_host: Host, win_host: Host) -> None:
    """Launch the boot-into-Debian sequence in a background thread."""
    threading.Thread(target=boot_into_debian_sequence,
                     args=(linux_host, win_host), daemon=True).start()
