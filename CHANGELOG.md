# Changelog

All notable changes to Pi Hub are documented here. Every release ships a
changelog entry AND matching GitHub release notes.

## [7.5.6] - 2026-08-17

### Fixed

- **Plugin store scan found no plugins** — the redirect handling added
  in 7.5.2 raised `HTTPError` on the first 302 (asset download to
  `release-assets.githubusercontent.com`) instead of following it: the
  `_NoRedirect` opener throws on 3xx, and the manual redirect loop only
  caught redirects from a *returned* response. Redirect HTTPErrors are
  now caught on every hop (including the first), so release assets
  download correctly again and the store scan reports plugins.

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
