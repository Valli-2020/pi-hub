"""pi_hub.auth — user store, password hashing, sessions, rate limiting, permissions.

Stdlib only. Pure functions (no http imports) so the module is unit-testable
and usable from run.py --create-user and cli.py.

Security invariants:
- passwords stored as PBKDF2-HMAC-SHA256, per-user salt, 600k iterations
- constant-time comparisons (hmac.compare_digest)
- login timing does not reveal whether a username exists (dummy hash)
- sessions are 256-bit random tokens, in-memory, expiring, thread-safe
  (persisted to sessions.json, reloaded at import; restarts keep sessions)
- per-IP login rate limiting (5 fails -> 300s lockout)
- deny-by-default permission classification
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PKG_DIR)
USERS_PATH = os.path.join(_REPO_ROOT, "users.json")

_ALGO = "sha256"
_ITERATIONS = 600_000
_MIN_PASSWORD_LEN = 8
_USERNAME_RE = re.compile(r"[A-Za-z0-9._-]{1,32}")

# ── Capability matrix (v6.1, v7 instance-qualified) ─────────────────────────
# Per-user, per-target permissions.  `caps` maps an action name to a list of
# targets (host ids, or `instance:vmid` for containers since v7).  admin role
# = everything (caps ignored); viewer role = exactly the listed caps; missing
# caps = read-only.
CAP_ACTIONS = ("wake", "shutdown", "reboot", "dualboot", "containers")
# v7 (#14): optional `instance:` qualifier for container targets.
_TARGET_RE = re.compile(r"[A-Za-z0-9._-]{1,64}(:[A-Za-z0-9._-]{1,64})?")

# Set True once users.json has been migrated to instance-qualified container
# targets (caps_version 2).  After that, colon-less container targets are
# rejected at write time (no positional default fallback — #3).
_caps_v2 = False

# ── Password hashing ─────────────────────────────────────────────────────────


def hash_password(password: str) -> str:
    """Return a versioned PBKDF2 hash string (per-call random salt)."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"), salt, _ITERATIONS)
    return "pbkdf2${}${}${}${}".format(
        _ALGO,
        _ITERATIONS,
        base64.b64encode(salt).decode(),
        base64.b64encode(dk).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time password check; False on any malformed input."""
    try:
        prefix, algo, iters, salt_b64, hash_b64 = stored.split("$", 4)
        if prefix != "pbkdf2" or algo != _ALGO:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        iters = int(iters)
        if not 1000 <= iters <= 2_000_000:      # corrupt iters must not hang a worker
            return False
        dk = hashlib.pbkdf2_hmac(_ALGO, password.encode("utf-8"), salt, iters)
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# ── users.json store ─────────────────────────────────────────────────────────

_users_lock = threading.Lock()


def load_users() -> dict:
    """Return {username: {role, hash, created, updated}} (empty on error).

    NOTE: does NOT distinguish "no users" from "corrupt store" — callers
    that gate security decisions on emptiness MUST use users_state().
    """
    try:
        with open(USERS_PATH) as f:
            data = json.load(f)
        return data.get("users", {}) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def users_state() -> str:
    """Return 'missing' | 'empty' | 'ok' | 'error' for the users store (v7 #2).

    Security-critical distinction: a CORRUPT users.json must fail CLOSED
    (503), never be treated as "no users" (which would arm bootstrap).
    """
    try:
        with open(USERS_PATH) as f:
            data = json.load(f)
        users = data.get("users", {}) if isinstance(data, dict) else {}
        return "empty" if not users else "ok"
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError):
        return "error"


def save_users(users: dict, meta: dict | None = None) -> None:
    """Atomic 0600 write; callers hold _users_lock.  *meta* adds store-level
    keys (e.g. caps_version) next to "users".

    v7 #3: the caps_version stamp is PRESERVED across plain writes — a
    set_caps/add_user/delete_user without meta must never drop it, or the
    one-shot caps migration would re-run on the next boot and re-point
    legacy targets at whatever instance is first in the list.
    """
    _, existing_meta = _read_store()
    payload: dict = {"users": users}
    if meta:
        payload.update(meta)
    if "caps_version" in existing_meta and "caps_version" not in payload:
        payload["caps_version"] = existing_meta["caps_version"]
    tmp = USERS_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # 0600 from birth
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, USERS_PATH)


def _read_store() -> tuple[dict, dict]:
    """Return (users, meta) from users.json; ({}, {}) on any error."""
    try:
        with open(USERS_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}, {}
        return data.get("users", {}), {k: v for k, v in data.items() if k != "users"}
    except (OSError, ValueError):
        return {}, {}


def _normalize_caps(caps, require_instance: bool) -> tuple[dict, list]:
    """Validate a caps dict → (clean, dropped_targets).

    Unknown actions and malformed targets are dropped (deny-by-default).
    Accepts None / non-dict → ({}, []).
    """
    if not isinstance(caps, dict):
        return {}, []
    out: dict = {}
    dropped: list = []
    for action, targets in caps.items():
        if action not in CAP_ACTIONS:
            continue
        if not isinstance(targets, (list, tuple, set)):
            continue
        clean: list = []
        for t in targets:
            if not isinstance(t, str) or not _TARGET_RE.fullmatch(t):
                dropped.append(t)
                continue
            # v7 (#3): after migration, container targets MUST be
            # instance-qualified — colon-less ones are dropped, never
            # positionally mapped to the default instance.
            if require_instance and action == "containers" and ":" not in t:
                dropped.append(t)
                continue
            clean.append(t)
        clean = sorted(set(clean))
        if clean:
            out[action] = clean
    return out, dropped


def normalize_caps(caps) -> dict:
    """Validate a caps dict; return a clean {action: [targets]} or {}."""
    clean, _ = _normalize_caps(caps, _caps_v2)
    return clean


def migrate_caps(default_instance_id: str) -> None:
    """One-shot users.json migration to instance-qualified container targets.

    Rewrites legacy ``"102"`` → ``"<default_id>:102"`` and stamps
    ``caps_version: 2``.  Idempotent (guarded by the store's caps_version);
    afterwards the module rejects colon-less container targets (#3).
    """
    global _caps_v2
    with _users_lock:
        users, meta = _read_store()
        if meta.get("caps_version", 1) >= 2:
            _caps_v2 = True
            return
        if users_state() == "error":
            # never clobber a corrupt store from the migration path
            return
        changed = False
        for rec in users.values():
            caps = rec.get("caps") or {}
            for action, targets in list(caps.items()):
                if action != "containers" or not isinstance(targets, list):
                    continue
                new = []
                for t in targets:
                    if isinstance(t, str) and ":" not in t:
                        new.append(f"{default_instance_id}:{t}")
                        changed = True
                    else:
                        new.append(t)
                if new:
                    caps[action] = sorted(set(new))
                else:
                    caps.pop(action, None)
            if caps:
                rec["caps"] = caps
        # Stamp caps_version: 2 ALWAYS (even with no users yet, e.g. a
        # fresh install where the first bootstrap has not run) so the
        # colon-less reject rule survives process restarts — a version-1
        # store would re-arm it on the next boot.
        save_users(users, {"caps_version": 2})
        _caps_v2 = True


def add_user(username: str, password: str, role: str = "viewer",
             caps: dict | None = None) -> dict:
    """Create a user. Returns {ok, error?} — never raises."""
    username = username.strip()
    if not _USERNAME_RE.fullmatch(username):
        # strict: usernames are URL path segments (DELETE /api/auth/users/<name>)
        return {"ok": False, "error": "invalid username (letters, digits, . _ - only)"}
    if role not in ("admin", "viewer"):
        return {"ok": False, "error": "role must be admin or viewer"}
    if len(password) < _MIN_PASSWORD_LEN:
        return {"ok": False, "error": "password must be at least 8 characters"}
    pwhash = hash_password(password)          # ~0.5s on a Pi — hash OUTSIDE the lock
    clean_caps = normalize_caps(caps)
    with _users_lock:
        users = load_users()
        if username in users:
            return {"ok": False, "error": "user already exists"}
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rec = {"role": role, "hash": pwhash,
               "created": now, "updated": now}
        if clean_caps:
            rec["caps"] = clean_caps
        users[username] = rec
        save_users(users)
    return {"ok": True}


def set_caps(username: str, caps: dict | None) -> dict:
    """Replace a user's capability matrix.  Admin-only at the route level.

    Returns ``{"ok": True, "dropped": [...]}`` — dropped targets (invalid,
    or colon-less container targets after v7 migration) are surfaced so the
    UI can warn instead of silently succeeding (#14).
    """
    with _users_lock:
        users = load_users()
        if username not in users:
            return {"ok": False, "error": "user not found"}
        clean, dropped = _normalize_caps(caps, _caps_v2)
        if clean:
            users[username]["caps"] = clean
        else:
            users[username].pop("caps", None)
        users[username]["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_users(users)
    # caps changed → kick sessions so the change applies immediately
    revoke_user_sessions(username)
    return {"ok": True, "dropped": dropped}


def bootstrap_admin(username: str, password: str) -> dict:
    """Create the FIRST user (admin) — only while the store is missing/empty.

    Locked check-then-create: two concurrent bootstraps with different
    usernames cannot both succeed (#5).  Password hashing happens OUTSIDE
    the lock (600k PBKDF2 iterations must not serialize the store).  The
    route is responsible for rate limiting (CPU DoS guard).
    """
    username = username.strip()
    if not _USERNAME_RE.fullmatch(username):
        return {"ok": False, "error": "invalid username (letters, digits, . _ - only)"}
    if len(password) < _MIN_PASSWORD_LEN:
        return {"ok": False, "error": "password must be at least 8 characters"}
    pwhash = hash_password(password)          # ~0.5s on a Pi — hash OUTSIDE the lock
    with _users_lock:
        state = users_state()
        if state == "error":
            # corrupt store must never be clobbered by an unauthenticated write (#2)
            return {"ok": False, "error": "users store is corrupt — fix users.json"}
        if state == "ok":
            return {"ok": False, "error": "users already exist"}
        users = load_users()                  # {} for missing/empty states
        if username in users:
            return {"ok": False, "error": "user already exists"}
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # role is FORCED to admin server-side; body role/caps are ignored (#5)
        users[username] = {"role": "admin", "hash": pwhash,
                           "created": now, "updated": now}
        # stamp caps_version with the first user so the instance-qualified
        # cap rule is active even if migrate_caps never saw users (#3)
        save_users(users, {"caps_version": 2})
    return {"ok": True}


def delete_user(username: str) -> dict:
    """Delete a user; refuse to remove the last admin."""
    with _users_lock:
        users = load_users()
        if username not in users:
            return {"ok": False, "error": "user not found"}
        if users[username]["role"] == "admin" and \
                sum(1 for u in users.values() if u["role"] == "admin") <= 1:
            return {"ok": False, "error": "cannot delete the last admin"}
        del users[username]
        save_users(users)
    revoke_user_sessions(username)
    return {"ok": True}


def strip_caps_target(target: str) -> list:
    """Remove *target* from every user's caps; return affected usernames.

    Kicks the affected users' sessions (v7 #15) so a deleted host id cannot
    silently resurrect old grants when the id is re-used.
    """
    affected: list = []
    with _users_lock:
        users = load_users()
        for name, rec in users.items():
            caps = rec.get("caps") or {}
            changed = False
            for action, targets in list(caps.items()):
                if isinstance(targets, list) and target in targets:
                    caps[action] = [t for t in targets if t != target]
                    changed = True
                    if not caps[action]:
                        caps.pop(action)
            if changed:
                rec["caps"] = caps or None
                if not caps:
                    rec.pop("caps", None)
                affected.append(name)
        if affected:
            save_users(users)
    for name in affected:
        revoke_user_sessions(name)
    return affected


def strip_caps_prefix(prefix: str) -> list:
    """Remove every cap target starting with *prefix* (e.g. 'keller:').

    Used when a Proxmox instance is deleted — instance-qualified container
    caps must not survive the instance (v7 #3/#15).
    """
    affected: list = []
    with _users_lock:
        users = load_users()
        for name, rec in users.items():
            caps = rec.get("caps") or {}
            changed = False
            for action, targets in list(caps.items()):
                if not isinstance(targets, list):
                    continue
                kept = [t for t in targets if not (isinstance(t, str) and t.startswith(prefix))]
                if len(kept) != len(targets):
                    caps[action] = kept
                    changed = True
                    if not kept:
                        caps.pop(action)
            if changed:
                rec["caps"] = caps or None
                if not caps:
                    rec.pop("caps", None)
                affected.append(name)
        if affected:
            save_users(users)
    for name in affected:
        revoke_user_sessions(name)
    return affected


def change_password(username: str, password: str, by: str) -> dict:
    if len(password) < _MIN_PASSWORD_LEN:
        return {"ok": False, "error": "password must be at least 8 characters"}
    with _users_lock:
        users = load_users()
        if username not in users:
            return {"ok": False, "error": "user not found"}
        users[username]["hash"] = hash_password(password)
        users[username]["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_users(users)
    if by != username:          # admin reset → kick the user's sessions
        revoke_user_sessions(username)
    return {"ok": True}


def authenticate(username: str, password: str) -> dict | None:
    """Return the user record on success, None on failure.

    TIMING-SAFE: unknown usernames run a PBKDF2 against a dummy hash so
    response time does not reveal whether the account exists.
    """
    user = load_users().get(username.strip())
    if not user:
        verify_password(password, _DUMMY_HASH)
        return None
    if not verify_password(password, user.get("hash", "")):
        return None
    return user


_DUMMY_HASH = hash_password("x" * 32)   # module-level, computed once


# ── Sessions (persistent, 7 days) ───────────────────────────────────────────

SESSIONS_PATH = os.path.join(_REPO_ROOT, "sessions.json")
SESSION_DAYS = 7
_session_file_lock = threading.Lock()   # guards file I/O (atomic writes)

_sessions: dict = {}                    # sha256(token) -> {user, role, expires}
_sessions_lock = threading.Lock()


def _token_key(token: str) -> str:
    """Hash the token so the sessions file is useless if stolen."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load_sessions() -> dict:
    """Load persisted sessions from disk (drops expired)."""
    try:
        with open(SESSIONS_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        now = time.time()
        return {k: v for k, v in data.items()
                if isinstance(v, dict) and v.get("expires", 0) > now}
    except (OSError, ValueError):
        return {}


def _save_sessions(sessions: dict) -> None:
    """Atomic 0600 write of the session store."""
    with _session_file_lock:
        tmp = SESSIONS_PATH + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(sessions, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SESSIONS_PATH)


# Load persisted sessions at import time (survives server restarts)
_sessions = _load_sessions()


def issue_session(username: str, role: str) -> str:
    token = secrets.token_urlsafe(32)
    with _sessions_lock:
        _sessions[_token_key(token)] = {
            "user": username, "role": role,
            "expires": time.time() + SESSION_DAYS * 86400,
        }
        _save_sessions(_sessions)
    return token


def validate_token(token: str) -> dict | None:
    """Return {user, role} or None (unknown/expired)."""
    if not token:
        return None
    key = _token_key(token)
    with _sessions_lock:
        s = _sessions.get(key)
        if not s:
            return None
        if s["expires"] < time.time():
            del _sessions[key]
            _save_sessions(_sessions)
            return None
        return {"user": s["user"], "role": s["role"]}


def revoke_session(token: str) -> None:
    key = _token_key(token)
    with _sessions_lock:
        if _sessions.pop(key, None) is not None:
            _save_sessions(_sessions)


def revoke_user_sessions(username: str) -> None:
    with _sessions_lock:
        keys = [k for k, s in _sessions.items() if s.get("user") == username]
        for k in keys:
            del _sessions[k]
        if keys:
            _save_sessions(_sessions)


def sweep_sessions() -> None:
    """Drop expired sessions (daemon thread, hourly)."""
    now = time.time()
    with _sessions_lock:
        keys = [k for k, s in _sessions.items() if s["expires"] < now]
        for k in keys:
            del _sessions[k]
        if keys:
            _save_sessions(_sessions)
    # Also forget rate-limit entries that expired long ago (lazy prune).
    with _rate_lock:
        stale = [ip for ip, a in _attempts.items()
                 if now - a.get("last_fail", 0) > LOCKOUT_SECONDS]
        for ip in stale:
            del _attempts[ip]


# ── Rate limiting (per-IP) ───────────────────────────────────────────────────

_attempts: dict = {}          # ip -> {"fails", "locked_until", "last_fail"}
_rate_lock = threading.Lock()
MAX_FAILS = 5
LOCKOUT_SECONDS = 300


def check_rate_limit(ip: str) -> bool:
    """True if the IP may attempt a login."""
    with _rate_lock:
        a = _attempts.get(ip)
        if not a:
            return True
        now = time.time()
        if a["locked_until"] > now:
            return False
        if now - a.get("last_fail", 0) > LOCKOUT_SECONDS:
            del _attempts[ip]            # forget stale entries
        return True


def record_failure(ip: str) -> None:
    with _rate_lock:
        a = _attempts.setdefault(ip, {"fails": 0, "locked_until": 0.0, "last_fail": 0.0})
        a["fails"] += 1
        a["last_fail"] = time.time()
        if a["fails"] >= MAX_FAILS:
            a["locked_until"] = time.time() + LOCKOUT_SECONDS
            a["fails"] = 0


def record_success(ip: str) -> None:
    with _rate_lock:
        _attempts.pop(ip, None)


# ── Permission matrix (deny-by-default) ──────────────────────────────────────

PUBLIC_GET = {"/api/health"}
PUBLIC_POST = {"/api/auth/login", "/api/auth/bootstrap"}


def classify(method: str, path: str) -> str:
    """Return 'public' | 'read' | 'admin' (deny-by-default).

    public: no auth.  read: any authenticated user.  admin: admin role only.
    POST host/proxmox actions are 'read' here — the fine-grained per-target
    capability check (can_act) happens in the route handlers.
    """
    if method == "GET" and path in PUBLIC_GET:
        return "public"
    if method == "POST" and path in PUBLIC_POST:
        return "public"
    if path.startswith("/api/auth/users"):
        # list/create/delete/caps — admin; password change — admin OR self (route checks)
        if method == "POST" and path.endswith("/password"):
            return "read"
        return "admin"
    if method == "POST" and path == "/api/auth/logout":
        return "read"                              # any authenticated user may log out
    # v7 (#1): /api/config/* is admin-only.  Bare /api/config stays 'read' —
    # it serves public_config() and must remain readable by any logged-in
    # viewer.  POST/DELETE on /api/config/* fall through to 'admin' anyway.
    if path.startswith("/api/config/"):
        return "admin"
    if path.startswith("/api/update/"):
        # Update system is admin-only: the Settings UI surface is admin-only
        # and forcing a release check must not be triggerable by viewers
        # (shared unauthenticated GitHub rate-limit budget).
        return "admin"
    if path.startswith("/api/plugin/"):
        # Plugins handle their own fine-grained caps in their route handlers.
        # Any authenticated user may reach a plugin route; the plugin checks
        # the per-route caps list against the session.
        return "read" if method == "GET" else "read"
    if method == "GET":
        return "read"                              # all other GETs require login
    if method == "POST" and (
        path.startswith("/api/hosts/")
        # v7 (#8): instanced route /api/proxmox/<instance>/container/<vmid>/<action>
        or (path.startswith("/api/proxmox/") and "/container/" in path)
    ):
        return "read"                              # authenticated; caps checked in route
    return "admin"                                 # PUT/DELETE/...: deny-by-default


def can(user: dict | None, needed: str) -> bool:
    if needed == "public":
        return True
    if not user:
        return False
    if needed == "read":
        return True
    if needed == "admin":
        return user.get("role") == "admin"
    return False


def fresh_user(username: str) -> dict | None:
    """Load the CURRENT user record from users.json (role + caps).

    Guards refresh from this instead of trusting the session snapshot so
    role/caps changes apply immediately (no re-login needed).
    """
    return load_users().get(username)


def can_act(session: dict | None, action: str, target: str) -> bool:
    """Per-target capability check: does *session* allow *action* on *target*?

    admin → always True (caps ignored).  viewer → target must be listed in
    the user's caps[action].  No session / no record → False (deny-by-default).
    """
    if not session:
        return False
    if session.get("role") == "admin":
        return True
    rec = fresh_user(session.get("user", ""))
    if not rec:
        return False
    if rec.get("role") == "admin":
        return True
    caps = rec.get("caps") or {}
    return isinstance(caps.get(action), list) and target in caps[action]
