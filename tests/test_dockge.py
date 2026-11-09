"""Dockge stack listing, command building and capability gating.

Docker and SSH are stubbed; no container is touched.

Run: python3 tests/test_dockge.py
"""

from __future__ import annotations

import json
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, ".")

from pi_hub import auth, dockge, routes, tasks  # noqa: E402

ROOT = "/opt/stacks"
LOCAL = {"id": "pi", "name": "Pi", "stacks_dir": ROOT, "host": None, "url": ""}
LISTING = json.dumps([
    {"Name": "web", "Status": "running(2)", "ConfigFiles": f"{ROOT}/web/compose.yaml"},
    {"Name": "db", "Status": "running(1), exited(1)", "ConfigFiles": f"{ROOT}/db/compose.yaml"},
    {"Name": "old", "Status": "exited(1)", "ConfigFiles": f"{ROOT}/old/compose.yml"},
    {"Name": "dockge", "Status": "running(1)", "ConfigFiles": "/home/pi/dockge/compose.yaml"},
]) + f"\n{dockge._DIRS_MARKER}\n{ROOT}/web/compose.yaml\n{ROOT}/db/compose.yaml\n" \
     f"{ROOT}/old/compose.yml\n{ROOT}/idle/docker-compose.yml\n{ROOT}/Bad.Name/compose.yaml"


