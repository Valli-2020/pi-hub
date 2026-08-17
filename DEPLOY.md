# Pi Hub — Deployment

Current version: **v7.6.x**. The updater ships with core — no plugin needed.

## Fresh install

```bash
# 1. Get the code
git clone git@github.com:Valli-2020/pi-hub.git /home/pi/pi-hub
cd /home/pi/pi-hub

# 2. Create config.json from the example
cp config.example.json config.json
# → set dashboard title, proxmox instances, scan sources, auth on

# 3. Secrets (Proxmox token, SSH keys) — NEVER in config.json
#    Create secrets.json (chmod 600), keyed by Proxmox instance id:
echo '{"proxmox_tokens": {"pve1": "PVEAPIToken=root@pam!pi-hub=YOUR_NEW_TOKEN"}}' > secrets.json
chmod 600 secrets.json

# 4. Systemd user service
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/pi-hub.service << 'EOF'
[Unit]
Description=Pi Hub
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/pi/pi-hub
ExecStart=/usr/bin/python3 /home/pi/pi-hub/run.py --bind 100.64.0.1 --port 8898
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

# 5. Enable and start
loginctl enable-linger pi
systemctl --user daemon-reload
systemctl --user enable --now pi-hub
```

Bind address: use the host's Tailscale IP (e.g. `100.64.0.1`) instead of
`127.0.0.1` when you want to reach the hub from other Tailscale devices.
`0.0.0.0` prints a warning and should only be used with auth enabled.

## Updating

The built-in updater handles everything — no manual rsync:

1. Settings → Update card shows the latest release (checked every 30 min).
2. **Update now** downloads the release tarball, verifies it (allowlist
   extraction, version parity, compile check, completeness check), backs
   up the current install to `.pi-hub-backups/` (last 3 kept), atomically
   swaps, and restarts the service. The page reloads on the new version.

API equivalent (admin session required):

```bash
curl -X POST http://<host>:8898/api/update/check  -H "Authorization: Bearer <token>"
curl -X POST http://<host>:8898/api/update/apply  -H "Authorization: Bearer <token>" \
     -H "Content-Type: application/json" -d '{"expected_tag":"v7.5.3"}'
```

Updates never touch `config.json`, `users.json`, `secrets.json`,
`sessions.json` or `web/icons.json`. Rollback: restore a backup from
`.pi-hub-backups/`.

## Auth

Auth is **on** by default in production (`"auth": true` in config.json).
First run shows a setup screen that creates the first admin. Users are
managed in Settings → Users, with a per-user capability matrix
(instance-qualified container targets). Plugin management and update
apply require a REAL authenticated admin session — the synthetic
"local" admin (auth disabled) is rejected for those routes.

## Access

- Direct: `http://<host>:8898` (Tailscale IP recommended)
- Via nginx on the Pi (optional):

```nginx
server {
    listen 80;
    server_name pi-hub.local;
    location / {
        proxy_pass http://127.0.0.1:8898;
        proxy_set_header Host $host;
    }
}
```

## Plugins

Plugins are installed from GitHub repos via Settings → Plugins (store).
The store scans a repo's releases for `pihub-plugin.json` manifests and
installs the tarball with a compile gate. See PLUGINS.md for the
developer guide.

## Verify

```bash
curl http://<host>:8898/api/health         # → {"ok":true}
curl http://<host>:8898/api/config          # → must NOT contain token
curl http://<host>:8898/api/hosts/status    # → < 5s
curl -sI http://<host>:8898/ | grep -i etag # → ETag present
```

## Security notes (v7.5.x)

- The update mechanism lives in **core** (`pi_hub/updater.py`) — a
  plugin failure can never break the update path.
- Plugin store routes require a real admin session even when auth is
  disabled; plugin names reject `.`/`..` (path containment enforced).
- Release-asset downloads strip credentials on cross-host redirects and
  cap the read size.
- Never commit `config.json`, `secrets.json`, `users.json` or any live
  config to the repo — they are gitignored.
