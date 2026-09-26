"""Plugin API v2 — contribution slots, node validation and CSS sanitising.

Plugins never ship HTML or JavaScript.  They return small JSON *nodes*
that the core validates here (server side) and renders with ``esc()``
(client side).  Everything a plugin can put on screen is therefore an
element of a closed vocabulary; unknown node types and unknown keys are
dropped, oversized payloads are rejected.

Three layers of defence for a contribution payload:

1. :func:`validate_node` / :func:`validate_keyed` — strict allowlist,
   size limits, ``href`` scheme check.  Raises :class:`ContribError`.
2. The client renderer (``PH.render`` in ``web/index.html``) escapes every
   string; it never uses plugin data as markup.
3. :func:`sanitize_css` / :func:`validate_theme` for the static slots.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

MAX_DEPTH = 6
MAX_NODES = 2000
MAX_STR = 500
MAX_BYTES = 256 * 1024
MAX_KEYS = 500
MAX_ROWS = 500
MAX_CSS_BYTES = 32 * 1024

TONES = ("ok", "warn", "bad", "muted", "accent", "info")
STATES = ("ok", "warn", "bad", "off", "unknown")
FIELD_TYPES = ("text", "password", "number", "checkbox", "select")

#: Per-slot metadata.  ``keyed`` slots return ``{key: node}`` (one node per
#: host / service / container ...), singleton slots return one node.
#: ``cap`` is the ``ui.*`` grant the plugin needs for that slot.
SLOTS: Dict[str, Dict[str, Any]] = {
    "hosts.card.badges":       {"keyed": True,  "cap": "ui.slots"},
    "hosts.card.body":         {"keyed": True,  "cap": "ui.slots"},
    "hosts.card.actions":      {"keyed": True,  "cap": "ui.slots"},
    "services.card.badges":    {"keyed": True,  "cap": "ui.slots"},
    "services.card.actions":   {"keyed": True,  "cap": "ui.slots"},
    "containers.column":       {"keyed": True,  "cap": "ui.slots"},
    "containers.row.actions":  {"keyed": True,  "cap": "ui.slots"},
    "stacks.row.badges":       {"keyed": True,  "cap": "ui.slots"},
    "hosts.top":               {"keyed": False, "cap": "ui.slots"},
    "services.top":            {"keyed": False, "cap": "ui.slots"},
    "containers.top":          {"keyed": False, "cap": "ui.slots"},
    "stacks.top":              {"keyed": False, "cap": "ui.slots"},
    "users.top":               {"keyed": False, "cap": "ui.slots", "admin": True},
    "settings.top":            {"keyed": False, "cap": "ui.slots", "admin": True},
    "header.pill":             {"keyed": False, "cap": "ui.header"},
    "settings.card":           {"keyed": False, "cap": "ui.settings", "admin": True},
    "tab":                     {"keyed": False, "cap": "ui.tab"},
    # static (payload given at load time, no provider)
    "theme":                   {"static": True, "cap": "ui.theme", "exclusive": True},
    "style":                   {"static": True, "cap": "ui.style"},
    "layout":                  {"static": True, "cap": "ui.layout", "exclusive": True},
}

#: Every ui.* capability a plugin may declare (consent list in the UI).
UI_CAPS = ("ui.tab", "ui.slots", "ui.header", "ui.settings", "ui.theme",
           "ui.style", "ui.style.global", "ui.layout", "ui.frame")

#: System capabilities (PluginContext).
SYSTEM_CAPS = ("hosts.read", "hosts.wake", "services.read", "proxmox.read",
               "proxmox.control", "dockge.read", "ssh.execute")

CONTRIB_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")
ACTION_RE = re.compile(r"^[A-Za-z0-9._~-]+(/[A-Za-z0-9._~-]+)*$")
KEY_RE = re.compile(r"^[A-Za-z0-9._:@ /()+-]{1,128}$")

#: Core CSS variables a theme may set.
THEME_TOKENS = ("--bg", "--surface", "--surface-2", "--border", "--border-strong",
                "--text", "--muted", "--faint", "--accent", "--accent-hover",
                "--accent-ink", "--ok", "--ok-ink", "--warn", "--bad", "--bad-ink",
                "--radius", "--radius-sm", "--shadow")

LAYOUT_VIEWS = ("hosts", "services", "containers", "stacks", "users", "settings")


class ContribError(ValueError):
    """A contribution payload violated the schema or a limit."""


# ═══════════════════════════════════════════════════════════════════════════════
# Nodes
# ═══════════════════════════════════════════════════════════════════════════════


class _Budget:
    __slots__ = ("nodes",)

    def __init__(self) -> None:
        self.nodes = 0

    def take(self) -> None:
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise ContribError("too many nodes (max %d)" % MAX_NODES)


def _str(v: Any, what: str, required: bool = False, limit: int = MAX_STR) -> str:
    if v is None:
        if required:
            raise ContribError("%s is required" % what)
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if not isinstance(v, str):
        raise ContribError("%s must be a string" % what)
    if len(v) > limit:
        raise ContribError("%s too long (max %d)" % (what, limit))
    return v


def _tone(v: Any, default: str = "muted") -> str:
    return v if isinstance(v, str) and v in TONES else default


def _pct(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ContribError("pct must be a number")
    if f != f:                                   # NaN
        raise ContribError("pct must be a number")
    return max(0.0, min(100.0, f))


def _scalar(v: Any) -> Any:
    """A table cell / kv value that is not a node."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v != v:
            return None
        return v
    if isinstance(v, str):
        if len(v) > MAX_STR:
            raise ContribError("cell too long (max %d)" % MAX_STR)
        return v
    raise ContribError("cell must be a scalar or a node")


