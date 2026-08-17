# Pi Hub v7 — Frozen API Contract

All frontends (web/index.html, pi_hub/cli.py) depend on this contract.
Breaking changes require both files to be updated simultaneously.

## Static

| Path | Content-Type | Notes |
|------|-------------|-------|
| `GET /` | text/html | web/index.html from memory (ETag, 304) |
| `GET /icons.json` | application/json | brand icons, public cache 1h |
| `GET /logo.svg` | image/svg+xml | public cache 1h |

## Read-only API

| Method | Path | Returns |
|--------|------|---------|
| `GET` | `/api/health` | `{"ok": true}` (public) |
| `GET` | `/api/config` | `public_config()` — NEVER raw config (no token, no ssh_user, no mac); since v7 includes `proxmox_instances: [{id, host, enabled}]` (default-instance object `proxmox` kept for compat) and `scan: {auto_add}` |
| `GET` | `/api/icons` | icons.json (same as static, kept for backward compat) |
| `GET` | `/api/hosts` | Static host registry (CLI use) |
| `GET` | `/api/hosts/status` | Rich record per host: `{id: {online, name, ip, type, capabilities, dual_boot_peer, dualboot_state, grub_next, hide_card, linux_online, windows_online, peer_id}}` |
| `GET` | `/api/hosts/dualboot` | DEPRECATED alias for `/api/hosts/status` — kept one release for cached frontends |
| `GET` | `/api/status` | `{service_name: "online"\|"offline"}` — SERVICES, not hosts |
| `GET` | `/api/proxmox/containers` | `{success, containers: [{vmid, name, status, cpu, mem, maxmem, tags[], instance}], errors?}` — ALL instances merged; each container carries its instance id |
| `GET` | `/api/dockge/stacks` | `[{name, status}, ...]` |
| `GET` | `/api/task/<name>` | `{status, message, ts}` or 404 |
| `GET` | `/api/config/full` | **admin only** — full config for the Settings UI; tokens redacted to `{set, source}` (`env`\|`secrets`\|`secrets-legacy`\|`config`\|`none`); includes host mac/ssh_user/grub_entries, service status_host/status_port/health_path/host/source, proxmox instances (no base_url in dockge) |

## Auth API (v6/v7)

| Method | Path | Body | Access |
|--------|------|------|--------|
| `GET` | `/api/auth/me` | — | any authenticated → `{username, role, caps}` |
| `GET` | `/api/auth/users` | — | admin → `[{username, role, caps, created}]` (no hashes) |
| `POST` | `/api/auth/login` | `{username, password}` | public → `{token, user:{username, role, caps}}` |
| `POST` | `/api/auth/logout` | — | any authenticated |
| `POST` | `/api/auth/bootstrap` | `{username, password}` | public — **only while users.json is missing/empty**; creates the FIRST admin (role forced, rate-limited). Corrupt store → 503 fail-closed |
| `POST` | `/api/auth/users` | `{username, password, role, caps?}` | admin |
| `POST` | `/api/auth/users/<name>/password` | `{password}` | admin or self |
| `POST` | `/api/auth/users/<name>/caps` | `{caps: {action: [targets]}}` | admin → `{success, dropped: [...]}` |
| `DELETE` | `/api/auth/users/<name>` | — | admin |

**Capability matrix (v6.1, v7 instance-qualified):** each user (role `viewer`)
carries `caps: {action: [target,...]}`. Actions: `wake`, `shutdown`, `reboot`,
`dualboot`, `containers`. Targets are host ids or **`<instance>:<vmid>`** for
containers (legacy colon-less VMIDs are migrated to the default instance on
first start; post-migration they are dropped and reported via `dropped`).
Admin role ignores caps (full access). Missing caps = read-only.
Caps changes revoke the user's sessions immediately.

## Mutating API

