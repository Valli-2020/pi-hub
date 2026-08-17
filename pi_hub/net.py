"""pi_hub.net — network primitives: DNS cache, WOL, TCP ping, ICMP ping, parallel map.

Reachability rule
    TCP port 22 is the canonical host-liveness check. ICMP ping (send one
    packet via system `ping`) is used *only* for dual-boot detection, where
    the Windows side of a pair may not run an SSH server.  Call sites must
    not use ICMP for generic "is this host up?" — Windows firewalls commonly
    drop ICMP echo requests, producing false negatives.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence, TypeVar

# ── Constants ─────────────────────────────────────────────────────────────────

PING_COUNT = 1
PING_WAIT_S = 2
PING_TIMEOUT_S = 3

WOL_BROADCAST = "255.255.255.255"
WOL_PORTS = (9, 7)

DNS_TTL = 300.0          # positive cache
DNS_NEGATIVE_TTL = 30.0  # cache a failed lookup

POOL_WORKERS = 16

# ── Perf B10: shared thread pool ─────────────────────────────────────────────

POOL = ThreadPoolExecutor(max_workers=POOL_WORKERS, thread_name_prefix="pihub-net")

T = TypeVar("T")


def parallel_map(
    fn: Callable[[Any], T],
    items: Sequence[Any],
    timeout: float = 5.0,
) -> list[T | None]:
    """Map *fn* over *items* using the shared ``POOL``.

    Returns a list with one entry per input item, preserving order.  Each
    future is given *timeout* seconds; items that time out or raise produce
    ``None``.  No item is silently dropped — the output list always has the
    same length as *items*.
    """
    if not items:
        return []

    futures = {POOL.submit(fn, item): idx for idx, item in enumerate(items)}
    results: list[T | None] = [None] * len(items)

    for fut, idx in futures.items():
        try:
            results[idx] = fut.result(timeout=timeout)
        except Exception:  # noqa: BLE001 — TimeoutError and any fn exception
            results[idx] = None

    return results


# ── DNS cache ────────────────────────────────────────────────────────────────


@dataclass
class _DnsEntry:
    ip: str              # empty string = negative cache
    expires: float = field(default_factory=time.monotonic)


_dns_cache: dict[str, _DnsEntry] = {}
_dns_lock = threading.Lock()


def _invalidate_dns(host: str) -> None:
    """Remove stale entries for *host* from the cache."""
    now = time.monotonic()
    with _dns_lock:
        entry = _dns_cache.get(host)
        if entry and entry.expires < now:
            del _dns_cache[host]


def _resolve(host: str, timeout: float) -> str | None:
    """Resolve *host* to an IPv4 string, with a timeout on DNS lookup.

    Returns ``None`` when resolution fails or times out.  Positive results are
    cached for *DNS_TTL* seconds, and failures for *DNS_NEGATIVE_TTL* seconds.
    The cache is lock-guarded.
    """
    # Fast path: already an IP literal
    try:
        socket.inet_aton(host)
        return host
    except OSError:
        pass

    # Check the guarded cache
    now = time.monotonic()
    with _dns_lock:
        entry = _dns_cache.get(host)
        if entry is not None and entry.expires > now:
            return entry.ip or None  # empty string → negative cache hit

    # Resolve via a daemon thread so we can enforce a timeout on the
    # synchronous ``getaddrinfo`` call.
    result: dict[str, str | None] = {}

    def _worker() -> None:
        try:
            # Request only IPv4 (AF_INET) to stay compatible with the rest of
            # the module which assumes dotted-quad addresses.
            addrs = socket.getaddrinfo(host, None, socket.AF_INET)
            if addrs:
                result["ip"] = str(addrs[0][4][0])
            else:
                result["ip"] = None
        except Exception:  # noqa: BLE001
            result["error"] = None  # sentinel

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(timeout=timeout)

    ip: str | None = result.get("ip")  # may be None (no addr) or missing (timeout/error)

    with _dns_lock:
        if ip:
            _dns_cache[host] = _DnsEntry(ip=ip, expires=now + DNS_TTL)
        else:
            _dns_cache[host] = _DnsEntry(ip="", expires=now + DNS_NEGATIVE_TTL)

    return ip


# ── Wake-on-LAN ──────────────────────────────────────────────────────────────


def subnet_broadcast(ip: str) -> str | None:
    """Derive the /24 subnet broadcast for *ip*, or ``None``."""
    parts = ip.split(".")
    if len(parts) != 4:
        return None
    return ".".join(parts[:3] + ["255"])


def send_wol(mac: str, ip: str = "") -> bool:
    """Send a Wake-on-LAN magic packet.

    Broadcasts to ``255.255.255.255`` on ports 9 **and** 7, plus the host's
    /24 subnet broadcast when *ip* is provided.

    Ported verbatim from V4 ``hosts.py:66-93`` (``bytes.fromhex`` path).
    """
    try:
        mac_bytes = bytes.fromhex(mac.replace(":", "").replace("-", ""))
        if len(mac_bytes) != 6:
            return False
        magic = b"\xff" * 6 + mac_bytes * 16
        addrs = [WOL_BROADCAST]
        if ip:
            sb = subnet_broadcast(ip)
            if sb and sb not in addrs:
                addrs.append(sb)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(PING_TIMEOUT_S)
        try:
            for addr in addrs:
                for port in WOL_PORTS:
                    sock.sendto(magic, (addr, port))
        finally:
            sock.close()
        return True
    except Exception:  # noqa: BLE001
        return False


# ── ICMP ping ────────────────────────────────────────────────────────────────


def ping(ip: str, timeout: float = 2.0) -> bool:
    """Return ``True`` if a single ICMP echo-request succeeds.

    **Do not use this for general liveness checks** — Windows firewalls
    routinely drop ICMP.  Only use this when you need to detect a Windows
    host that lacks SSH (dual-boot detection).
    """
    try:
        r = subprocess.run(
            ["ping", "-c", str(PING_COUNT), "-W", str(PING_WAIT_S), ip],
            capture_output=True,
            timeout=timeout,
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False


# ── TCP ping (DNS-safe) ──────────────────────────────────────────────────────


def tcp_ping(host: str, port: int = 22, timeout: float = 1.5) -> bool:
    """Check whether *host*:*port* accepts a TCP connection.

    Unlike a naive ``socket.create_connection()`` call, this function resolves
    *host* through the lock-guarded module DNS cache and connects to the
    resulting IP literal — so the DNS lookup is bounded by *timeout* instead
    of blocking indefinitely.
    """
    ip = _resolve(host, timeout)
    if not ip:
        return False
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False