def valid_href(href: str) -> bool:
    """https?:// URLs and same-origin absolute paths only."""
    if not isinstance(href, str) or not href or len(href) > 2000:
        return False
    if any(ord(c) <= 0x20 or ord(c) == 0x7f or c in "\\\"'<>" for c in href):
        return False
    if href.startswith("//"):
        return False
    if href.startswith("/"):
        return True
    return bool(re.match(r"^https?://[^\s/?#]+", href, re.I))


def _fields(raw: Any) -> List[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 16:
        raise ContribError("fields must be a list (max 16)")
    out = []
    for f in raw:
        if not isinstance(f, dict):
            raise ContribError("field must be an object")
        name = _str(f.get("name"), "field.name", True, 64)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
            raise ContribError("invalid field name")
        ftype = f.get("type", "text")
        if ftype not in FIELD_TYPES:
            ftype = "text"
        item = {"name": name, "label": _str(f.get("label"), "field.label", limit=120) or name,
                "type": ftype, "placeholder": _str(f.get("placeholder"), "field.placeholder", limit=120)}
        d = f.get("default")
        if d is not None:
            item["default"] = _scalar(d)
        if ftype == "select":
            opts = f.get("options")
            if not isinstance(opts, list) or not opts or len(opts) > 50:
                raise ContribError("select field needs 1-50 options")
            item["options"] = [_str(o, "option", limit=120) for o in opts]
        out.append(item)
    return out


def _button(n: dict, b: _Budget, depth: int) -> dict:
    action = _str(n.get("action"), "button.action", True, 200)
    if not ACTION_RE.fullmatch(action) or ".." in action.split("/"):
        raise ContribError("invalid button action path")
    method = str(n.get("method", "POST")).upper()
    if method not in ("GET", "POST", "PUT", "DELETE"):
        method = "POST"
    style = n.get("style", "secondary")
    if style not in ("primary", "secondary", "ok", "bad", "quiet"):
        style = "secondary"
    out = {"type": "button", "label": _str(n.get("label"), "button.label", True, 80),
           "action": action, "method": method, "style": style,
           "confirm": _str(n.get("confirm"), "button.confirm", limit=200),
           "fields": _fields(n.get("fields"))}
    caps = n.get("caps")
    if isinstance(caps, list):
        out["caps"] = [c for c in caps if isinstance(c, str) and len(c) <= 32][:8]
    return out


def _node(n: Any, b: _Budget, depth: int, inline_only: bool = False) -> dict:
    if depth > MAX_DEPTH:
        raise ContribError("nesting too deep (max %d)" % MAX_DEPTH)
    if not isinstance(n, dict):
        raise ContribError("node must be an object")
    b.take()
    t = n.get("type")
    if t == "text":
        return {"type": "text", "text": _str(n.get("text"), "text", True),
                "tone": _tone(n.get("tone"), ""), "mono": bool(n.get("mono"))}
    if t == "badge":
        return {"type": "badge", "text": _str(n.get("text"), "badge.text", True, 80),
                "tone": _tone(n.get("tone")), "title": _str(n.get("title"), "badge.title", limit=200)}
    if t == "status":
        st = n.get("state") if n.get("state") in STATES else "unknown"
        return {"type": "status", "state": st, "text": _str(n.get("text"), "status.text", limit=80)}
    if t == "stat":
        return {"type": "stat", "label": _str(n.get("label"), "stat.label", True, 80),
                "value": _str(n.get("value"), "stat.value", True, 80),
                "unit": _str(n.get("unit"), "stat.unit", limit=16), "tone": _tone(n.get("tone"), "")}
    if t in ("gauge", "progress"):
        return {"type": t, "label": _str(n.get("label"), "label", limit=80),
                "pct": _pct(n.get("pct")), "text": _str(n.get("text"), "text", limit=80)}
    if t == "link":
        href = _str(n.get("href"), "link.href", True, 2000)
        if not valid_href(href):
            raise ContribError("link.href must be http(s):// or a same-origin path")
        return {"type": "link", "text": _str(n.get("text"), "link.text", True, 200), "href": href}
    if t == "empty":
        return {"type": "empty", "title": _str(n.get("title"), "empty.title", limit=120),
                "text": _str(n.get("text"), "empty.text", limit=300)}
    if inline_only:
        raise ContribError("node type %r is not allowed here" % (t,))
    if t == "button":
        return _button(n, b, depth)
    if t == "kv":
        rows = n.get("rows")
        if not isinstance(rows, list) or len(rows) > 100:
            raise ContribError("kv.rows must be a list (max 100)")
        out_rows = []
        for r in rows:
            if not isinstance(r, (list, tuple)) or len(r) != 2:
                raise ContribError("kv row must be [key, value]")
            out_rows.append([_str(r[0], "kv.key", limit=120), _scalar(r[1])])
        return {"type": "kv", "rows": out_rows}
    if t in ("stack", "columns", "section"):
        ch = n.get("children")
        if not isinstance(ch, list) or len(ch) > 100:
            raise ContribError("%s.children must be a list (max 100)" % t)
        out = {"type": t, "children": [_node(c, b, depth + 1) for c in ch]}
        if t == "stack":
            out["dir"] = "row" if n.get("dir") == "row" else "col"
        if t == "section":
            out["title"] = _str(n.get("title"), "section.title", limit=120)
        return out
    if t == "table":
        cols = n.get("columns")
        rows = n.get("rows")
        if not isinstance(cols, list) or not 1 <= len(cols) <= 12:
            raise ContribError("table.columns must be a list (1-12)")
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            raise ContribError("table.rows must be a list (max %d)" % MAX_ROWS)
        ocols = []
        for c in cols:
            if not isinstance(c, dict):
                raise ContribError("table column must be an object")
            key = _str(c.get("key"), "column.key", True, 64)
            ocols.append({"key": key, "label": _str(c.get("label"), "column.label", limit=80) or key,
                          "align": "right" if c.get("align") == "right" else "left",
                          "mono": bool(c.get("mono"))})
        ra = n.get("row_actions")
        oacts = []
        if ra is not None:
            if not isinstance(ra, list) or len(ra) > 4:
                raise ContribError("row_actions must be a list (max 4)")
            for a in ra:
                if not isinstance(a, dict):
                    raise ContribError("row action must be an object")
                oacts.append(_button(dict(a, type="button"), b, depth + 1))
        orows = []
        for r in rows:
            if not isinstance(r, dict):
                raise ContribError("table row must be an object")
            cells: Dict[str, Any] = {}
            for c in ocols:
                v = r.get(c["key"])
                if isinstance(v, dict):
                    b.take()
                    cells[c["key"]] = _node(v, b, depth + 1, inline_only=True)
                else:
                    cells[c["key"]] = _scalar(v)
            row_id = r.get("_id")
            if row_id is not None:
                cells["_id"] = _str(row_id, "row._id", limit=128)
            orows.append(cells)
        return {"type": "table", "columns": ocols, "rows": orows, "row_actions": oacts}
    raise ContribError("unknown node type %r" % (t,))


def validate_node(node: Any) -> dict:
    """Validate + normalise one node tree (raises :class:`ContribError`)."""
    out = _node(node, _Budget(), 1)
    if len(json.dumps(out)) > MAX_BYTES:
        raise ContribError("payload too large (max %d bytes)" % MAX_BYTES)
    return out


def validate_keyed(data: Any) -> Dict[str, dict]:
    """Validate a ``{key: node}`` mapping for a keyed slot."""
    if not isinstance(data, dict):
        raise ContribError("keyed slot must return an object {key: node}")
    if len(data) > MAX_KEYS:
        raise ContribError("too many keys (max %d)" % MAX_KEYS)
    b = _Budget()
    out: Dict[str, dict] = {}
    for k, v in data.items():
        k = str(k)
        if not KEY_RE.fullmatch(k):
            raise ContribError("invalid key %r" % k[:40])
        out[k] = _node(v, b, 1)
    if len(json.dumps(out)) > MAX_BYTES:
        raise ContribError("payload too large (max %d bytes)" % MAX_BYTES)
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# Static slots: theme / style / layout
# ═══════════════════════════════════════════════════════════════════════════════

_TOKEN_VALUE_BAD = re.compile(r"[;{}<>\\]|url\s*\(|@import|expression\s*\(|/\*|\*/", re.I)


def validate_theme(data: Any) -> dict:
    """``{"tokens": {"dark": {--x: v}, "light": {--x: v}}}`` — core variables
    only, values without CSS control characters."""
    if not isinstance(data, dict) or not isinstance(data.get("tokens"), dict):
        raise ContribError("theme needs {tokens: {dark: {...}, light: {...}}}")
    out: Dict[str, Dict[str, str]] = {"dark": {}, "light": {}}
    for mode in ("dark", "light"):
        toks = data["tokens"].get(mode) or {}
        if not isinstance(toks, dict):
            raise ContribError("theme.tokens.%s must be an object" % mode)
        for k, v in toks.items():
            if k not in THEME_TOKENS:
                raise ContribError("theme token %r is not allowed" % k)
            if not isinstance(v, str) or not v or len(v) > 100 or _TOKEN_VALUE_BAD.search(v):
                raise ContribError("invalid value for %s" % k)
            out[mode][k] = v.strip()
    if not out["dark"] and not out["light"]:
        raise ContribError("theme has no tokens")
    return {"tokens": out}


def sanitize_css(css: Any) -> str:
    """Reject CSS that could escape its wrapper or reach the network.

    The stylesheet is later nested under ``.plg-<name>{ ... }`` (scoped) or
    emitted as-is (global, needs ``ui.style.global``), so unbalanced braces
    or a stray ``</style>`` would break out.
    """
    if not isinstance(css, str):
        raise ContribError("css must be a string")
    if len(css.encode("utf-8")) > MAX_CSS_BYTES:
        raise ContribError("css too large (max %d bytes)" % MAX_CSS_BYTES)
    # Browsers turn CR, CRLF and FF into LF before tokenising, and a newline ends
    # a string.  Reject what we cannot reason about instead of guessing.
    if "\r" in css or "\f" in css or "\x00" in css:
        raise ContribError("css contains a forbidden control character")
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    if "/*" in stripped or "*/" in stripped:
        raise ContribError("unterminated comment")
    low = stripped.lower()
    for bad in ("@import", "url(", "expression", "</", "<!--", "javascript:",
                "-moz-binding", "\\", "@charset", "@namespace", "behavior:",
                "@font-face", "image-set(", "src:"):
        if bad in low:
            raise ContribError("css contains a forbidden construct: %s" % bad)
    if "<" in stripped:                  # '>' is a legal child combinator
        raise ContribError("css contains a forbidden character")
    # Braces inside string literals do not count for the browser, so they must
    # not count here either — otherwise `a{content:"{"}}` balances for us but
    # closes the plugin's scope wrapper in the browser.
    unquoted = re.sub(r'"[^"\n]*"|\'[^\'\n]*\'', '""', stripped)
    if '"' in unquoted.replace('""', "") or "'" in unquoted:
        raise ContribError("css contains an unterminated string")
    depth = 0
    for ch in unquoted:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                raise ContribError("unbalanced braces in css")
    if depth != 0:
        raise ContribError("unbalanced braces in css")
    return stripped.strip()


def validate_style(data: Any, plugin: str) -> dict:
    """``{"css": str, "global": bool}`` → ``{"css": final, "global": bool}``.
    Scoped CSS is wrapped in ``.plg-<name>{...}`` (native nesting)."""
    if not isinstance(data, dict):
        raise ContribError("style needs {css, global?}")
    css = sanitize_css(data.get("css"))
    is_global = bool(data.get("global"))
    final = css if is_global else ".plg-%s{%s}" % (re.sub(r"[^A-Za-z0-9_-]", "-", plugin), css)
    return {"css": final, "global": is_global}


def validate_layout(data: Any) -> dict:
    """``{default_view, nav_order, nav_hidden, density}``."""
    if not isinstance(data, dict):
        raise ContribError("layout must be an object")
    out: Dict[str, Any] = {}
    dv = data.get("default_view")
    if dv is not None:
        if dv not in LAYOUT_VIEWS:
            raise ContribError("default_view must be one of %s" % ", ".join(LAYOUT_VIEWS))
        out["default_view"] = dv
    for k in ("nav_order", "nav_hidden"):
        v = data.get(k)
        if v is not None:
            if not isinstance(v, list) or any(x not in LAYOUT_VIEWS for x in v):
                raise ContribError("%s must be a list of view names" % k)
            out[k] = list(dict.fromkeys(v))
    if "settings" in out.get("nav_hidden", []):
        raise ContribError("the settings view cannot be hidden")
    dens = data.get("density")
    if dens is not None:
        if dens not in ("compact", "comfortable"):
            raise ContribError("density must be compact or comfortable")
        out["density"] = dens
    if not out:
        raise ContribError("layout is empty")
    return out


def validate_static(slot: str, data: Any, plugin: str) -> dict:
    if slot == "theme":
        return validate_theme(data)
    if slot == "style":
        return validate_style(data, plugin)
    if slot == "layout":
        return validate_layout(data)
    raise ContribError("slot %r is not static" % slot)


# ═══════════════════════════════════════════════════════════════════════════════
# Config schema
# ═══════════════════════════════════════════════════════════════════════════════


def validate_config_schema(raw: Any) -> List[dict]:
    """Normalise ``Plugin.get_config_schema()`` (silently drops bad fields)."""
    out: List[dict] = []
    if not isinstance(raw, list):
        return out
    for f in raw[:40]:
        if not isinstance(f, dict):
            continue
        name = f.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
            continue
        ftype = f.get("type", "text")
        if ftype not in FIELD_TYPES:
            ftype = "text"
        item: Dict[str, Any] = {
            "name": name, "type": ftype,
            "label": str(f.get("label") or name)[:120],
            "help": str(f.get("help") or "")[:300],
            "placeholder": str(f.get("placeholder") or "")[:120],
            "required": bool(f.get("required")),
            "secret": bool(f.get("secret")) or ftype == "password",
            "default": f.get("default") if isinstance(f.get("default"), (str, int, float, bool)) else None,
        }
        for k in ("min", "max"):
            if isinstance(f.get(k), (int, float)) and not isinstance(f.get(k), bool):
                item[k] = f[k]
        if ftype == "select":
            opts = f.get("options")
            if not isinstance(opts, list) or not opts:
                continue
            item["options"] = [str(o)[:120] for o in opts[:50]]
        out.append(item)
    return out


def coerce_config_values(schema: List[dict], values: Any, current: dict) -> tuple[dict, Optional[str]]:
    """Validate submitted values against *schema*.

    Returns ``(new_values, error)``.  Secret fields left blank (or sent as
    the ``{"__set": true}`` placeholder) keep their current value.
    """
    if not isinstance(values, dict):
        return {}, "JSON object required"
    out: Dict[str, Any] = {}
    for f in schema:
        name = f["name"]
        if name not in values:
            continue
        v = values[name]
        if f["secret"] and (v in (None, "") or (isinstance(v, dict) and v.get("__set"))):
            continue
        t = f["type"]
        if t == "checkbox":
            out[name] = bool(v)
        elif t == "number":
            if isinstance(v, bool) or v in (None, ""):
                if f.get("required"):
                    return {}, "%s is required" % f["label"]
                continue
            try:
                num = float(v)
                if num != num or num in (float("inf"), float("-inf")):
                    raise ValueError
            except (TypeError, ValueError):
                return {}, "%s must be a number" % f["label"]
            if "min" in f and num < f["min"]:
                return {}, "%s must be >= %s" % (f["label"], f["min"])
            if "max" in f and num > f["max"]:
                return {}, "%s must be <= %s" % (f["label"], f["max"])
            out[name] = int(num) if num == int(num) else num
        elif t == "select":
            if v not in f["options"]:
                return {}, "%s: invalid choice" % f["label"]
            out[name] = v
        else:
            if not isinstance(v, str) or len(v) > 2000:
                return {}, "%s must be text (max 2000)" % f["label"]
            if f.get("required") and not v.strip():
                return {}, "%s is required" % f["label"]
            out[name] = v
    return out, None
