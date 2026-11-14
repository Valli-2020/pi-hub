# Changelog

All notable changes to Pi Hub are documented here. Every release ships a
changelog entry AND matching GitHub release notes.

## [8.3.2] - 2026-11-14

Open dashboard tabs follow a core update.

### Fixed

- **A tab opened before an update kept running the old page.** A plugin frame,
  which always loads fresh, could then use a bridge operation the old page did
  not know (`ph.open` after 8.3.1) and was stopped as misbehaving. `/api/auth/me`
  now reports `core_version`; the page checks it every 30 s and whenever the tab
  is shown again, and reloads when the server runs a different version. While a
  dialog is open or a field has focus it waits and shows a note instead.
- **Unknown frame operations are refused, not punished.** A well-formed message
  with an operation the page does not know is answered with status 501 and no
  longer counts towards stopping the frame. Malformed messages still do.

## [8.3.1] - 2026-11-14

Upgrading a plugin from the store no longer switches it off, and plugins get
clickable header pills, links from frames and a data folder that survives
upgrades.

### Fixed

- **Store upgrades kept plugins disabled.** Installing a new (or the same)
  version over an enabled plugin removed it from `plugins.json`, so every
  upgraded plugin had to be enabled again by hand. Now it is enabled and loaded
  again right after the swap when the new version asks for no new capability;
  its approval moves to the new version and its frame files are pinned anew. A
  version that asks for more stays installed and shows as *needs approval*. A
  disabled plugin stays disabled. A failed swap also re-enables the old version.
- **Plugin `config.json` stays private on upgrade.** The store wrote it back
  with mode 0644 after an upgrade; it is now 0600, since it can hold API keys.

### Added

- **Header pills can open a tab:** `Contribution("header.pill", id, provider,
  opens="<tab id>")` names the plugin's own `tab` contribution or a frame with a
  `tab` surface. The pill becomes a link (click, Enter or Space). Unknown ids
  are ignored with a warning when the plugin loads.
- **`ph.open(url)` for frames:** opens an http(s) link in a new tab with
  `noopener,noreferrer`. Only absolute http/https URLs up to 2000 characters
  without user name or password, from a visible frame, one per second. When the
  browser would block the new tab, an *Open link* dialog shows the URL with a
  real link and Cancel. The dashboard itself is never navigated.
- **`ctx.data_dir()`:** `<plugin dir>/data`, created with mode 0700. The store
  keeps it across upgrades and reinstalls, never extracts `data/` from a
  tarball and removes it on uninstall.

### Changed

- A `header.pill` provider that returns `None` shows nothing instead of the `!`
  error chip. Exceptions and invalid nodes still show the chip.
- PLUGINS.md documents pill `opens`, `ph.open`, `ctx.data_dir()` and what an
  upgrade does to an enabled plugin.

### Upgrade notes

Plugins that use `opens`, `ph.open` or `data_dir()` need
`min_core_version = "8.3.1"`. On older cores `opens` is a TypeError at load,
`ph.open` is undefined and `data_dir` is missing.

## [8.3.0] - 2026-11-14

Every plugin can now be configured completely in Settings → Plugins → Configure.

### Added

