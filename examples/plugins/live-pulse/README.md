# live-pulse

Demo of a **sandboxed frame** (Plugin API v2, `ui.frame`):

- a widget under the Hosts title and a **Pulse** tab with canvas sparklines
  of each host's TCP latency (5 minute window) and a log of host state
  changes;
- a latency badge in every host card, fed with `ph.render`.

The frame's JavaScript runs in an opaque-origin iframe — it cannot read the
admin token, cannot make network requests, and only talks to the plugin
through `ph.call('samples')` / `ph.call('events')`. Host names and event
text are written with `textContent`, never `innerHTML`.

Install: copy this directory to `pi_hub_plugins/live-pulse`, list it in
`plugins.json`, enable it in Settings → Plugins and approve `ui.frame`.
