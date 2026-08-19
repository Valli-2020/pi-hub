"""pi_hub.server — v5 HTTP server with static serving, ETags, gzip, HTTP/1.1.

PERF B11  — protocol_version = "HTTP/1.1" (connection reuse).
PERF B2/B3 — static assets preloaded at import time; ETag + 304 + gzip.
           ``--dev`` flag re-reads files per request.
SECURITY S6 — statics served from a hardcoded allowlist dict; client-supplied
              paths are NEVER joined onto a directory.
SECURITY S5 — NO ``Access-Control-Allow-Origin: *`` (v5 UI is same-origin).
"""

from __future__ import annotations

import gzip
import hashlib
import json as _json
import os as _os
import sys as _sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from pi_hub import config
from pi_hub import routes

# B1 (review 2026-08-18): sentinel for "413 already sent, abort" — distinct
# from None because ``json.loads(b"null")`` is also None and a literal
# ``null`` body must not be mistaken for a rejected request (that left the
# connection hanging with no response ever written).
_BODY_REJECTED = object()

# ── Paths ────────────────────────────────────────────────────────────────────
_HERE = _os.path.dirname(_os.path.abspath(__file__))
_WEB_DIR = _os.path.join(_os.path.dirname(_HERE), "web")

# ── Dev mode ─────────────────────────────────────────────────────────────────
DEV_MODE = "--dev" in _sys.argv

# ── Static allowlist (SECURITY S6) ───────────────────────────────────────────
STATIC_ALLOWLIST = {"/", "/icons.json", "/logo.svg"}

# Map path → filesystem filename
_PATH_TO_FILE = {
    "/": "index.html",
    "/icons.json": "icons.json",
    "/logo.svg": "logo.svg",
}

# Map path → Content-Type
_CONTENT_TYPES: dict[str, str] = {
    "/": "text/html; charset=utf-8",
    "/icons.json": "application/json",
    "/logo.svg": "image/svg+xml",
}

# Map path → Cache-Control
_CACHE_CONTROL: dict[str, str] = {
    "/": "no-cache",              # revalidate every poll
    "/icons.json": "public, max-age=3600",
    "/logo.svg": "public, max-age=3600",
}

# ── Preloaded static cache (release mode) ────────────────────────────────────
_STATIC_CACHE: dict[str, bytes] = {}
_STATIC_ETAGS: dict[str, str] = {}


def _preload_statics() -> None:
    """Load index.html, icons.json, logo.svg into memory at import time."""
    for path, filename in _PATH_TO_FILE.items():
        filepath = _os.path.join(_WEB_DIR, filename)
        try:
            with open(filepath, "rb") as f:
                data = f.read()
            _STATIC_CACHE[path] = data
            _STATIC_ETAGS[path] = hashlib.sha256(data).hexdigest()[:16]
        except OSError:
            pass


_preload_statics()


def _read_static(path: str) -> bytes | None:
    """Return the static asset bytes for *path*, or *None*.

    In dev mode, re-reads from disk every call.  In release mode,
    returns the preloaded cached copy.
    """
    filename = _PATH_TO_FILE.get(path)
    if not filename:
        return None

    if DEV_MODE:
        try:
            with open(_os.path.join(_WEB_DIR, filename), "rb") as f:
                return f.read()
        except OSError:
            return None

    return _STATIC_CACHE.get(path)


def _etag_for(path: str, data: bytes) -> str:
    """Return the ETag for *path* + *data*.

    In dev mode, computes fresh each call.  In release mode, returns the
    precomputed value.
    """
    if DEV_MODE and data:
        return hashlib.sha256(data).hexdigest()[:16]
    return _STATIC_ETAGS.get(path, "")


def _should_gzip(data: bytes, content_type: str, accept_encoding: str) -> bool:
    """Return True when *data* should be gzip-compressed for the client."""
    if len(data) <= 1024:
        return False
    if "gzip" not in accept_encoding:
        return False
    return content_type.startswith(
        ("text/", "application/json", "image/svg")
    )


