import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from porter import pre_mono
from porter.handoff import latest_success, publish_handoff
from porter.workspace import append_runbook, append_runner


class TestPreMono(unittest.TestCase):
    def test_tool_log_and_runner_are_separate(self):
        with TemporaryDirectory() as raw:
            ws = Path(raw)
            append_runbook(ws, "pre-mono", 0, ["porter", "pre-mono"])
            self.assertIn("porter pre-mono", (ws / "runbook.md").read_text())
            self.assertFalse((ws / "runner.md").exists())
            append_runner(ws, "模块编译", 1, ["make", "module"])
            runner = (ws / "runner.md").read_text()
            self.assertIn("make module", runner)
            self.assertNotIn("porter pre-mono", runner)

    def test_agent_owns_division_and_publishes_handoff(self):
        with TemporaryDirectory() as raw:
            ws = Path(raw)
            (ws / "prepare").mkdir()
            (ws / "project.json").write_text("{}", encoding="utf-8")
            (ws / "goals.md").write_text("goal", encoding="utf-8")
            (ws / "prepare" / "state.json").write_text(
                json.dumps({"status": "complete"}), encoding="utf-8")
            (ws / "runner.md").write_text(
                "# runner\n## 第一部分：构建/编译\n### 1. 模块编译\n"
                "### 2. 镜像编译\n## 第二部分：启动\n### 1. 设备自启动\n"
                "### 2. 设备注入与交互\n## 第三部分：单元测试\n",
                encoding="utf-8")
            publish_handoff(ws, "prepare", summary="prepare pass",
                            artifacts=[ws / "project.json", ws / "goals.md",
                                       ws / "prepare" / "state.json", ws / "runner.md"])

            def fake_agent(prompt, workdir, log_stem, **kwargs):
                self.assertIn("module-divsion.json", prompt)
                self.assertIn("Porter CLI", prompt)
                (ws / "module-divsion.json").write_text(json.dumps({
                    "version": 1, "order": ["m"], "driver_home": "home/m",
                    "modules": {"m": {"source_files": ["a.c"],
                                        "target": "home/m", "depends_on": [],
                                        "verification": ["ut"], "status": "planned",
                                        "description": "m"}},
                    "unknown": []}), encoding="utf-8")
                (ws / "module-divsion.md").write_text("# division\n", encoding="utf-8")
                (ws / "migration-plan.json").write_text(json.dumps({
                    "order": ["m"], "driver_home": "home/m",
                    "modules": {"m": {"depends_on": []}}}), encoding="utf-8")
                (ws / "migration-plan.md").write_text("# plan\n", encoding="utf-8")
                module = ws / "mono-input" / "modules" / "m"
                module.mkdir(parents=True)
                (module / "module.json").write_text("{}", encoding="utf-8")
                (module / "spec.md").write_text("# m\n", encoding="utf-8")
                return 0, "written"

            with mock.patch.object(pre_mono.agent, "run_agent", fake_agent):
                self.assertEqual(pre_mono.run(ws), 0)
            self.assertTrue(latest_success(ws, "pre-mono"))
            self.assertTrue((ws / "module-divsion.json").exists())
            self.assertFalse((ws / "P1" / "modules" / "deps.json").exists())


if __name__ == "__main__":
    unittest.main()
