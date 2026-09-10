"""Prerequisite gating uses fresh provider findings and real source evidence."""

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from porter.bootstrap import scaffold_p0
from porter.env import prerequisites as pre
from porter.handoff import HandoffManager
from porter.loop import gates


class PrerequisitesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.ws, self.source, self.target = root / "ws", root / "linux", root / "target"
        self.driver = self.source / "drivers/device"
        for path in (self.ws, self.driver, self.source / "include/linux", self.target):
            path.mkdir(parents=True, exist_ok=True)
        (self.ws / "P0/reports").mkdir(parents=True)
        (self.driver / "core.c").write_text("register_device(bus);\n")
        (self.source / "bus.c").write_text("controller_transfer();\n")
        (self.source / "controller.c").write_text("controller_register();\n")
        (self.target / "platform.rs").write_text("platform_init();\n")
        (self.ws / "project.json").write_text(json.dumps({
            "linux_driver": str(self.driver), "target_os": str(self.target), "category": ["other"]}))
        self.calls = []
        patch = mock.patch.dict("os.environ", {"PORTER_NO_AGENT": "1", "PORTER_VCS": "0"})
        patch.start()
        self.addCleanup(patch.stop)

    def report(self, available=False):
        deps = []
        for key, needs in (("controller", []), ("bus", ["controller"])):
            deps.append({
                "id": key, "capability": key, "reason": "Device registration requires this provider",
                "status": "available" if available else "missing", "depends_on": needs,
                "source_files": [f"{key}.c"],
                "source_evidence": [{"path": f"{key}.c", "line": 1, "quote": "controller_"}],
                "target_status": "Available" if available else "No matching provider in target",
                "target_evidence": ([{"path": "support.rs", "line": 1, "quote": "real_bus_provider"}]
                                    if available else []),
                "action": "Provide the native provider and wiring", "acceptance": ["Provider is linked and enumerates the selected device"]})
        return {"subject": "Device driver", "platform": "Test platform", "binding": "register_device(bus)",
                "source_evidence": [{"path": "drivers/device/core.c", "line": 1, "quote": "register_device"}],
                "dependencies": list(reversed(deps)), "handoff_summary": "Prerequisite analysis"}

    def provider(self, message, **kwargs):
        self.assertEqual(kwargs["task"]["step"], "p0.prerequisites.discover")
        self.calls.append(message)
        output = Path(re.findall(r"输出文件：`([^`]+)`", message)[-1])
        output.write_text(json.dumps(self.report((self.target / "support.rs").exists())))
        return 0, json.dumps({"type": "step_start", "sessionID": "prerequisite-session"})

    def test_block_before_scaffold_recheck_and_resume(self):
        with mock.patch.object(scaffold_p0.agent, "_opencode_json_runner", side_effect=self.provider), \
                mock.patch.object(scaffold_p0.recipe_apply, "apply_recipe", side_effect=AssertionError("blocked")):
            self.assertEqual(scaffold_p0.prepare(self.ws, self.target), 3)
            tasks = json.loads((self.ws / pre.TASKS).read_text())
            self.assertEqual([t["id"] for t in tasks], ["controller", "bus"])
            self.assertEqual(tasks[1]["blocked_by"], ["controller"])
            self.assertEqual(tasks[0]["linux_reference_root"], str(self.source))
            self.assertIn(str(self.source / "controller.c"), (self.ws / pre.TASK_VIEW).read_text())
            self.assertFalse(pre.is_ready(self.ws, self.target))
            # Even an old scaffold and a manually cleared gate cannot bypass fresh analysis.
            manifest = self.ws / "P2/reports/scaffold_manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text('{"status":"verified"}')
            gates.GateLedger(self.ws).load().mark(pre.GATE, "resolved")
            self.assertEqual(scaffold_p0.prepare(self.ws, self.target), 3)
            self.assertEqual(gates.GateLedger(self.ws).load().find(pre.GATE)["status"], "open")
            (self.target / "support.rs").write_text("real_bus_provider();\n")
            self.assertEqual(pre.run(self.ws, self.target), 0)
            self.assertTrue(pre.is_ready(self.ws, self.target))
            self.assertEqual(json.loads((self.ws / pre.TASKS).read_text()), [])
            self.assertEqual(gates.GateLedger(self.ws).load().find(pre.GATE)["status"], "resolved")
            self.assertEqual(len(self.calls), 3)
            # An unchanged fresh result retains the readiness handoff for downstream reuse.
            prior = HandoffManager(self.ws).require_success(pre.GATE)["execution_id"]
            self.assertEqual(pre.run(self.ws, self.target), 0)
            self.assertEqual(HandoffManager(self.ws).require_success(pre.GATE)["execution_id"], prior)
            (self.target / "support.rs").write_text("provider_removed();\n")
            self.assertFalse(pre.is_ready(self.ws, self.target))

    def test_reject_unsubstantiated_available_cycles_and_escaped_sources(self):
        value = self.report()
        pre.validate(value, self.source, self.target)
        for change in ("available", "cycle", "escape", "unlisted"):
            bad = copy.deepcopy(value)
            if change == "available":
                bad["dependencies"][0]["status"] = "available"
            elif change == "cycle":
                bad["dependencies"][1]["depends_on"] = ["bus"]
            elif change == "escape":
                bad["dependencies"][0]["source_files"] = ["../target/platform.rs"]
            else:
                bad["dependencies"][0]["depends_on"] = ["not-listed"]
            with self.subTest(change=change), self.assertRaises(ValueError):
                pre.validate(bad, self.source, self.target)
        value["dependencies"][0]["status"] = "unknown"
        pre.validate(value, self.source, self.target)
        self.assertEqual(len(pre._write_tasks(self.ws, value, self.source, self.target)), 2)

    def test_empty_dependencies_require_source_identity_and_failed_provider_does_not_pass(self):
        value = self.report()
        value["dependencies"] = []
        pre.validate(value, self.source, self.target)
        value["source_evidence"] = []
        with self.assertRaises(ValueError):
            pre.validate(value, self.source, self.target)
        with mock.patch.object(scaffold_p0.agent, "_opencode_json_runner", return_value=(-1, "timeout")):
            with self.assertRaisesRegex(RuntimeError, "provider rc=-1"):
                pre.run(self.ws, self.target)
        self.assertFalse(pre.is_ready(self.ws, self.target))


if __name__ == "__main__":
    unittest.main()
