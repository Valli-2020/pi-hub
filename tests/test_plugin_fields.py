"""Smoke test for the plugin form-dialog core change (7.7).

Covers: ActionDef accepts `fields`; _serialize_ui carries the field
schema (types/defaults) into the /api/plugins/list payload; actions
without fields serialize exactly as before (backwards compatible).
Run: python3 tests/test_plugin_fields.py
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")

from pi_hub.plugins.base import ActionDef, TabUIDef  # noqa: E402
from pi_hub.plugins.manager import _serialize_ui  # noqa: E402

FAILED = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("  ok  " if cond else "FAIL  ") + name + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        FAILED.append(name)


a = ActionDef("config", "Configure…", style="secondary", caps=["admin"],
              fields=[
                  {"name": "base_url", "label": "Panel URL",
                   "type": "text", "default": "https://p.example.com",
                   "placeholder": "https://panel.example.com"},
                  {"name": "api_key", "label": "API key", "type": "password"},
                  {"name": "timeout", "label": "Timeout", "type": "number",
                   "default": 8},
                  {"name": "verify_ssl", "label": "Verify SSL",
                   "type": "checkbox", "default": True},
              ])
tab = TabUIDef(id="t", label="T", icon_svg="<svg/>", actions=[a])
serialized = _serialize_ui([tab])[0]
action = serialized["actions"][0]
check("fields serialized", action.get("fields"), json.dumps(action))
fields = action["fields"]
check("field count", len(fields) == 4, str(len(fields)))
check("text field shape",
      fields[0]["name"] == "base_url" and fields[0]["type"] == "text"
      and fields[0]["default"] == "https://p.example.com"
      and fields[0]["placeholder"] == "https://panel.example.com",
      json.dumps(fields[0]))
check("password type", fields[1]["type"] == "password", fields[1]["type"])
check("number default", fields[2]["type"] == "number" and fields[2]["default"] == 8,
      json.dumps(fields[2]))
check("checkbox default", fields[3]["type"] == "checkbox" and fields[3]["default"] is True,
      json.dumps(fields[3]))

a2 = ActionDef("start", "Start", style="primary", caps=["admin"])
serialized2 = _serialize_ui([TabUIDef(id="t2", label="T2", icon_svg="<svg/>",
                                      actions=[a2])])[0]
check("legacy action shape",
      serialized2["actions"][0]["fields"] == []
      and serialized2["actions"][0]["id"] == "start"
      and serialized2["actions"][0]["style"] == "primary",
      json.dumps(serialized2["actions"][0]))

a3 = ActionDef("x", "X", fields=[{"name": "ok", "type": "text"}, "junk", None])
fields3 = _serialize_ui([TabUIDef(id="t3", label="T3", icon_svg="<svg/>",
                                  actions=[a3])])[0]["actions"][0]["fields"]
check("non-dict fields skipped", len(fields3) == 1 and fields3[0]["name"] == "ok",
      json.dumps(fields3))

json.dumps(serialized)
check("serializable", True)

print("FAILED: " + ", ".join(FAILED) if FAILED else "ALL CHECKS PASSED")
sys.exit(1 if FAILED else 0)