- **New field types for plugin settings:** `url` (checked on the server: http://
  or https:// with a host), `textarea` (up to 8000 characters) and `list`, a
  table of rows with up to 8 columns. Rows can be added, moved up and down and
  removed; an error names the row (`Links, row 3: URL must start with http:// or
  https://`). On phones each row is shown as a small card.
- **Sections:** fields with the same `section` are grouped under a heading.
- **Plugins without a schema** get a form built from their `config.json`
  (checkboxes, numbers, URLs, text, masked secrets for keys such as `api_key` or
  `token`, and lists of flat objects). The dialog says that the form was
  inferred. Their Configure button now shows up.
- `examples/plugins/host-insights` 1.1.0 groups its settings in sections and has
  an *Extra targets* list.

### Changed

- The Configure dialog is wider, scrolls with the Save button always visible,
  shows help texts under each field and shows save errors in red.
- PLUGINS.md §5 documents every field key and value rule; the checklist asks for
  every setting to be in the schema.

### Upgrade notes

A plugin that uses `url`, `textarea`, `list` or `section` needs
`min_core_version = "8.3.0"`. Older cores show these fields as plain text
inputs, which cannot edit a list.

## [8.2.1] - 2026-11-09

Dockge stacks inside a Proxmox LXC container can now be listed and controlled.

### Added

- **`pct` on a Dockge endpoint.** With a Proxmox host as `host_id`, Pi Hub runs
  `docker compose` inside that container through `pct exec`. See
  `config.example.json`.

### Upgrade notes

Add a `pct` endpoint only after updating to 8.2.1. Version 8.2.0 ignores the
field and would run Docker on the Proxmox host itself.

## [8.2.0] - 2026-11-08

Dockge stacks can now be controlled from Pi Hub, on more than one host.

### Added

- **Start, Stop, Restart, Update and Logs for each stack** in the Dockge tab.
  Pi Hub runs `docker compose` in the stack directory, locally or over SSH, so
  changes show up in Dockge as well. Stop, Restart and Update ask first.
- **Several Dockge hosts.** `dockge.endpoints` in `config.json` lists them, each
  with an optional `host_id` from the host registry and an "Open Dockge" link.
  The tab groups stacks by host and shows an error for an unreachable one.
- **`stacks` capability** per `endpoint:stack` target, in the user capability
  matrix. It covers both control and logs. Admins keep full access.
- `GET /api/dockge/endpoints`, `GET /api/dockge/<endpoint>/<stack>/logs` and
  `POST /api/dockge/<endpoint>/<stack>/<start|stop|restart|update>`.

### Changed

- **Stacks show their real state:** `running`, `partial`, `exited` or
  `inactive`, instead of `managed` for everything. `/api/dockge/stacks` keeps
  `name` and `status` and adds `running`, `total`, `managed`, `endpoint` and
  `endpoint_name`.
- Stacks outside the stacks directory are listed read-only.

### Removed

- The HTTP fallback to `<dockge>/api/stacks`. Dockge has no such API; the call
  only ever returned its HTML page.

### Upgrade notes

Without `dockge.endpoints` Pi Hub uses one local endpoint on
`dockge.stacks_dir`. The user running Pi Hub needs access to Docker, and remote
endpoints need working SSH keys. See `config.example.json`.

## [8.1.4] - 2026-09-27

One colour change on the service card. Only `web/index.html` changes.

### Changed

- **The "Open" button is filled with Pi Hub's yellow/orange (`--accent`).**
  The button below the service name is the one coloured element on the card, so
  the eye lands on the action.
- **The service name is plain text, not a link.** It is no longer an
  `<a href>`: the card has exactly one way out, the Open button, and the name is
  just the name. The URL is still shown below it as plain text, and the search
  filter still matches the name. With no card title being a link any more, the
  `.card h4 a` rules are removed (host, dual-boot and Proxmox cards already
  rendered a plain `<h4>`).
- Reverts the `--link` / `--link-hover` tokens introduced in 8.1.2 — the
  service name keeps the card's text colour.

## [8.1.3] - 2026-09-27

Correction to 8.1.2, which shipped the wrong yellow. Only `web/index.html`
changes.

### Fixed

- **The "Open" button uses Pi Hub's own yellow/orange (`--accent`) again.**
  8.1.2 introduced a separate, much yellower token (hue 92) for the button fill
  — that was not the intent. The button is now filled with `--accent` and
  `--accent-ink`, brightening to `--accent-hover`, exactly like the primary
  button, and the three stray `--open*` tokens are gone. The orange `--link`
  colour for the service name from 8.1.2 is unchanged and still correct.

## [8.1.2] - 2026-09-27

Service cards get their colour back. Only `web/index.html` changes; no API,
plugin or config change.

### Changed

- **The "Open" button is filled with Pi Hub's own yellow/orange again.** 8.1.0
  toned it down to a neutral surface with an orange icon; on a card full of grey
  text that read as "no action here". The whole button is now `--accent` with
  `--accent-ink` text and the brighter `--accent-hover` on hover — no new colour
  is introduced, the button just uses the accent it always had. The icon
  inherits the text colour instead of carrying its own.
- **The service name is orange.** It inherited the card text colour, so the only
  clickable field on the card looked like plain grey text. It now uses a
  dedicated `--link` token, redder than `--accent` (hue 48 dark / 45 light vs
  68 / 60 → #f28a4e / #aa4600) so the link and the button next to it stay
  distinguishable, and it brightens via `--link-hover`.
- Host, dual-boot and Proxmox cards are untouched: they render a plain `<h4>`,
  so `.card h4 a` is exactly the service card.

## [8.1.1] - 2026-09-27

### Fixed

- Status dots (online/offline chips, header pills, live indicator, plugin
  list) now sit exactly on the text's centre line; the halo ring that made
  them look off-centre is gone. Plugin badges get the same fix.
- Dots no longer shimmer: cards stay put on hover and views fade in
  without sliding, so nothing moves by sub-pixels. The live indicator is a
  steady dot and only blinks when data stops arriving.

## [8.1.0] - 2026-09-26

**A polished dashboard.** Same features, calmer and more consistent UI, and
a handful of long-standing display bugs fixed. Only `web/index.html`
changes; plugins, themes and the API are untouched.

### Fixed

- Settings cards, scan rows, banners and inputs had no background: the CSS
  used `--card` and `--raised`, which were never defined. Both are now
  aliases of real tokens.
- The first-run setup screen was unstyled (it used class names that did
  not exist), and the login inputs had no spacing.
- Table column widths applied to every table and were off by one after the
  Host column was added: containers were misaligned and the Users headers
  overlapped. Widths now apply to the containers table only.
- Proxmox instance fields in Settings were squeezed to one character.
- Hint texts were unstyled outside the caps dialog, and the status dots in
  the plugin list were invisible.
- The edit dialog had no field labels once values were filled in; add and
  edit dialogs now label every field.
- IP addresses on host cards wrapped in the middle of a number.

### Changed

- **Header:** status dots on the up/down pills, a live indicator that turns
  amber after 20 s and red after 60 s without fresh data, avatar badge.
- **Sidebar:** accent rail on the active section, divider before plugin
  tabs; on phones the nav scrolls with faded edges.
- **Cards:** edit and delete are small icon buttons, "Open" is a calm
  button instead of a full-width accent bar, the service name opens the
  service, offline icons are desaturated.
- **Containers:** tags on one line (all of them on hover), usage bars in
  three levels (green, amber from 65 %, red from 85 %).
- **Users and Dockge:** avatar and role pill, readable dates; running
  stacks first.
- **Settings:** switches instead of checkboxes, wide cards for Proxmox and
  plugins, a save bar that stays in view.
- **Dialogs and toasts:** enter animation and footer bar; toasts get a
  dismiss button and a timeout bar that pauses on hover.
- New design tokens (`--r-xs`, `--r-md`, `--ease`, `--dur`, `--field`,
  `--ring`, `--shadow-lg`) are additive; existing theme plugins keep
  working. `prefers-reduced-motion` is respected.

## [8.0.0] - 2026-09-26

**Plugin API v2.** Plugins can now change the standard tabs and the whole
UI, and — when they really need it — run their own JavaScript in a
sandbox. Existing (API v1) plugins keep working unchanged. This is a major
release because the trust model and the browser security contract change:
plugins are approved before they load, and the dashboard now enforces a
strict Content-Security-Policy.

### Added

- **Contribution slots.** Plugins add badges, columns, buttons and banners
  to Hosts, Services, Containers and Dockge, a status pill to the header,
  cards to Settings and their own sidebar tabs — declared as JSON nodes
  (`text badge status stat gauge kv link button empty stack columns section
  table`) that the server validates and the browser renders with escaping.
  Plugins never send HTML. Failures are isolated: a broken provider shows a
  small `!` chip, never a blank page.
- **Theme, CSS and layout.** Plugins can offer a colour theme (the core's own
  CSS variables only), sanitised CSS scoped to the plugin's own elements
  (global CSS needs its own high-risk grant), and a layout (default view,
  navigation order and hidden items, density). Theme and layout are
  exclusive: the admin picks one in **Settings → Appearance**.
- **Sandboxed JavaScript frames (`ui.frame`).** Plugin JS runs only in an
  opaque-origin `<iframe sandbox="allow-scripts">` with `connect-src 'none'`:
  it cannot read the admin token, use cookies, make requests, navigate the
  page or open popups. It reaches the dashboard through a validated
  `MessageChannel` bridge (`ph.call`, `ph.read`, `ph.render`, `ph.on`,
  `ph.theme`, `ph.toast`, `ph.resize`, `ph.asset`) with rate limits, a
  watchdog and a crash guard. The server independently scopes every frame
  request (`Authorization: Frame` token: the frame's own plugin routes and
  the read APIs it declared, nothing else); the iframe URL carries a
  one-time 30 s ticket. Frame files are pinned by SHA-256.
- **Consent before code runs.** Enabling a plugin shows what it declares
  (system and `ui.*` capabilities, high-risk ones flagged) and the plugin is
  not imported until the admin approves (a plugin that hides its
  capabilities from the static read is refused after import instead). Grants live in
  `pi_hub_plugins/plugin_state.json`; an update that asks for more waits for
  re-approval. Plugins that were enabled before the upgrade are
  grandfathered.
- **Safe mode.** `/?safe=1` (no plugin UI), `/?noframes=1`, a per-plugin
  **Hide UI** switch, and `run.py --safe-mode` / `PIHUB_SAFE_MODE=1` (no
  plugins).
- **Plugin config schema.** `get_config_schema()` (text, password, number,
  checkbox, select) renders a Configure dialog; secrets are masked and kept
  when left blank. `migrate_config()` runs on version changes.
- **Events.** `host.state`, `container.state`, `scan.complete` and
  `config.changed`, delivered on a dispatcher thread; a watcher probes hosts
  (30 s) and containers (60 s) only while a plugin subscribes.
- **Routes:** path parameters (`/item/{id}`), query strings, `PUT` and
  `DELETE`; handlers receive only the arguments they name.
- **Multi-file plugins** install from the store (helper modules, one
  sub-package, `pihub-plugin.json`, README, LICENSE, `static/**`;
  `static/frame/**` restricted to inert types and sizes; at most 300 files;
  dotfiles, `__pycache__` and `.pyc` never extract).
- Examples: `examples/plugins/host-insights`, `midnight-theme`,
  `live-pulse`; a hostile `tests/fixtures/evil-frame` plus a browser test
  that proves every escape attempt fails.

### Changed

- **Plugin API v1 fixes:** `TaskDef` now runs (`interval`, `autostart`),
  `TabUIDef.position` sorts the sidebar, `ActionDef.style` and `.caps` take
  effect, `ctx.toast()` reaches the UI (admins only), `on_host_state_change` and
  `on_scan_complete` are called, `load()` is optional, plugins that fail to
  load are listed with their error, and an unknown `plugin_api_version` is
  refused. Plugin tabs are no longer rewritten on every poll.
- **Web sessions are 12 h sliding** (login with `client:"web"`, which the
  dashboard sends). CLI and service logins (`pi_hub/cli.py`, the Hermes
  refresh script) keep 7 days and Bearer auth is unchanged.
- The Containers table `colspan` for its empty/loading row now matches its
  column count.

### Security

- **Hash-based Content-Security-Policy for the dashboard.** `script-src`
  pins the SHA-256 of the page's two inline scripts (no `'self'`, no
  `'unsafe-inline'`), `object-src 'none'`, `base-uri 'none'`,
  `frame-ancestors 'none'`, `form-action 'self'`; every other response
  (JSON, SVG, plugin assets, errors) gets a script-less policy. The server
  refuses to start if the page's inline scripts cannot be hashed.
- Plugin CSS is sanitised (no `@import`, `url(`, `expression`, backslash
  escapes, `@font-face`, unbalanced braces) and wrapped in a per-plugin
  scope; theme values are restricted to the core's variables.
- Grants are consent and a kill-switch, **not** a sandbox for plugin
  Python, which still runs in-process with the service's privileges — see
  PLUGINS.md §7 for the honest scope.

### Upgrade notes

- A hand-edited `web/index.html` with extra inline scripts or `on*=`
  handlers is now blocked by the CSP (the stock file has exactly two
  attribute-less inline scripts and no handlers).
- Newly enabled plugins, and updates that request new capabilities, need
  admin approval. Plugin Python that declares `capabilities` in a form
  Pi Hub cannot read without running it must ship `pihub-plugin.json`.
- `<script src="/plugin-static/…">` never worked (auth header) and is now
  blocked by the CSP too; use a frame.
- Rolling back to 7.7.0 is safe: 7.7.0 ignores `plugin_state.json`.

## [7.7.0] - 2026-08-19

### Added

- **Plugin form-dialog actions** (`ActionDef.fields`): plugin actions can
  declare a field schema (allowlisted types `text` / `password` /
  `number` / `checkbox`); the frontend opens a form dialog instead of a
  bodyless POST and submits the values as JSON to
  `/api/plugin/<name>/<action-id>`. Enables plugin config forms without
  allowing plugin-supplied HTML (stored-XSS surface stays closed).
  Backwards compatible: actions without `fields` behave exactly as
  before.

## [7.6.1] - 2026-08-19

Fixes from the 2026-08-18 review of the 7.6.1 fork (review doc outside the
repo; A/B/C/D/E/F ids refer to its sections). Every item was verified
live against a running instance where applicable.

### Security

- **Login lockout is per-username, not per-IP (B3).** Any valid account
  could previously reset another account's failure counter by logging in
  from the same IP — `admin` guessing could be retried forever. A
  successful login now clears only its own username's counter.
- **A `null` request body can no longer park a handler thread (B1).**
  `json.loads(b"null")` returned `None`, the same value used as the
  "413 already sent" signal — a 4-byte POST got no response and hung the
  connection until the client gave up, with no auth required. Rejection
  now uses a dedicated sentinel, and handlers have a 30 s socket timeout
  so an announced-but-never-delivered body can't hold a thread forever.
- **GET bodies are drained (B2).** With HTTP/1.1 keep-alive a GET body
  stayed in the buffer and was parsed as the next request line — a
  request-smuggling primitive behind the planned Pangolin reverse proxy.
  `do_GET`/`do_DELETE` now drain or close on oversized/odd bodies.
- **Plugin tab icon_svg is sanitized (E1).** It was the only plugin
  payload field inserted as raw markup (`<img onerror>` fired; CSP has
  `'unsafe-inline'`). Icons now pass through an SVG allowlist sanitizer
  that strips `on*` attributes and `javascript:`/`data:` hrefs.
- **Plugin tab ids are escaped (E2).** The id landed unescaped in three
  attributes and two selector interpolations.

### Fixed

- **Plugin store downloads work again (A1).** `_NoRedirect` makes urllib
  RAISE `HTTPError` on 3xx — the manual redirect loop treated that as a
  response and never ran; every asset fetch ended as `GitHub HTTP 302`,
  so no plugin could be listed or installed. `HTTPError` IS a response
  object; the chain is now walked per-hop with a redirect cap. (The
  fork's own "fix" for this was also broken — verified empirically.)
- **MAC removal from a host works again (A2).** Update validation
  contradicted the create path; `"mac": ""` 400'd and a mistaken MAC
  stayed forever with a live wake button.
- **v6→v7 migration no longer NameErrors (C1).** A dangling reference to
  the removed `DEFAULT_PROXMOX_NODE` crashed config load (→ fail-closed
  stub → every route 503) for v6 installs without a `node`.
- **Dual-boot start without a MAC fails cleanly (C2).** `POST
  /api/hosts/<id>/start/windows` answered 200, then the background task
  died on `KeyError: 'mac'` and the UI spun on "running" for an hour.
  Both boot sequences now guard the MAC; the route rejects with 400.
- **Backup pruning only touches the updater's own dirs (C3).** With
  `BACKUP_ROOT` on a NAS the "keep the 3 highest" rmtree could delete
  arbitrary neighbor directories; `ignore_errors` hid the failures.
- **dockge-ssh scanner works (C4).** The `find` parens were unquoted for
  the remote shell (syntax error → empty result, indistinguishable from
  "nothing found"); `StrictHostKeyChecking` is now set.
- **Plugin updates preserve the plugin's config.json (D1).** The swap
  deleted the old dir before replacing it, wiping stored credentials on
  every store update (e.g. Bambuddy password). The old tree is parked
  until the new one is in place, config.json is restored, `disable()`
  failures abort, and a failed move rolls the old version back.
- **Tar-bomb guard runs before memory is exhausted (D2).**
  `tf.getmembers()` materialized the full member list; a crafted archive
  with hundreds of thousands of members burned memory/CPU while holding
  the install lock. Extraction now streams with a 10 000-member cap.
- **`ctx.ssh_action()` actually acts (D3).** It passed the host DICT to
  `hosts.ssh_action(host_id, ...)` — the lookup always failed, so every
  call returned a truthy `{"success": False}` while nothing was sent.
- **`ctx.get_hosts()` / `get_services()` no longer hand out the live
  config (D4).** Plugin writes mutated the module cache; both now return
  deep copies.
- **Plugin `name` must match its directory (D5).** A plugin naming
  itself differently could hijack foreign static registrations and task
  status; PLUGINS.md required the match, nothing enforced it.
- **Unload actually stops plugin background work (D6).** Threads that
  ignore `thread_cancel()` kept running as daemons with a fully
  functional context after disable/uninstall. The context is now marked
  dead on unload (every ctx method raises), and the module is dropped
  from `sys.modules` so a reinstall loads fresh code.
- **Scan candidates can no longer be mis-added (E3).** Checkbox indices
  restarted per group while the apply handler indexed the global list —
  with both groups populated, checking one row added another.
- **`CardUIDef` serializes its title/content (F).** The generic UI
  serializer dropped them; PLUGINS.md now honestly marks the
  not-yet-implemented surfaces (`TaskDef`/`get_tasks`, cards, action
  caps, toast surfacing, auth-header-only plugin static).

## [7.6.0] - 2026-08-17

Port of the findings from the 7.6.0 hardening review (F-xx ids refer to
that review; 7.5.5/7.5.6 already carried its own implementations of
F-02/F-03/F-07/F-08, which are untouched).

### Security

- **`ssh_user` validated everywhere (F-11).** `ssh` reads any argument
  starting with `-` as an option, so `ssh_user: "-oProxyCommand=…"` was
  argument injection into the local `ssh` argv — local command execution
  on the hub. The API rejects invalid values, and `hosts._ssh_target()`
  enforces a strict character class again at call time (hosts without a
  usable target fail closed instead of running).
- **Token leak guard raises instead of asserting (F-18).** `python -O`
  strips assertions; the invariant is now a real check.
- **Plugin config written 0600 (F-15).** Plugin config routinely holds
  API keys and was created with the process umask.
- **Plugin errors no longer echo exception text to users (F-20).** The
  detail goes to the server log; viewers get a generic message.
- **Password change revokes other sessions (F-06).** Both admin-reset
  and self-service cases kick every other session; the caller's own
  token survives, so changing your own password keeps you signed in.

### Fixed

- **No default Proxmox node (F-04).** An unset `node` is now an
  explicit, actionable error instead of a silent "0 containers" (the
  API returns an empty list for an unknown node). Same for container
  actions.
- **Container actions ignore the read-path backoff (F-14).** A failed
  status poll no longer refuses explicit start/stop clicks for up to
  60 s — exactly when someone is restarting a stuck container.
- **Connection errors name the configured host (F-16)** instead of a
  generic "Proxmox unreachable".
- **Dual-boot detection cannot take down host status (F-09).** A pool
  timeout or a missing `ip` propagates no longer — the pair reports
  `unknown` instead of killing every host's status.
- **WOL without a MAC returns 400 (F-10)** instead of a 500.
- **`service_endpoint()` no longer raises (F-13)** — a hand-typed
  `status_port` can no longer break the whole services view.
- **Remote compose scan honours `ssh_user` (F-17)** instead of assuming
  root.
- **GRUB entry is shell-quoted (F-12)** — an apostrophe in a menu title
  can no longer break out of the `grub-reboot` quotes.
- **One config-edit lock (F-22).** `config`, `routes` and the plugin
  sandbox share a single lock for read-modify-write on config.json.
- **Dead branch removed in `classify()` (F-21).**

### Added

- **Hosts without a MAC can be added (F-24).** A VPS or NAS that cannot
  be woken is still worth showing; new host type `other` for machines
  with no power actions.
- **Service names may contain spaces and parentheses (F-25).** They
  were neither editable nor deletable through the UI; path segments are
  now percent-decoded.
- **Group labels are rendered (F-23).** Sorting services by group now
  inserts a heading per group (string map or `{label}` object form).
- **Container table stops rebuilding on every poll (F-26).** The change
  signature now hashes the displayed values at display precision, so
  the table is only re-rendered when something visible changed (text
  selection survives polling).
- **`web/icons.json` ships again (F-28).** It was gitignored, so a
  fresh clone had no icons at all. The repo now carries a generic set
  plus the previously local brand icons; brand overrides stay local.

### Changed

- **Update source configurable via `UPDATE_OWNER` / `UPDATE_REPO`**
  (defaults unchanged: `Valli-2020` / `pi-hub`).
- **Backup root overridable via `BACKUP_ROOT`** (default: inside the
  install root).
- **Example config uses documentation ranges** (RFC 5737 addresses,
  RFC 7042 MACs, `hub.example.lan`) and documents `cert_fingerprint`.

## [7.5.5] - 2026-08-17

### Security

- **`pct exec` allowlist hardened** — the plugin `ssh_cmd` boundary was
  a string prefix check, bypassable with an unquoted `;` / `&&` / `|`
  (the SSH transport hands the command to the remote shell, so the
  suffix ran on the Proxmox host as root). Commands are now parsed with
  `shlex`, the structure `pct exec <vmid> -- <cmd>` is validated (vmid
  numeric, `--` required), and the command is rebuilt with `shlex.join`
  so metacharacters are quoted and cannot escape the container.
- **Proxmox TLS certificate pinning** — the API client no longer
  disables certificate verification (`CERT_NONE`). When a
  `cert_fingerprint` (SHA-256, config or `PROXMOX_CERT_FINGERPRINT`
  env) is set, the peer certificate MUST match it — ARP/DNS-spoofing
  can no longer read the API token in transit. Without a pin, the
  default verified context is used (self-signed certs fail loudly).
- **Auth defaults to enabled** — a config without an `auth` block used
  to mean "no login required" (every LAN visitor became admin). The
  default is now `true`; opt out explicitly and bind to localhost.

### Fixed

- **`DEFAULT_PROXMOX_NODE` was the developer's node name** (`keller`).
  Neutral default (`pve`) — every install must set its own node.
- **Update backups live inside the install root** (`.pi-hub-backups/`)
  instead of the parent directory — an install at `/app` used to write
  to `/pi-hub-backups` (filesystem root) and fail the update.
- **Asset download redirects capped at 5 hops** — a redirect loop can no
  longer tie up a request thread indefinitely.

## [7.5.4] - 2026-08-17

### Changed

- **Repo restored** — fresh repository history with only the product
  files: `run.py`, `pi_hub/`, `web/`, `config.example.json`, docs.
  Runtime state (`config.json`, `secrets.json`, `users.json`) lives
  outside the repo.
- **README.md / DEPLOY.md rewritten** — cover the current product:
  `run.py`, auth, multi-Proxmox, plugin store, core self-update.
- **Docs ship with updates** — `PLUGINS.md`, `README.md`, `DEPLOY.md`
  are part of the updater extraction allowlist and the swap pairs, so
  installed hubs carry the current guides.

### Security

- **Plugin store requires a real admin session** — the store installs
  and executes arbitrary Python from GitHub; all
  `/api/config/plugins/*` mutating routes now reject the synthetic
  "local" admin when auth is disabled.
- **Plugin names reject `.` / `..`** — a crafted name can no longer
  escape the plugins root; `_plugin_dir()` enforces realpath
  containment.
- **Credential-safe asset downloads** — redirects are followed manually
  with the auth header stripped on cross-host hops; reads are capped.
- **No downgrade installs** — `apply()` refuses releases that are not
  strictly newer than the running build.
- **Stage-first plugin installs** — a broken archive never destroys a
  working plugin; top-level `__init__.py` is required.
