# host-insights

Demo plugin for Pi Hub's **Plugin API v2** — it edits the *standard* tabs instead of only adding a new one.

| What you see | Slot |
|---|---|
| Latency badge (`12 ms` / `down`) and a `flapping` badge on every host card | `hosts.card.badges` |
| "CPU 5m avg" column in the Containers table | `containers.column` |
| `N flaps/h` pill in the header | `header.pill` |
| Stats banner above the host cards | `hosts.top` |
| "Host insights" card in Settings | `settings.card` |
| "Insights" tab with a table, per-row **Probe** button and a threshold form | `tab` |
| CSS scoped to the plugin (`.plg-host-insights`) | `style` |

It only **reads** (TCP connect to port 22 by default, Proxmox container list). It sends nothing and controls nothing.

Install: copy this folder to `pi_hub_plugins/host-insights/`, add `"host-insights"` to `pi_hub_plugins/plugins.json` and click **Review & approve** in Settings → Plugins. The consent dialog lists exactly the capabilities in `pihub-plugin.json`.