def ok(stdout: str = "") -> dict:
    return {"ok": True, "stdout": stdout, "stderr": "", "returncode": 0}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        dockge._cache.clear()
        dockge._busy.clear()
        patches = [
            mock.patch.object(dockge, "_endpoints", return_value=[LOCAL]),
            mock.patch.object(dockge, "dockge_enabled", return_value=True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)


class Listing(Base):
    def stacks(self) -> dict:
        with mock.patch.object(dockge, "_run", return_value=ok(LISTING)):
            return {s["name"]: s for s in dockge.list_stacks()}

    def test_states(self) -> None:
        s = self.stacks()
        self.assertEqual(s["web"]["status"], "running")
        self.assertEqual((s["db"]["status"], s["db"]["running"], s["db"]["total"]), ("partial", 1, 2))
        self.assertEqual(s["old"]["status"], "exited")
        self.assertEqual(s["idle"]["status"], "inactive")

    def test_outside_stacks_dir_is_read_only(self) -> None:
        s = self.stacks()
        self.assertFalse(s["dockge"]["managed"])
        self.assertTrue(s["web"]["managed"])

    def test_invalid_directory_name_is_hidden(self) -> None:
        self.assertNotIn("Bad.Name", self.stacks())

    def test_unreachable_host_reports_error(self) -> None:
        bad = {"ok": False, "stdout": "", "stderr": "ssh failed", "returncode": None}
        with mock.patch.object(dockge, "_run", return_value=bad):
            self.assertEqual(dockge.list_stacks(), [])
            self.assertFalse(dockge.list_endpoints()[0]["online"])


class Container(unittest.TestCase):
    def test_commands_are_wrapped_in_pct_exec(self) -> None:
        ep = dict(LOCAL, host={"id": "pve"}, pct=110)
        with mock.patch.object(dockge.hosts, "ssh_result", return_value=ok()) as ssh:
            dockge._run(ep, "cd /opt/stacks/web && docker compose stop", 5)
        self.assertEqual(ssh.call_args.args[1],
                         "pct exec 110 -- sh -c 'cd /opt/stacks/web && docker compose stop'")

    def test_endpoint_validation(self) -> None:
        def names(extra: dict) -> list:
            cfg = {"dockge": {"endpoints": [dict({"id": "ct"}, **extra)]}}
            with mock.patch.object(dockge, "get_config", return_value=cfg), \
                    mock.patch.object(dockge, "find_host_by_id", return_value={"id": "pve"}):
                return [e["pct"] for e in dockge._endpoints()]

        self.assertEqual(names({"host_id": "pve", "pct": 110}), [110])
        self.assertEqual(names({"host_id": "pve", "pct": "110"}), [110])
        self.assertEqual(names({"host_id": "pve"}), [None])
        for bad in ("110; reboot", 5, True, "-1"):
            self.assertEqual(names({"host_id": "pve", "pct": bad}), [], bad)
        self.assertEqual(names({"pct": 110}), [])


class Actions(Base):
    def run_action(self, stack: str, action: str) -> tuple[dict, int, list]:
        calls: list = []
        done = threading.Event()

        def fake(_ep, cmd, _timeout):
            calls.append(cmd)
            return ok(LISTING) if "compose ls" in cmd else ok()

        def wrapped(*args):
            try:
                original(*args)
            finally:
                done.set()

        original = dockge._run_action
        with mock.patch.object(dockge, "_run", side_effect=fake), \
                mock.patch.object(dockge, "_run_action", side_effect=wrapped):
            data, code = dockge.start_action("pi", stack, action)
            if code == 202:
                self.assertTrue(done.wait(5))
        return data, code, [c for c in calls if "compose ls" not in c]

    def test_commands(self) -> None:
        cases = {
            "start": ["up -d --remove-orphans"],
            "stop": ["stop"],
            "restart": ["restart"],
            "update": ["pull", "up -d --remove-orphans"],
        }
        for action, steps in cases.items():
            _, code, calls = self.run_action("web", action)
            self.assertEqual(code, 202)
            self.assertEqual(calls, [f"cd {ROOT}/web && docker compose {s}" for s in steps])

    def test_update_of_stopped_stack_only_pulls(self) -> None:
        _, code, calls = self.run_action("old", "update")
        self.assertEqual(code, 202)
        self.assertEqual(calls, [f"cd {ROOT}/old && docker compose pull"])

    def test_rejected_requests_run_nothing(self) -> None:
        for stack, action, expected in [
            ("web", "down", 400),
            ("web;reboot", "stop", 404),
            ("../etc", "stop", 404),
            ("missing", "stop", 404),
            ("dockge", "stop", 403),
        ]:
            _, code, calls = self.run_action(stack, action)
            self.assertEqual(code, expected, stack)
            self.assertEqual(calls, [], stack)

    def test_busy_stack_is_refused(self) -> None:
        dockge._busy.add("pi:web")
        _, code, calls = self.run_action("web", "restart")
        self.assertEqual(code, 409)
        self.assertEqual(calls, [])

    def test_failure_is_reported(self) -> None:
        bad = {"ok": False, "stdout": "", "stderr": "pulling\nno such image", "returncode": 1}
        dockge._busy.add("pi:web")
        with mock.patch.object(dockge, "_run", return_value=bad):
            dockge._run_action(LOCAL, "web", "update", ["pull"], "pi:web")
        status = tasks.get_task_status("stack_pi_web")
        self.assertEqual(status["status"], "error")
        self.assertIn("no such image", status["message"])
        self.assertNotIn("pi:web", dockge._busy)


class Permissions(Base):
    def test_post_reaches_route_guard(self) -> None:
        self.assertEqual(auth.classify("POST", "/api/dockge/pi/web/stop"), "read")

    def test_viewer_without_cap_is_refused(self) -> None:
        viewer = {"user": "v", "role": "viewer"}
        with mock.patch.object(auth, "fresh_user", return_value={"role": "viewer", "caps": {}}), \
                mock.patch.object(dockge, "start_action") as action, \
                mock.patch.object(dockge, "stack_logs") as logs:
            _, post = routes.handle_post("/api/dockge/pi/web/stop", {}, {}, session=viewer)
            _, get = routes.handle_get("/api/dockge/pi/web/logs", {}, session=viewer)
        self.assertEqual((post, get), (403, 403))
        action.assert_not_called()
        logs.assert_not_called()

    def test_cap_is_per_stack(self) -> None:
        viewer = {"user": "v", "role": "viewer"}
        rec = {"role": "viewer", "caps": {"stacks": ["pi:web"]}}
        with mock.patch.object(auth, "fresh_user", return_value=rec), \
                mock.patch.object(dockge, "start_action", return_value=({"success": True}, 202)):
            _, allowed = routes.handle_post("/api/dockge/pi/web/stop", {}, {}, session=viewer)
            _, denied = routes.handle_post("/api/dockge/pi/db/stop", {}, {}, session=viewer)
        self.assertEqual((allowed, denied), (202, 403))


if __name__ == "__main__":
    unittest.main()
