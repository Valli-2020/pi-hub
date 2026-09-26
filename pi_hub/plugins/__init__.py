"""Pi Hub plugin system — base types and PluginContext.

Plugins subclass :class:`Plugin` from :mod:`pi_hub.plugins.base` and
register routes/tasks/UI through declarative descriptors.  All system
access goes through :class:`PluginContext` (the security boundary).

Directory layout::

    pi_hub/
        plugins/
            __init__.py     ← this module
            base.py          ← Plugin ABC, PluginContext, descriptors
            manager.py       ← discovery, consent, load, unload, dispatch
            contrib.py       ← API v2 slots, node validation, CSS sanitising
            events.py        ← API v2 event bus

    pi_hub_plugins/          ← user-space plugins (gitignored)
        plugins.json         ← manifest: {"enabled": [...]}
        <name>/
            __init__.py      ← Plugin subclass
            config.json      ← auto-managed per-plugin config
            static/          ← optional assets (served read-only)
"""

from pi_hub.plugins.base import (
    Plugin,
    PluginContext,
    RouteDef,
    TaskDef,
    TabUIDef,
    CardUIDef,
    ActionDef,
    Contribution,
    FrameDef,
    PluginLoadError,
)

__all__ = [
    "Plugin",
    "PluginContext",
    "RouteDef",
    "TaskDef",
    "TabUIDef",
    "CardUIDef",
    "ActionDef",
    "Contribution",
    "FrameDef",
    "PluginLoadError",
    "get_manager",
    "store",
]


def get_manager() -> "PluginManager":
    """Return the singleton :class:`PluginManager` (lazy import avoids
    circular dependency with the ``pi_hub.config`` import chain)."""
    from pi_hub.plugins.manager import PluginManager
    return PluginManager.get_instance()
