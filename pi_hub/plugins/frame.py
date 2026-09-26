"""Sandboxed JavaScript frames for plugins (Plugin API v2, ``ui.frame``).

A plugin that needs its own JavaScript ships it under
``<plugin>/static/frame/`` and declares a :class:`~pi_hub.plugins.base.FrameDef`.
The dashboard mounts the frame in ``<iframe sandbox="allow-scripts">`` — no
``allow-same-origin`` — so the frame runs in an *opaque origin*: it cannot
read the dashboard's ``localStorage`` (where the admin token lives), cannot
send credentialed requests to Pi Hub, and its CSP has ``connect-src 'none'``.
It talks to the dashboard only through the ``ph`` bridge below, over a
private ``MessageChannel``; the dashboard validates every message.

This module (Python strings only — the 7.7.0 updater cannot ship new web
files) provides:

* :func:`load_frame` — read + validate a frame's files once, at plugin load;
* :func:`build_document` — the HTML the iframe loads, plus its exact CSP;
* one-shot **tickets** (the iframe URL credential) and scoped **frame
  tokens** (what the *dashboard* sends on the frame's behalf, as
  ``Authorization: Frame <token>``);
* :data:`READ_APIS`, the fixed list of core read endpoints a frame may
  request through ``ph.read``.

Trust boundary (see PLUGINS.md): frames stop hostile *data* (hostnames,
SSH usernames, container names…) from becoming code in the dashboard origin.
They do not stop an approved author's code from leaking data the frame was
given — the same trust the plugin's Python already has.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

# Size limits are shared with the store, which enforces them at install time.
MAX_FRAME_FILE = 512 * 1024
MAX_FRAME_TOTAL = 2 * 1024 * 1024
FRAME_EXTS = (".js", ".css", ".svg", ".png", ".json", ".woff2")

#: Core read endpoints a frame may request via ``ph.read(name)``:
#: name → (path, system capability the plugin must hold).
READ_APIS: Dict[str, Tuple[str, str]] = {
    "hosts.status":       ("/api/hosts/status",       "hosts.read"),
    "services.status":    ("/api/status",             "services.read"),
    "proxmox.containers": ("/api/proxmox/containers", "proxmox.read"),
    "dockge.stacks":      ("/api/dockge/stacks",      "dockge.read"),
}
READ_PATHS = {p: name for name, (p, _c) in READ_APIS.items()}

SURFACE_TYPES = ("tab", "widget", "settings")
_FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,127}$")
_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")
_ROUTE_TAIL_RE = re.compile(r"[A-Za-z0-9._:~-]{1,128}(/[A-Za-z0-9._:~-]{1,128}){0,8}")
_MIME = {".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json",
         ".woff2": "font/woff2"}


class FrameError(ValueError):
    """A frame declaration or one of its files is unacceptable."""


# ═══════════════════════════════════════════════════════════════════════════════
# Loading + validation
# ═══════════════════════════════════════════════════════════════════════════════

class LoadedFrame:
    """A validated frame with its files held in memory (fixed at load time)."""

    def __init__(self, plugin: str, spec: Dict[str, Any], scripts: List[str],
                 css: str, assets: Dict[str, str], digest: str):
        self.plugin = plugin
        self.spec = spec              # normalised FrameDef fields
        self.id = spec["id"]
        self.scripts = scripts
        self.css = css
        self.assets = assets          # path → data: URL
        self.digest = digest
        self.blocked = ""             # non-empty → not served (reason)
        self._doc: Optional[Tuple[bytes, str]] = None

    def public(self) -> Dict[str, Any]:
        """What the dashboard needs to mount the frame."""
        s = self.spec
        return {"plugin": self.plugin, "id": self.id, "surfaces": s["surfaces"],
                "height": s["height"], "renders": s["renders"], "reads": s["reads"]}


def _clean_files(kind: str, files: Any, exts: Tuple[str, ...], limit: int) -> List[str]:
    if not isinstance(files, (list, tuple)) or len(files) > limit:
        raise FrameError(f"frame {kind}: expected a list of at most {limit} paths")
    out: List[str] = []
    for f in files:
        if not isinstance(f, str) or not _FILE_RE.fullmatch(f) or ".." in f.split("/") \
                or any(p.startswith(".") or not p for p in f.split("/")):
            raise FrameError(f"frame {kind}: bad path {f!r}")
        if not f.lower().endswith(exts):
            raise FrameError(f"frame {kind}: {f!r} must end in {', '.join(exts)}")
        if f not in out:
            out.append(f)
    return out


def _read(root: str, rel: str, budget: List[int]) -> bytes:
    path = os.path.realpath(os.path.join(root, *rel.split("/")))
    if not path.startswith(root + os.sep):
        raise FrameError(f"frame file escapes static/frame/: {rel}")
    try:
        if not os.path.isfile(path):
            raise FrameError(f"frame file missing: {rel}")
        if os.path.getsize(path) > MAX_FRAME_FILE:
            raise FrameError(f"frame file too large (> {MAX_FRAME_FILE // 1024} KB): {rel}")
        with open(path, "rb") as fh:
            data = fh.read(MAX_FRAME_FILE + 1)
    except OSError as e:
        raise FrameError(f"cannot read frame file {rel}: {e}") from e
    budget[0] += len(data)
    if len(data) > MAX_FRAME_FILE or budget[0] > MAX_FRAME_TOTAL:
        raise FrameError("frame files exceed the size limit")
    return data


def load_frame(plugin: str, plugin_dir: str, fd: Any, granted: set,
               allowed_render_slots: Dict[str, str]) -> LoadedFrame:
    """Validate the :class:`FrameDef` *fd* and read its files.

    *granted* is the plugin's approved capability set; *allowed_render_slots*
    maps every slot a frame may render into → the ``ui.*`` capability it needs.
    Raises :class:`FrameError` on anything unacceptable.
    """
    if "ui.frame" not in granted:
        raise FrameError("frame requires the ui.frame capability")
    fid = getattr(fd, "id", "")
    if not isinstance(fid, str) or not _ID_RE.fullmatch(fid):
        raise FrameError(f"frame id {fid!r} must match [a-z0-9-]{{1,32}}")

    entry = _clean_files("entry", getattr(fd, "entry", None), (".js",), 8)
    if not entry:
        raise FrameError("frame needs at least one entry script")
    css = _clean_files("css", getattr(fd, "css", None) or [], (".css",), 8)
    assets = _clean_files("assets", getattr(fd, "assets", None) or [], tuple(_MIME), 64)

    reads = list(getattr(fd, "reads", None) or [])
    for r in reads:
        if r not in READ_APIS:
            raise FrameError(f"unknown read API {r!r} (known: {', '.join(READ_APIS)})")
        if READ_APIS[r][1] not in granted:
            raise FrameError(f"read {r!r} needs the {READ_APIS[r][1]} capability")

    renders = list(getattr(fd, "renders", None) or [])
    for s in renders:
        need = allowed_render_slots.get(s)
        if need is None:
            raise FrameError(f"frame cannot render into slot {s!r}")
        if need not in granted:
            raise FrameError(f"rendering into {s!r} needs the {need} capability")

    surfaces: List[Dict[str, Any]] = []
    for sf in (getattr(fd, "surfaces", None) or []):
        if not isinstance(sf, dict) or sf.get("type") not in SURFACE_TYPES:
            raise FrameError(f"surface type must be one of {', '.join(SURFACE_TYPES)}")
        t = sf["type"]
        item: Dict[str, Any] = {"type": t}
        if t == "tab":
            label = str(sf.get("label") or fid)[:40]
            item.update(label=label, icon_svg=str(sf.get("icon_svg") or "")[:4000])
        elif t == "widget":
            view = sf.get("view")
            if view not in ("hosts", "services", "containers", "stacks"):
                raise FrameError("widget surface needs view: hosts|services|containers|stacks")
            item["view"] = view
        else:
            item["label"] = str(sf.get("label") or fid)[:40]
        surfaces.append(item)
    if not surfaces:
        raise FrameError("frame declares no surface (tab, widget or settings)")

    height = getattr(fd, "height", 240)
    if not isinstance(height, int) or isinstance(height, bool) or not 40 <= height <= 1200:
        raise FrameError("frame height must be an integer between 40 and 1200")

    root = os.path.realpath(os.path.join(plugin_dir, "static", "frame"))
    budget = [0]
    parts: List[Tuple[str, str, str]] = []           # (kind, path, sha256)
    scripts: List[str] = []
    for f in entry:
        raw = _read(root, f, budget)
        text = raw.decode("utf-8", "strict")
        low = text.lower()
        # These sequences would end or confuse the inline <script> element.
        if "</script" in low or "<!--" in low:
            raise FrameError(f"{f}: must not contain '</script' or '<!--'")
        if "\r" in text:
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        scripts.append(text)
        parts.append(("js", f, hashlib.sha256(raw).hexdigest()))
    css_text = ""
    for f in css:
        raw = _read(root, f, budget)
        text = raw.decode("utf-8", "strict")
        if "</style" in text.lower() or "<!--" in text:
            raise FrameError(f"{f}: must not contain '</style' or '<!--'")
        css_text += text.replace("\r\n", "\n").replace("\r", "\n") + "\n"
        parts.append(("css", f, hashlib.sha256(raw).hexdigest()))
    data_urls: Dict[str, str] = {}
    for f in assets:
        raw = _read(root, f, budget)
        mime = _MIME[os.path.splitext(f)[1].lower()]
        data_urls[f] = "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode())
        parts.append(("asset", f, hashlib.sha256(raw).hexdigest()))

    digest = hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()
    spec = {"id": fid, "surfaces": surfaces, "renders": renders, "reads": reads, "height": height}
    return LoadedFrame(plugin, spec, scripts, css_text, data_urls, digest)


# ═══════════════════════════════════════════════════════════════════════════════
# The document the iframe loads
# ═══════════════════════════════════════════════════════════════════════════════

BASE_CSS = """\
*,*::before,*::after{box-sizing:border-box}
html,body{margin:0;padding:0;background:transparent}
body{display:flow-root}
body{font:13.5px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:var(--text,#1c1d21)}
a{color:var(--accent,#b45309)}
"""

# Runs inside the sandbox.  It is the ONLY code in the frame that touches
# window.parent; the plugin's own scripts get the frozen `ph` object.
BRIDGE_JS = r"""(function(){
'use strict';
var TOP = window.parent, port = null, seq = 0, pending = Object.create(null), outbox = [];
var listeners = { theme: [], visible: [], refresh: [] };
var assets = {};
try { assets = JSON.parse(document.getElementById('ph-assets').textContent || '{}'); } catch (e) {}
var state = { theme: { mode: 'light', vars: {} }, role: '', surface: '', visible: true };

// Messages sent before the dashboard's init arrives (scripts run before the
// iframe's load event) are queued and flushed once the channel is up.
function send(m){
  if (port) { try { port.postMessage(m); } catch (e) {} }
  else if (outbox.length < 100) outbox.push(m);
}
function fire(name, arg){ (listeners[name] || []).slice().forEach(function(f){ try { f(arg); } catch (e) {} }); }
function applyTheme(t){
  if (!t || typeof t !== 'object') return;
  var mode = t.mode === 'dark' ? 'dark' : 'light', vars = {};
  document.documentElement.setAttribute('data-theme', mode);
  document.documentElement.style.colorScheme = mode;
  Object.keys(t.vars || {}).forEach(function(k){
    if (/^--[a-z0-9-]{1,32}$/.test(k) && typeof t.vars[k] === 'string' && t.vars[k].length < 300) {
      vars[k] = t.vars[k]; document.documentElement.style.setProperty(k, t.vars[k]);
    }
  });
  state.theme = { mode: mode, vars: vars };
}
function request(op, payload){
  return new Promise(function(resolve, reject){
    var id = ++seq;
    pending[id] = { resolve: resolve, reject: reject };
    payload.op = op; payload.id = id;
    send(payload);
    setTimeout(function(){ if (pending[id]) { delete pending[id]; reject(new Error('timeout')); } }, 20000);
  });
}
function onPort(ev){
  var m = ev.data;
  if (!m || typeof m !== 'object') return;
  if (m.op === 'ping') { send({ op: 'pong', n: m.n }); return; }
  if (m.op === 'result' && pending[m.id]) {
    var p = pending[m.id]; delete pending[m.id];
    if (m.ok) p.resolve(m.data); else { var e = new Error(m.error || ('HTTP ' + m.status)); e.status = m.status; p.reject(e); }
    return;
  }
  if (m.op === 'theme') { applyTheme(m.theme); fire('theme', state.theme); return; }
  if (m.op === 'visible') { state.visible = !!m.visible; fire('visible', state.visible); return; }
  if (m.op === 'refresh') { fire('refresh'); return; }
}
window.addEventListener('message', function first(ev){
  // Exactly one init, from the embedding dashboard, carrying the port.
  if (port || ev.source !== TOP || !ev.data || ev.data.ph !== 'init' || !ev.ports || ev.ports.length !== 1) return;
  window.removeEventListener('message', first);
  port = ev.ports[0];
  port.onmessage = onPort;
  state.role = ev.data.role === 'admin' ? 'admin' : 'viewer';
  state.surface = /^(tab|widget|settings)$/.test(ev.data.surface) ? ev.data.surface : '';
  applyTheme(ev.data.theme);
  if (window.ResizeObserver) {
    var last = 0;
    new ResizeObserver(function(){
      var h = Math.ceil(document.body.getBoundingClientRect().height);
      if (h !== last) { last = h; send({ op: 'resize', h: h }); }
    }).observe(document.body);
  }
  send({ op: 'ready' });
  var queued = outbox; outbox = [];
  queued.forEach(send);
  fire('theme', state.theme);
});
var ph = {
  call: function(route, opts){
    opts = opts || {};
    return request('call', { route: String(route), method: opts.method || 'GET', body: opts.body === undefined ? null : opts.body });
  },
  read: function(name){ return request('read', { name: String(name) }); },
  render: function(slot, key, node){ send({ op: 'render', slot: String(slot), key: key == null ? null : key, node: node }); },
  on: function(name, cb){ if (listeners[name] && typeof cb === 'function') listeners[name].push(cb); },
  resize: function(h){ send({ op: 'resize', h: +h || 0 }); },
  toast: function(msg, kind){ send({ op: 'toast', msg: String(msg), kind: String(kind || 'info') }); },
  asset: function(path){ return Object.prototype.hasOwnProperty.call(assets, path) ? assets[path] : null; },
  get theme(){ return state.theme; },
  get user(){ return { role: state.role }; },
  get surface(){ return state.surface; },
  get visible(){ return state.visible; }
};
Object.freeze(ph);
Object.defineProperty(window, 'ph', { value: ph, writable: false, configurable: false });
})();
"""


def _sri(text: str) -> str:
    return "'sha256-%s'" % base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode()


FRAME_CSP_TMPL = ("default-src 'none'; script-src %s; style-src 'unsafe-inline'; img-src data: blob:; "
                  "font-src data:; connect-src 'none'; frame-src 'none'; worker-src 'none'; "
                  "form-action 'none'; base-uri 'none'; object-src 'none'; frame-ancestors 'self'; "
                  "sandbox allow-scripts")

FRAME_HEADERS = {
    "X-Frame-Options": "SAMEORIGIN",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), usb=(), payment=(), "
                          "clipboard-read=(), display-capture=(), fullscreen=()",
}


def build_document(frame: LoadedFrame) -> Tuple[bytes, str]:
    """Return ``(html_bytes, csp_header_value)`` for *frame* (cached)."""
    if frame._doc is not None:
        return frame._doc
    scripts = [BRIDGE_JS] + list(frame.scripts)
    assets_json = json.dumps(frame.assets).replace("</", "<\\/").replace("<!--", "<\\u0021--")
    html = (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>plugin frame</title>"
        "<style>" + BASE_CSS + "</style>"
        + ("<style>" + frame.css + "</style>" if frame.css else "")
        + "</head><body>\n"
        "<script type=\"application/json\" id=\"ph-assets\">" + assets_json + "</script>\n"
        + "".join("<script>" + s + "</script>\n" for s in scripts)
        + "</body></html>\n"
    )
    csp = FRAME_CSP_TMPL % " ".join(_sri(s) for s in scripts)
    frame._doc = (html.encode("utf-8"), csp)
    return frame._doc


# ═══════════════════════════════════════════════════════════════════════════════
# Tickets and frame tokens
# ═══════════════════════════════════════════════════════════════════════════════

TICKET_TTL = 30.0
TOKEN_TTL = 2 * 3600.0
MAX_TICKETS_PER_USER = 20
MAX_TOKENS_PER_USER = 64

_lock = threading.Lock()
_tickets: Dict[str, Dict[str, Any]] = {}   # sha256(ticket) → record
_tokens: Dict[str, Dict[str, Any]] = {}    # sha256(frame token) → record


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _purge(now: float) -> None:
    for d in (_tickets, _tokens):
        for k in [k for k, v in d.items() if v["exp"] <= now]:
            del d[k]


def issue(user: str, plugin: str, frame: str, reads: List[str],
          now: Optional[float] = None) -> Optional[Tuple[str, str]]:
    """Mint ``(ticket, frame_token)`` for *user*; ``None`` when the user has
    too many outstanding tickets (rate limit)."""
    now = time.time() if now is None else now
    with _lock:
        _purge(now)
        if sum(1 for v in _tickets.values() if v["user"] == user) >= MAX_TICKETS_PER_USER:
            return None
        mine = sorted((k for k, v in _tokens.items() if v["user"] == user), key=lambda k: _tokens[k]["exp"])
        for k in mine[:max(0, len(mine) - MAX_TOKENS_PER_USER + 1)]:
            del _tokens[k]
        ticket = secrets.token_urlsafe(32)
        token = secrets.token_urlsafe(32)
        _tickets[_h(ticket)] = {"user": user, "plugin": plugin, "frame": frame, "exp": now + TICKET_TTL}
        _tokens[_h(token)] = {"user": user, "plugin": plugin, "frame": frame,
                              "reads": list(reads), "exp": now + TOKEN_TTL}
    return ticket, token


def consume_ticket(ticket: str, plugin: str, frame: str, now: Optional[float] = None) -> str:
    """Consume *ticket* (single use).  Returns ``"ok"``, ``"gone"`` (unknown,
    expired or already used → HTTP 410) or ``"mismatch"`` (bound to another
    plugin/frame → 403).  The ticket is spent in every case."""
    now = time.time() if now is None else now
    if not isinstance(ticket, str) or not 20 <= len(ticket) <= 100:
        return "gone"
    with _lock:
        rec = _tickets.pop(_h(ticket), None)
    if rec is None or rec["exp"] <= now:
        return "gone"
    if rec["plugin"] != plugin or rec["frame"] != frame:
        return "mismatch"
    return "ok"


def resolve_token(token: str, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The scope record for a frame token (``user``, ``plugin``, ``frame``,
    ``reads``) or ``None``."""
    now = time.time() if now is None else now
    if not isinstance(token, str) or not 20 <= len(token) <= 100:
        return None
    with _lock:
        rec = _tokens.get(_h(token))
        if rec is None or rec["exp"] <= now:
            return None
        return dict(rec)


def allowed(scope: Dict[str, Any], method: str, path: str) -> bool:
    """Is *scope* (a resolved frame token) allowed to call *method* *path*?

    Only the frame's own plugin routes, plus GET on the read APIs it
    declared.  Everything else — config, users, other plugins, the ticket
    endpoint itself — is refused, whatever the user behind it may do.
    """
    prefix = "/api/plugin/%s/" % scope["plugin"]
    if path.startswith(prefix):
        rest = path[len(prefix):]
        # Same alphabet the route matcher accepts; no dot-segments, no empty
        # segments, nothing that a normalising proxy could turn into another path.
        return bool(_ROUTE_TAIL_RE.fullmatch(rest)) and not any(seg in (".", "..") for seg in rest.split("/"))
    name = READ_PATHS.get(path)
    return method == "GET" and name is not None and name in scope["reads"]


def revoke(plugin: Optional[str] = None, user: Optional[str] = None,
           frame: Optional[str] = None) -> int:
    """Drop tickets and tokens matching every given filter (all when none)."""
    n = 0
    with _lock:
        for d in (_tickets, _tokens):
            for k in list(d):
                v = d[k]
                if (plugin is None or v["plugin"] == plugin) and (user is None or v["user"] == user) \
                        and (frame is None or v["frame"] == frame):
                    del d[k]
                    n += 1
    return n


def _reset_for_tests() -> None:
    with _lock:
        _tickets.clear()
        _tokens.clear()
