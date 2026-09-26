"""CSP (8.0): the dashboard pins its inline scripts by sha256, everything
else gets a restrictive policy.  Talks to a real server on an ephemeral port.

Run: python3 tests/test_csp_headers.py
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import re
import sys
import threading
from http.server import ThreadingHTTPServer

sys.path.insert(0, ".")

from pi_hub import server  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


html = open("web/index.html", "rb").read()

print("hash computation")
scripts = re.findall(rb"<script>(.*?)</script>", html, re.S)
expected = ["'sha256-%s'" % base64.b64encode(hashlib.sha256(s).digest()).decode() for s in scripts]
check("index.html has inline scripts", len(scripts) >= 2, str(len(scripts)))
check("hashes match an independent computation", server.inline_script_hashes(html) == expected)
check("every <script> is attribute-less", len(re.findall(rb"<script\b", html)) == len(scripts))
check("no CR bytes", b"\r" not in html)
check("no inline event-handler attributes",
      not re.search(rb"\son[a-z]+\s*=\s*[\"']", html, re.I),
      str(re.findall(rb"\son[a-z]+\s*=", html, re.I)[:3]))
check("no javascript: URLs", b"javascript:" not in html.lower().replace(b"'javascript:'", b"").replace(b'"javascript:"', b""))

print("failure modes are loud")
for name, page in [("script with src", b"<script src='x.js'></script>"),
                   ("no scripts", b"<p>hi</p>"),
                   ("CRLF", b"<script>a\r\nb</script>"),
                   ("unclosed script", b"<script>a</script><script>b")]:
    try:
        server.inline_script_hashes(page)
        check("rejects %s" % name, False)
    except RuntimeError:
        check("rejects %s" % name, True)

print("policy")
csp = server.dashboard_csp(expected)
m = re.search(r"script-src ([^;]+);", csp)
sp = m.group(1) if m else ""
check("script-src lists exactly the hashes", sp.split() == expected, sp)
check("no 'unsafe-inline' in script-src", "unsafe-inline" not in sp)
check("no 'self' in script-src", "'self'" not in sp)
check("no 'unsafe-eval'", "unsafe-eval" not in csp)
for d in ("object-src 'none'", "base-uri 'none'", "form-action 'self'", "frame-ancestors 'none'",
          "frame-src 'self'", "worker-src 'none'", "connect-src 'self'"):
    check("has " + d, d in csp)
check("generic CSP forbids scripts", "script-src" not in server.GENERIC_CSP and "default-src 'none'" in server.GENERIC_CSP)

print("live headers")
httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
port = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def get(path: str):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("GET", path)
    r = c.getresponse()
    body = r.read()
    h = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return r.status, h, body


st, h, _ = get("/")
check("/ serves", st == 200)
check("/ CSP is the hash policy", h.get("content-security-policy") == server._CSP_CACHE["/"]
      or h.get("content-security-policy") == server.dashboard_csp(server.inline_script_hashes(html)))
check("/ CSP hashes match the served bytes",
      all(x in h["content-security-policy"] for x in expected))
check("X-Frame-Options DENY", h.get("x-frame-options") == "DENY")
check("nosniff", h.get("x-content-type-options") == "nosniff")
st, h, _ = get("/logo.svg")
check("/logo.svg gets the generic CSP", h.get("content-security-policy") == server.GENERIC_CSP)
st, h, _ = get("/api/health")
check("/api/health gets the generic CSP", h.get("content-security-policy") == server.GENERIC_CSP, str(h.get("content-security-policy")))
st, h, _ = get("/plugin-static/x/y.js")
check("plugin-static needs auth", st in (401, 503), str(st))
check("plugin-static error has generic CSP", h.get("content-security-policy") == server.GENERIC_CSP)
st, h, _ = get("/nope")
check("unknown path 404 + generic CSP", st == 404 and h.get("content-security-policy") == server.GENERIC_CSP)
httpd.shutdown()

print()
if FAILED:
    print("FAILED:", ", ".join(FAILED))
    sys.exit(1)
print("ALL CHECKS PASSED")
