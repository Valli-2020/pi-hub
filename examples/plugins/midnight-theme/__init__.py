"""Midnight — a theme / layout / style demo for Pi Hub (Plugin API v2).

Everything here is static: no provider runs, no system capability is
needed.  ``theme`` and ``layout`` are *exclusive* slots — installing this
plugin does nothing until an admin picks it in Settings › Appearance.
"""

from __future__ import annotations

from pi_hub.plugins import Contribution, Plugin


class MidnightTheme(Plugin):
    name = "midnight-theme"
    version = "1.0.0"
    description = "Violet theme, compact layout, softer cards"
    plugin_api_version = 2
    capabilities = ["ui.theme", "ui.layout", "ui.style"]   # must match pihub-plugin.json

    def get_contributions(self):
        return [
            Contribution("theme", "midnight", label="Midnight", static={"tokens": {
                "dark": {
                    "--bg": "#0f0d1a", "--surface": "#171426", "--surface-2": "#1e1a33",
                    "--border": "#2c2745", "--border-strong": "#3d3660",
                    "--text": "#ece9ff", "--muted": "#9a94c4",
                    "--accent": "#a78bfa", "--accent-hover": "#c4b5fd", "--accent-ink": "#1a1030",
                },
                "light": {
                    "--bg": "#f6f4ff", "--surface": "#ffffff", "--surface-2": "#efebff",
                    "--border": "#ddd6fe", "--accent": "#7c3aed", "--accent-hover": "#6d28d9",
                    "--accent-ink": "#ffffff",
                },
            }}),
            Contribution("layout", "compact", label="Compact, Containers first", static={
                "default_view": "containers",
                "nav_order": ["containers", "hosts", "services", "stacks"],
                "density": "compact",
            }),
            Contribution("style", "cards", static={"css": ".hostcard, .card { box-shadow: none; }"}),
        ]