| Method | Path | Body | Notes |
|--------|------|------|-------|
| `POST` | `/api/hosts/<id>/wake` | — | WOL broadcast |
| `POST` | `/api/hosts/<id>/reboot` | — | SSH reboot (action allowlist) |
| `POST` | `/api/hosts/<id>/shutdown` | — | SSH shutdown or Stop-Computer |
| `POST` | `/api/hosts/<id>/poweroff` | — | Same as shutdown |
| `POST` | `/api/hosts/<id>/dualboot/switch` | `{"target": "windows"\|"debian"}` | GRUB one-shot + reboot |
| `POST` | `/api/hosts/<id>/start/windows` | — | WOL → wait Debian → grub-reboot → reboot (background) |
| `POST` | `/api/hosts/<id>/start/debian` | — | Shutdown Windows → poll offline → WOL (background) |
| `POST` | `/api/hosts/<id>/grub/reset` | — | Clear GRUB next_entry |
| `POST` | `/api/proxmox/<instance>/container/<vmid>/<action>` | — | action ∈ {start, stop, shutdown, reboot}; cap target = `<instance>:<vmid>` |
| `POST` | `/api/proxmox/container/<vmid>/<action>` | — | LEGACY alias → default instance (same cap check) |
| `POST` | `/api/config/hosts` | host dict | **admin** — append host (id, name, ip, mac, type, os_label?, icon?, capabilities?) |
| `POST` | `/api/config/hosts/<id>` | host fields | **admin** — update host (PATCH semantics; id immutable; dual_boot_peer validated) |
| `DELETE` | `/api/config/hosts/<id>` | — | **admin** — delete host; clears dual_boot_peer refs, strips the id from all user caps + revokes their sessions |
| `POST` | `/api/config/services` | service dict | **admin** — append service (name regex `[A-Za-z0-9._-]{1,32}`, url http(s), icon?, group?, status_port?, host?, source?) |
| `POST` | `/api/config/services/<name>` | service fields | **admin** — update service (PATCH semantics; rename allowed) |
| `DELETE` | `/api/config/services/<name>` | — | **admin** — delete service |
| `POST` | `/api/config/proxmox` | `{instances: [{id, host, node, enabled?, ssh_user?}], tokens?: {id: token}}` | **admin** — replace instance list (write-only tokens → secrets.json `proxmox_tokens`; blank = keep; removed instances strip their `id:` caps and prune tokens) |
| `POST` | `/api/config/meta` | `{title?, dockge?: {enabled?}, groups?, scan?: {auto_add?, sources?}}` | **admin** — dashboard meta settings; scan sources validated against fixed types, `dockge-ssh` host_id must exist in the host registry |
| `POST` | `/api/config/scan` | — | **admin** — run service discovery (no writes) → `{success, candidates: [{name\|null, port, url, target, host, source: "nginx"\|"dockge", url_guessed}]}` |
| `POST` | `/api/config/scan/apply` | `{candidates: [...]}` | **admin** — add confirmed candidates (nginx candidates require a name; duplicates skipped) |
| `GET` | `/api/config/plugins/sources` | — | **admin** — all plugin sources + cached scan results (`{id, owner, repo, url, added_at, last_scanned, status, plugins: [{name, version, description, min_core_version, plugin_api_version, core_compatible, release_tag, asset_url, installed, enabled}]}`) |
| `POST` | `/api/config/plugins/sources` | `{url}` | **admin** — add a plugin source (must be `https://github.com/<owner>/<repo>`; SSRF guard rejects anything else) |
| `DELETE` | `/api/config/plugins/sources/<id>` | — | **admin** — remove source + uninstall every plugin that came from it |
| `POST` | `/api/config/plugins/sources/<id>/scan` | — | **admin** — force a GitHub release scan; returns `{success, plugins: [...]}` (manifest asset `pihub-plugin.json` + tarball asset per release; results cached in `plugins_sources.json`) |
| `POST` | `/api/config/plugins/install` | `{source_id, name, version}` | **admin** — install a plugin from the source's scan cache (asset URL resolved server-side — never client-supplied; download cap 5 MB, extraction cap 10 MB, allowlist-only members, py_compile gate, atomic swap; upgrades replace an existing install) |
| `POST` | `/api/config/plugins/<name>/enable` | — | **admin** — add to `plugins.json` + load at runtime |
| `POST` | `/api/config/plugins/<name>/disable` | — | **admin** — remove from `plugins.json` + unload at runtime |
| `DELETE` | `/api/config/plugins/<name>` | — | **admin** — disable + delete the plugin directory + clear provenance |
| `GET` | `/api/update/status` | — | **admin** — cached release check: current/latest version, update_available, changelog (release body), published_at, checked_at, last_error, busy |
| `POST` | `/api/update/check` | — | **admin** — force a GitHub release check (30-min cache bypass) and return the fresh status |
| `POST` | `/api/update/apply` | `{expected_tag: "v7.1.0"}` | **admin** — install the release matching the CACHED latest tag: download (codeload, quoted tag, 10 MB cap, redirect host pinned) → verify (allowlist extraction: only run.py, pi_hub/*.py, web/index.html, web/logo.svg; 20 MB expansion cap; version parity by regex, never exec; py_compile check) → backup → atomic swap → exit (systemd Restart=always). 400 invalid tag; 409 single-flight / TOCTOU / no release known. Requires a REAL authenticated admin session even when auth is disabled. Never touches config.json/users.json/sessions.json/secrets.json/web/icons.json |

## Access classification (v7)

- Public: `GET /api/health`, `POST /api/auth/login`, `POST /api/auth/bootstrap`
  (bootstrap only armed while no user exists; corrupt stores 503 fail-closed)
- `read` (any authenticated user): all other GETs, host/proxmox action POSTs
  (fine-grained caps checked in the route), logout, password change (self)
- `admin`: `/api/config/*` (incl. bare `/api/config/full`), `/api/auth/users*`
  (except self password change), `/api/update/*` (status/check/apply),
  PUT/DELETE, everything unknown (deny-by-default)

## Dropped from V4

Query-string endpoints that take raw MAC/IP from caller — allow unlisted targets:

- `GET /api/wake?mac=`, `/api/shutdown?ip=`, `/api/reboot?ip=`
- `GET /api/ping?ip=`, `/api/grub-reboot`
- `GET /api/dualboot/state`, `POST /api/dualboot/switch`