# ═══════════════════════════════════════════════════════════════════════════════
# HTTP handler
# ═══════════════════════════════════════════════════════════════════════════════


_SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    # app is deliberately inline (single-file UI); frame-ancestors kills clickjacking
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; "
                               "style-src 'self' 'unsafe-inline'; "
                               "script-src 'self' 'unsafe-inline'; "
                               "connect-src 'self'; frame-ancestors 'none'",
}


def _send_security_headers(self) -> None:  # noqa: N805
    """Attach hardening headers to every response."""
    for k, v in _SECURITY_HEADERS.items():
        self.send_header(k, v)


class Handler(BaseHTTPRequestHandler):
    """Serves static assets at /, /icons.json, /logo.svg and dispatches
    /api/* to :mod:`pi_hub.routes`.
    """

    protocol_version = "HTTP/1.1"  # PERF B11 — connection reuse
    # B1 (review 2026-08-18): a client that sends Content-Length but never
    # delivers the body must not park a handler thread forever — socketserver
    # only applies a timeout when the handler class defines one.
    timeout = 30

    # ── Auth guard (v6) ──────────────────────────────────────────────────

    def _auth_token(self) -> str:
        """Extract the Bearer token from the Authorization header (or '')."""
        ah = self.headers.get("Authorization", "")
        if ah.startswith("Bearer "):
            return ah[len("Bearer "):]
        return ""

    def _guard(self, method: str, path: str) -> tuple[dict | None, tuple[dict, int] | None]:
        """Return (session, None) to proceed, or (None, error_response) to reject.

        CALLER CONTRACT: branch on ``err is not None``.  ``(None, None)`` means
        PUBLIC — the caller still serves the route.  Do NOT reject on
        ``session is None``.
        """
        from pi_hub import auth as auth_mod
        cfg = config.get_config()
        if cfg.get("_load_error"):
            return None, ({"error": "Configuration error"}, 503)
        if not config.auth_enabled():
            return {"user": "local", "role": "admin"}, None
        state = auth_mod.users_state()
        if state == "error":
            # corrupt users.json → fail CLOSED (v7 #2): never arm bootstrap
            return None, ({"error": "users.json is corrupt — fix or delete it"}, 503)
        if state in ("missing", "empty"):
            # first run: ONLY bootstrap may pass; everything else 503
            if method == "POST" and path == "/api/auth/bootstrap":
                return None, None                       # public first-run route
            return None, ({"error": "no users configured — create the first "
                                    "admin via POST /api/auth/bootstrap"}, 503)
        needed = auth_mod.classify(method, path)
        if needed == "public":
            return None, None                     # caller handles public routes
        session = auth_mod.validate_token(self._auth_token())
        if not session:
            return None, ({"error": "Unauthorized"}, 401)          # no/invalid token
        # Refresh role/caps from users.json so changes apply immediately.
        rec = auth_mod.fresh_user(session.get("user", ""))
        if not rec:
            return None, ({"error": "Unauthorized"}, 401)          # user deleted
        session["role"] = rec.get("role", "viewer")
        session["caps"] = rec.get("caps", {})
        if needed == "admin" and session["role"] != "admin":
            return None, ({"error": "Forbidden"}, 403)             # authenticated, wrong role
        return session, None

    def _read_body(self) -> dict[str, Any] | None:
        """Read a JSON body with a 64 KB cap (keep-alive-safe rejection).

        Returns None when the request is rejected (413) — the caller must
        stop processing.  Empty bodies return {}.

        B1 (review 2026-08-18): the rejection signal is a module sentinel
        (``_BODY_REJECTED``), NOT None — ``json.loads(b"null")`` is also
        None, and treating a literal ``null`` body as "413 already sent"
        left the connection hanging with no response ever written.
        """
        try:
            cl = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            cl = -1
        if "Transfer-Encoding" in self.headers or cl < 0 or cl > 65536:
            self.close_connection = True            # don't reuse a desynced connection
            self._json({"error": "Payload too large"}, 413)
            return _BODY_REJECTED
        if cl == 0:
            return {}
        try:
            return _json.loads(self.rfile.read(cl))
        except (_json.JSONDecodeError, ValueError):
            return {}

    def _drain_body(self) -> bool:
        """B2 (review 2026-08-18): read + discard a request body on methods
        that don't use one (GET, DELETE), so the connection stays in sync
        on HTTP/1.1 keep-alive.  A leftover body would otherwise be parsed
        as the next request line (request-smuggling primitive behind a
        connection-pooling reverse proxy).  Oversized bodies close the
        connection instead of reading unbounded data.

        Returns True when the request may continue, False when the caller
        must abort WITHOUT writing a response (the connection is being
        closed).  The caller must NOT gate on ``self.close_connection``
        alone — it is already True for plain ``Connection: close`` /
        HTTP/1.0 requests, where a response is still expected."""
        try:
            cl = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            self.close_connection = True
            return False
        if cl <= 0:
            return True
        if cl > 65536 or "Transfer-Encoding" in self.headers:
            self.close_connection = True
            return False
        try:
            self.rfile.read(cl)
        except OSError:
            self.close_connection = True
            return False
        return True

    # ── GET ───────────────────────────────────────────────────────────────

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = parse_qs(parsed.query)

        # B2: a GET with a body must not desync keep-alive — drain first.
        self._drain_body()

        # SECURITY S6 — serve statics ONLY from the hardcoded allowlist
        if path in STATIC_ALLOWLIST:
            self._serve_static(path)
            return

        # Plugin static files (served read-only from the plugin manager's
        # exact-match map — no path traversal).  Requires an
        # authenticated session; plugin assets are part of the app
        # surface and must not be readable by unauthenticated network
        # clients.
        if path.startswith("/plugin-static/"):
            session, err = self._guard("GET", path)
            if err:
                self._json(*err)
                return
            from pi_hub.plugins import get_manager
            result = get_manager().serve_static(path)
            if result is not None:
                data, content_type = result
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=3600")
                for k, v in _SECURITY_HEADERS.items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self._json({"error": "Not found"}, 404)
            return

        if path.startswith("/api/"):
            session, err = self._guard("GET", path)
            if err:
                self._json(*err)
                return
            data, code = routes.handle_get(path, params, session=session)
            self._json(data, code)
            return

        self._json({"error": "Not found"}, 404)

    # ── POST ──────────────────────────────────────────────────────────────

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        body = self._read_body()
        if body is _BODY_REJECTED:
            return

        if path.startswith("/api/"):
            session, err = self._guard("POST", path)
            if err:
                self._json(*err)
                return
            data, code = routes.handle_post(path, params, body,
                                            session=session,
                                            token=self._auth_token(),
                                            ip=self.client_address[0])
            self._json(data, code)
            return

        self._json({"error": "Not found"}, 404)

    # ── DELETE (v6 — auth user management) ────────────────────────────────

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        # Drain any request body so keep-alive doesn't desync (HTTP/1.1).
        # B2: an unparsable Content-Length previously fell back to 0 and
        # left the body in the buffer — close the connection instead.
        # Gate on the DRAIN OUTCOME, not on self.close_connection (which
        # is already True for Connection: close / HTTP/1.0 requests that
        # still expect a response).
        if not self._drain_body():
            return

        if path.startswith("/api/"):
            session, err = self._guard("DELETE", path)
            if err:
                self._json(*err)
                return
            data, code = routes.handle_delete(path, params,
                                              session=session,
                                              ip=self.client_address[0])
            self._json(data, code)
            return

        self._json({"error": "Not found"}, 404)

    # ── Response helpers ──────────────────────────────────────────────────

    def _serve_static(self, path: str) -> None:
        """Serve a preloaded static asset with ETag, 304, and gzip."""
        data = _read_static(path)
        if data is None:
            self._json({"error": "Not found"}, 404)
            return

        etag = _etag_for(path, data)

        # Honour If-None-Match → 304
        if etag:
            client_etag = self.headers.get("If-None-Match", "")
            if client_etag == f'"{etag}"':
                self.send_response(304)
                self.end_headers()
                return

        content_type = _CONTENT_TYPES.get(path, "application/octet-stream")
        cache_control = _CACHE_CONTROL.get(path, "no-cache")

        accept_encoding = self.headers.get("Accept-Encoding", "")
        if _should_gzip(data, content_type, accept_encoding):
            data = gzip.compress(data)
            content_encoding = "gzip"
        else:
            content_encoding = None

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache_control)
        if etag:
            self.send_header("ETag", f'"{etag}"')
        if content_encoding:
            self.send_header("Content-Encoding", content_encoding)
        _send_security_headers(self)
        # SECURITY S5 — NO Access-Control-Allow-Origin
        self.end_headers()

        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data: Any, code: int = 200) -> None:
        """Serialize *data* as JSON and send with *code*.

        API responses get ``Cache-Control: no-store``; gzip is applied
        when the body exceeds 1 KB and the client advertises gzip in
        Accept-Encoding.
        """
        body = _json.dumps(data).encode()

        accept_encoding = self.headers.get("Accept-Encoding", "")
        if _should_gzip(body, "application/json", accept_encoding):
            body = gzip.compress(body)
            content_encoding = "gzip"
        else:
            content_encoding = None

        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if content_encoding:
            self.send_header("Content-Encoding", content_encoding)
        _send_security_headers(self)
        # SECURITY S5 — NO Access-Control-Allow-Origin
        self.end_headers()

        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args: Any) -> None:  # noqa: ARG002
        """Suppress per-request logging (V2 style)."""


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════


