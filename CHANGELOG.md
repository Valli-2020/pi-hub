# Changelog

All notable changes to Pi Hub are documented here. Every release ships a
changelog entry AND matching GitHub release notes.

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
