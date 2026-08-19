"""pi_hub.scanner — service discovery for the Pi Hub web UI (v7).

Finds candidate services from LOCAL, hardcoded sources only:

- ``nginx-streams``: the Pi's ``/etc/nginx/streams.conf`` (TCP proxy map —
  the real service topology).  Read with a plain ``open()`` first; falls
  back to ``sudo -n cat`` (narrow privilege, still hardcoded path).
- ``dockge-local``: ``/opt/stacks/*/compose.y*ml`` on the Pi.
- ``dockge-ssh``: the same scan on a Proxmox host, reached via SSH.  The
  target host MUST exist in the config host registry (``find_host_by_id``)
  — arbitrary hosts from config are rejected (#4).

Security invariants:
- Paths are HARDCODED here (STATIC_ALLOWLIST pattern).  Config may only
  select among the fixed source TYPES.
- ``subprocess`` calls use argv lists (``shell=False``) with timeouts and
  no shell interpolation — no option injection, no hung sudo/ssh.
- Candidates are GUESSES: every entry carries ``url_guessed: true`` and
  must be explicitly confirmed by an admin before it is added.  Auto-add
  never applies to nginx-stream candidates (they proxy TCP, not HTTP).
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from pi_hub.config import find_host_by_id, get_config

# ── Hardcoded scan sources (v7 #4 — never config-driven paths) ──────────────
NGINX_STREAMS_PATH = "/etc/nginx/streams.conf"
DOCKGE_DIRS = ("/opt/stacks",)
SOURCE_TYPES = frozenset({"nginx-streams", "dockge-local", "dockge-ssh"})

_SSH_TIMEOUT = 5
_SUDO_TIMEOUT = 5

# streams.conf block: `listen <port>;` ... `proxy_pass <target>;`
# (`;` optional — inline blocks are split on `;` before matching)
_LISTEN_RE = re.compile(r"^\s*listen\s+(?:\[[^\]]+\]:)?(?P<port>\d+)\s*(?:udp|ssl|so_keepalive.*)?;?")
_PROXY_PASS_RE = re.compile(r"^\s*proxy_pass\s+(?P<target>[^;]+);?")

# compose.yaml: `  <service>:` at 2-space indent; `ports:` list entries
_COMPOSE_SVC_RE = re.compile(r"^  (?P<name>[A-Za-z0-9._-]+):\s*$")
_COMPOSE_PORTS_RE = re.compile(r"^\s{4,}-\s*[\"']?(?P<port>[\d.:]+)[\"']?\s*$")
_COMPOSE_X_RE = re.compile(r"^  x-[A-Za-z0-9._-]+:\s*$")


def service_endpoint(svc: Dict[str, Any]) -> Tuple[str, int]:
    """Derive (host, port) from a service record (shared with routes.py
    service probing — v7 #21: the scanner dedups against EXISTING services
    with exactly the same parsing the health probe uses).

    Never raises (v7 #6): a malformed port yields (host, 0); https URLs
    default to 443 instead of 80.
    """
    host = str(svc.get("status_host", "") or "")
    port = svc.get("status_port", 0)
    url = str(svc.get("url", ""))
    scheme = url.split("://", 1)[0] if "://" in url else ""
    if not host and url:
        hp = url.split("://")[-1].split("/")[0]
        if ":" in hp:
            h, ps = hp.rsplit(":", 1)
            host = h
            if not port:
                try:
                    port = int(ps)
                except ValueError:
                    return host, 0
        else:
            host = hp
    if not port:
        port = 443 if scheme == "https" else 80
    try:
        # status_port comes from config and may be a hand-typed string.
        # This function is called from the service health probe, so a
        # single bad value must not take down the whole services view.
        return host, int(port)
    except (TypeError, ValueError):
        return host, 0


# ── nginx streams.conf ───────────────────────────────────────────────────────

def _read_nginx_streams() -> List[Dict[str, str]]:
    """Read streams.conf blocks → [{port, target}] ([] on any failure)."""
    text = None
    try:
        with open(NGINX_STREAMS_PATH) as f:
            text = f.read()
    except OSError:
        # root-owned file: narrow sudo fallback (path is hardcoded)
        try:
            r = subprocess.run(
                ["sudo", "-n", "cat", NGINX_STREAMS_PATH],
                capture_output=True, timeout=_SUDO_TIMEOUT, shell=False,
            )
            if r.returncode == 0:
                text = r.stdout.decode("utf-8", "replace")
        except (OSError, subprocess.TimeoutExpired):
            return []
    if not text:
        return []

    blocks: List[Dict[str, str]] = []
    current: Dict[str, str] = {}

    def _consume(seg: str) -> None:
        """Feed one statement (or inline block content) into the parser."""
        nonlocal current
        seg = seg.strip()
        if not seg or seg.startswith(("#", "//")):
            return
        m = _LISTEN_RE.match(seg)
        if m:
            current["port"] = m.group("port")
            return
        m = _PROXY_PASS_RE.match(seg)
        if m:
            current["target"] = m.group("target").strip()

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "//")):
            continue
        # one-line block: `server { listen 8096; proxy_pass X; }`
        if line.startswith("server") and line.endswith("}"):
            inner = line[line.find("{"):].strip("{}").strip()
            block: Dict[str, str] = {}
            old = current
            current = block
            for seg in inner.split(";"):
                _consume(seg)
            if block.get("port") and block.get("target"):
                blocks.append(block)
            current = old
            continue
        if line.startswith("server") and line.endswith("{"):
            current = {}
            continue
        if line == "}":
            if current.get("port") and current.get("target"):
                blocks.append(current)
            current = {}
            continue
        _consume(line)
    # trailing unclosed block
    if current.get("port") and current.get("target"):
        blocks.append(current)
    return blocks


# ── compose.yaml (Dockge) ────────────────────────────────────────────────────

def _parse_compose(text: str, base: str) -> List[Dict[str, Any]]:
    """Narrow regex YAML extraction (v7 #22): top-level ``services:`` keys
    and their ``ports:`` entries.  Any ambiguity → skip silently."""
    out: List[Dict[str, Any]] = []
    in_services = False
    current: Dict[str, Any] | None = None
    in_ports = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _COMPOSE_X_RE.match(line):
            in_services, in_ports = False, False
            current = None
            continue
        if re.match(r"^services:\s*$", line):
            in_services, in_ports = True, False
            continue
        if not in_services:
            continue
        if line.startswith("  ") and not line.startswith("    "):
            m = _COMPOSE_SVC_RE.match(line)
            if m:
                if current and current["name"] and current["port"]:
                    out.append(current)
                current = {"name": m.group("name"), "port": None,
                           "file": os.path.basename(base)}
                in_ports = False
                continue
            if re.match(r"^  [A-Za-z0-9._-]+:\s*$", line) is None and \
                    not re.match(r"^\s{4,}", line):
                # another top-level key (version, networks, volumes…)
                in_services, in_ports = False, False
                current = None
                continue
        if current is not None and re.match(r"^\s{4,}ports:\s*$", line):
            in_ports = True
            continue
        if current is not None and in_ports:
            m = _COMPOSE_PORTS_RE.match(line)
            if m and current["port"] is None:
                port = _host_port(m.group("port"))
                if port:
                    current["port"] = port
            continue
    if current and current["name"] and current["port"]:
        out.append(current)
    return out


def _host_port(spec: str) -> Optional[int]:
    """Extract the HOST port from a compose port spec (v7 #22):
    ``8080:80`` → 8080; ``127.0.0.1:8080:80`` → 8080; ``8080`` → 8080."""
    parts = spec.split(":")
    try:
        if len(parts) == 3:
            return int(parts[1])
        if len(parts) == 2:
            return int(parts[0])
        if len(parts) == 1:
            return int(parts[0])
    except ValueError:
        return None
    return None


def _scan_compose_dir(directory: str) -> List[Dict[str, Any]]:
    """Scan *directory* for compose.y*ml files (bounded, hardcoded base)."""
    out: List[Dict[str, Any]] = []
    if not os.path.isdir(directory):
        return out
    try:
        entries = sorted(os.listdir(directory))[:64]  # cap file count (#22)
        for entry in entries:
            if entry.startswith("."):
                continue
            path = os.path.join(directory, entry)
            if not os.path.isdir(path):
                continue
            compose = None
            for candidate in ("compose.yaml", "compose.yml",
                              "docker-compose.yaml", "docker-compose.yml"):
                p = os.path.join(path, candidate)
                if os.path.isfile(p) and os.path.getsize(p) <= 256 * 1024:  # cap size
                    compose = p
                    break
            if not compose:
                continue
            try:
                with open(compose, encoding="utf-8", errors="replace") as f:
                    out.extend(_parse_compose(f.read(), compose))
            except OSError:
                continue
    except OSError:
        return out
    return out


def _ssh_compose_scan(host_ip: str, ssh_user: str = "root") -> List[Dict[str, Any]]:
    """Run the compose scan on a remote host over SSH (argv list, timeout).

    Honours the host's configured ``ssh_user`` — hard-coding root here
    made the scan fail silently on every host that uses a dedicated
    service account.
    """
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", ssh_user or ""):
        return []
    # C4 (review 2026-08-18): ssh concatenates argv with spaces and hands
    # the result to the REMOTE shell — unquoted parens were a remote syntax
    # error (returncode != 0 → empty result, indistinguishable from "nothing
    # found").  The parens are now escaped for the remote shell, and
    # StrictHostKeyChecking is set so a first-contact hostkey doesn't hang
    # or fail silently (find -exec cat {} + needs a valid session).
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             "-o", "StrictHostKeyChecking=accept-new",
             f"{ssh_user}@{host_ip}",
             "find", "/opt/stacks", "-maxdepth", "2", "\\(",
             "-name", "compose.y*ml", "-o", "-name", "docker-compose.y*ml",
             "\\)", "-exec", "cat", "{}", "+"],
            capture_output=True, timeout=_SSH_TIMEOUT, shell=False,
        )
        if r.returncode != 0:
            return []
    except (OSError, subprocess.TimeoutExpired):
        return []
    return _parse_compose(r.stdout.decode("utf-8", "replace"), "ssh:" + host_ip)


# ── Public API ───────────────────────────────────────────────────────────────

def scan_sources() -> List[Dict[str, Any]]:
    """Resolve the configured scan sources (v7 #4).

    Only the fixed TYPES are accepted; ``dockge-ssh`` additionally requires
    a ``host_id`` that resolves through the config host registry.  Returns
    a list of valid source dicts (never raises).
    """
    raw = (get_config().get("scan") or {}).get("sources") or []
    sources: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        raw = []
    for src in raw:
        if not isinstance(src, dict):
            continue
        stype = str(src.get("type", ""))
        if stype not in SOURCE_TYPES:
            continue
        if stype == "dockge-ssh":
            host_id = str(src.get("host_id", "")).strip()
            host = find_host_by_id(host_id)
            ip = str(host.get("ip", ""))
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                continue
            sources.append({"type": stype, "host_id": host_id,
                            "name": str(src.get("name", host_id)) or host_id})
        else:
            sources.append({"type": stype,
                            "name": str(src.get("name", stype)) or stype})
    return sources


def run_scan() -> Dict[str, Any]:
    """Run all configured sources; return candidates (no writes).

    Candidate shape::

        {"name": str|None, "port": int, "url": str, "target": str,
         "host": <source name>, "source": "nginx"|"dockge",
         "url_guessed": true}

    Dedup: against existing services by (host, port) via service_endpoint
    (#21), and against other candidates by (host, port).
    """
    candidates: List[Dict[str, Any]] = []
    seen: set = set()

    # existing services → (host, port) set
    existing: set = set()
    for svc in get_config().get("services", []):
        if isinstance(svc, dict):
            host, port = service_endpoint(svc)
            existing.add((host, port))

    for src in scan_sources():
        stype = src["type"]
        if stype == "nginx-streams":
            pi_host = src.get("name", "nginx")
            for block in _read_nginx_streams():
                port = int(block["port"])
                url = f"http://{_pi_hostname()}:{port}"
                key = (pi_host, port)
                if key in seen or key in existing:
                    continue
                seen.add(key)
                candidates.append({
                    "name": None, "port": port, "url": url,
                    "target": block.get("target", ""), "host": pi_host,
                    "source": "nginx", "url_guessed": True,
                })
        elif stype == "dockge-local":
            for cand in _scan_compose_dir(DOCKGE_DIRS[0]):
                if not cand.get("name") or not cand.get("port"):
                    continue
                key = (src["name"], int(cand["port"]))
                if key in seen or key in existing:
                    continue
                seen.add(key)
                candidates.append({
                    "name": str(cand["name"]), "port": int(cand["port"]),
                    "url": f"http://{_pi_hostname()}:{cand['port']}",
                    "target": str(cand.get("file", "")), "host": src["name"],
                    "source": "dockge", "url_guessed": True,
                })
        elif stype == "dockge-ssh":
            host = find_host_by_id(src.get("host_id", ""))
            ip = str(host.get("ip", ""))
            for cand in _ssh_compose_scan(
                ip, str(host.get("ssh_user", "root") or "root")
            ):
                if not cand.get("name") or not cand.get("port"):
                    continue
                key = (src["name"], int(cand["port"]))
                if key in seen or key in existing:
                    continue
                seen.add(key)
                candidates.append({
                    "name": str(cand["name"]), "port": int(cand["port"]),
                    "url": f"http://{ip}:{cand['port']}",
                    "target": str(cand.get("file", "")), "host": src["name"],
                    "source": "dockge", "url_guessed": True,
                })

    candidates.sort(key=lambda c: (str(c.get("host", "")), c.get("port", 0)))
    return {"success": True, "candidates": candidates}


def _pi_hostname() -> str:
    """Hostname used for candidate URLs (the Pi's own name)."""
    try:
        import socket
        return socket.gethostname()
    except Exception:
        return "localhost"
