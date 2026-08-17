# Pi Hub

Lightweight self-hosted homelab dashboard for a Raspberry Pi — host
status (WOL/SSH/GRUB dual-boot), multi-Proxmox container control,
service discovery, plugin system, and self-update.

- **Python 3 stdlib only** — no pip, no database, flat module layout
- **~40 MB RAM** on a Pi 5 (~3W)
- **Flat dark theme** (shadcn new-york slate tokens, 8px radius)
- **Self-updating** — checks GitHub releases, installs with verified
  atomic swap (Settings → Update)
- **Plugin system + store** — install plugins from GitHub repos (see
  PLUGINS.md)

## Features

| Feature | Endpoint | Method |
|---------|----------|--------|
| Host status (parallel pings) | `/api/status` | GET |
| Wake / shutdown / reboot / ping | `/api/hosts/<id>/...` | POST |
| Dual-boot state + GRUB one-shot switch | `/api/hosts/<id>/dualboot/...` | POST |
| Task status polling | `/api/task/<name>` | GET |
| Proxmox containers (multi-instance) | `/api/proxmox/containers` | GET |
| Container actions | `/api/proxmox/container/<id>/<action>` | POST |
| Update status / check / apply | `/api/update/status\|check\|apply` | GET/POST |
| Plugin list | `/api/plugins/list` | GET |
| Plugin store (sources, scan, install) | `/api/config/plugins/...` | admin |
| Auth (users, sessions) | `/api/auth/...` | POST |

The UI shows context-aware buttons per host: Wake when offline,
Shutdown+Reboot when online. Dual-boot machines get a merged card with
OS badges and OS-switch buttons with confirm dialogs. Background boot
sequences show live progress.

## Quick Start

```bash
git clone git@github.com:Valli-2020/pi-hub.git
cd pi-hub
cp config.example.json config.json
python3 run.py            # → http://127.0.0.1:8898
```

Production install (systemd, auth, Tailscale bind): see **DEPLOY.md**.

## Configuration

Config lives in `config.json` (gitignored — never commit it).

- **Hosts** — `hosts: [{id, name, mac, ip, ssh_user?, ssh_host?,
  type?, dual_boot_peer?, grub_entries?}]`
- **Proxmox** — `proxmox: [{id, host, node, enabled?, ssh_user?}]`
  (multi-instance since v7). The API token goes in `secrets.json`, NOT
  config.json:
  ```json
  {"proxmox": {"token": "PVEAPIToken=root@pam!token-id=YOUR_TOKEN"}}
  ```
  Create a token on the Proxmox host:
  ```bash
  pveum user token add root@pam token-id --comment "Pi Hub" --privsep 0
  ```
- **Scan** — `scan: {auto_add?, sources: [{type, host_id?, name?}]}`
  (nginx `streams.conf` + Dockge compose discovery; candidates are never
  auto-added)
- **Auth** — `"auth": true` enables the login + per-user capability
  matrix. First run shows a setup screen creating the first admin.

## Updating

The hub checks its GitHub repo every 30 minutes. Settings → Update
shows current vs latest, the changelog, and an **Update now** button
when a release is available.

Update flow: download release tarball → verify (allowlist extraction,
version parity, compile check, completeness check) → backup current
install → atomic swap → automatic service restart → page reloads on the
new version. Config, users, sessions, secrets and the icon set are never
touched; installs are backed up to `.pi-hub-backups/` (last 3 kept).

**The update mechanism lives in core** (`pi_hub/updater.py`) — no plugin
dependency, a plugin failure can never break the update path.

## Plugins

Plugins extend the hub: API routes (`/api/plugin/<name>/...`), UI tabs
and cards, background tasks, event hooks. They are installed from GitHub
repos via Settings → Plugins (store): add a repo as source, scan its
releases for `pihub-plugin.json` manifests, install with one click.

- Manifest `plugins.json` is deny-by-default — only listed plugins load
- Plugin names reject `.`/`..`; extraction is allowlist-only with a
  compile gate
- Plugin store routes require a real admin session even when auth is
  disabled
- Full developer guide: **PLUGINS.md**

## API Behavior

- `GET /api/status` — pings all hosts in parallel. Returns `online`,
  `dualboot_state`, `grub_next`, `capabilities` per host.
- `POST /api/hosts/<id>/wake|shutdown|reboot|ping` — power actions with
  per-target capability checks.
- `POST /api/hosts/<id>/dualboot/switch` — body `{"target":
  "windows"|"uefi"}`. Sets GRUB next-entry and reboots atomically.
- `POST /api/start/windows|debian` — background boot sequences; progress
  at `GET /api/task/boot_windows` etc.
- All mutations return `{"success": bool, "message": str}`.
- Auth: `POST /api/auth/login` → Bearer token in the Authorization
  header (no cookies → not CSRF-reachable). `classify()` is
  deny-by-default; unknown paths need admin.

## Architecture

```
run.py          entry point: argparse, server startup, --create-user
pi_hub/server.py    HTTP server, auth guard, static serving
pi_hub/routes.py    API route dispatch (path-segment based)
pi_hub/hosts.py     ping, WOL, SSH, GRUB, Proxmox, ssh_result
pi_hub/proxmox.py   multi-instance PVE API + pct exec
pi_hub/scanner.py   service discovery (nginx streams + Dockge)
pi_hub/tasks.py     background boot sequences + task status
pi_hub/config.py    config.json loading (atomic saves)
pi_hub/auth.py      users, sessions, capability matrix, classify
pi_hub/updater.py   self-update: check/apply (verified atomic swap)
pi_hub/plugins/     plugin manager + store (manager.py, store.py)
pi_hub_plugins/     user-space plugin directory (gitignored)
web/index.html      single-file dark-theme dashboard (vanilla JS)
```

```
Browser → ThreadingHTTPServer (:8898)
  ├── /                        Dark theme dashboard (vanilla JS)
  ├── /api/status              Parallel host pings
  ├── /api/hosts/<id>/*        WOL / SSH / GRUB / dual-boot
  ├── /api/proxmox/*           Multi-instance Proxmox API + SSH pct
  ├── /api/update/*            Self-update (core)
  ├── /api/config/*            Admin settings (incl. plugin store)
  └── /api/plugin/<name>/*     Plugin routes (namespaced)
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Cards blank/delayed | Check server logs; pings are parallel, should finish in ~3s |
| WOL doesn't wake | Verify MAC, enable WOL in BIOS |
| SSH fails | Redeploy key: `ssh-keygen -f ~/.ssh/known_hosts -R IP` |
| Windows SSH times out | `Set-Service sshd -StartupType Automatic` + firewall rule |
| Proxmox table empty | Check token in `secrets.json` and `enabled: true` on the instance |
| Update fails | Check Settings → Update card for the error; backups in `.pi-hub-backups/` |

## License

MIT