def _sweeper() -> None:
    """Hourly expired-session sweep (daemon thread)."""
    from pi_hub import auth as auth_mod
    while True:
        time.sleep(3600)
        auth_mod.sweep_sessions()


def _restart_watcher() -> None:
    """Poll for plugin restart requests.  If a plugin requests a restart,
    exit — systemd's Restart=always picks up the new code."""
    from pi_hub.plugins.manager import get_restart_request
    while True:
        tag = get_restart_request()
        if tag:
            import os as _os, threading as _threading
            print(f"Restart requested by plugin (tag={tag}) — exiting.")
            _threading.Timer(1.0, lambda: _os._exit(0)).start()
            return
        time.sleep(1.0)


def run(bind: str = "0.0.0.0", port: int = 8898) -> None:
    """Start the threaded HTTP server (blocks forever)."""
    from pi_hub import __version__

    cfg = config.get_config()
    if cfg.get("_load_error"):
        print("FATAL: config.json is corrupt or unreadable — refusing to serve "
              "(fail closed). Fix the file and restart.", file=_sys.stderr)
        return

    from pi_hub import auth as auth_mod
    # v7 #3/#5: caps migration must run regardless of how the server was
    # launched (run.py, python -m pi_hub.server, an old unit file, …).
    # Idempotent — guarded by the store's caps_version stamp.
    auth_mod.migrate_caps(config.proxmox_default_instance().get("id", ""))
    if config.auth_enabled():
        n = len(auth_mod.load_users())
        print(f"Auth enabled — {n} user(s)")
        if n == 0:
            print("No users configured — create the first admin: "
                  "python3 run.py --create-user <name> --role admin",
                  file=_sys.stderr)
    else:
        if bind not in ("127.0.0.1", "localhost", "::1"):
            print(f"AUTH DISABLED — dashboard is open to anyone who can reach "
                  f"{bind}:{port}", file=_sys.stderr)
    threading.Thread(target=_sweeper, daemon=True).start()

    # Load plugins
    from pi_hub.plugins import get_manager
    get_manager().load_all()

    # Restart-watcher: if a plugin requests a restart (e.g. update apply),
    # exit so systemd Restart=always picks up the new code.
    threading.Thread(target=_restart_watcher, daemon=True).start()

    httpd = ThreadingHTTPServer((bind, port), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        get_manager().unload_all()
        httpd.shutdown()
