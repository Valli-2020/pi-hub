# Changelog

All notable changes to Pi Hub are documented here. Every release ships a
changelog entry AND matching GitHub release notes.

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
