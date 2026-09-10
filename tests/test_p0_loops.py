"""P0 acceptance regression checks using a local compiler and an interactive shell.

The shell is a guest-transport stand-in, not an OS boot. Provider discovery is
stubbed; build commands, stdin round trips, test commands and gates execute.
"""
import copy
import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from porter.bootstrap import scaffold, run as bootstrap
from porter.env import extract, gate, probe
from porter.handoff.integration import cli_spec, output_artifacts


class P0LoopsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ws = self.root / "ws"
        self.target = self.root / "os"
        self.target.mkdir()
        (self.ws / "P2/reports").mkdir(parents=True)
        (self.ws / "P0/logs").mkdir(parents=True)
        (self.ws / "P0/reports/out").mkdir(parents=True)
        (self.target / "driver").mkdir()
        (self.target / "driver/skeleton.c").write_text('''
int skeleton_test(void) { return 7; }
#ifdef TEST
#include <assert.h>
#include <stdio.h>
int main(void) { assert(skeleton_test() == 7); puts("skeleton_test PASS"); }
#endif
''')
        (self.target / "driver/probes.c").write_text("/* probe dormitory */\n")
        self.manifest = {"driver": "demo", "driver_home": "driver", "status": "applied",
                         "created": ["driver/skeleton.c"],
                         "commit_paths": ["driver/skeleton.c"],
                         "source_paths": ["driver/skeleton.c"],
                         "acceptance_log_patterns": ["CLAIMED demo"],
                         "api_claims": [], "dormitory": "driver/probes.c", "language": "c"}
        self.manifest_path = self.ws.joinpath(*scaffold.MANIFEST_NAME)
        self.manifest_path.write_text(json.dumps(self.manifest))
        (self.ws / "project.json").write_text(json.dumps({
            "linux_driver": str(self.root), "target_os": str(self.target),
            "category": ["block"], "driver_name": "demo"}))
        from porter.env import prerequisites as pre
        from porter.handoff import HandoffManager, TaskSpec
        report = {"subject": "Fixture", "platform": "Local compiler/shell", "binding": "Fixture device",
                  "source_evidence": [{"path": "os/driver/probes.c", "line": 1, "quote": "probe dormitory"}],
                  "dependencies": [], "handoff_summary": "Fixture prerequisites ready"}
        (self.ws / pre.REPORT).write_text(json.dumps(report))
        pre._write_tasks(self.ws, report, self.root, self.target)
        with HandoffManager(self.ws).start(TaskSpec(pre.GATE)) as execution:
            execution.complete("Fixture prerequisites ready", artifacts=tuple(
                self.ws / name for name in (pre.REPORT, pre.TASKS, pre.TASK_VIEW)))
        shell = self.target / "guest.py"
        shell.write_text('''import os
print("BOOTED", flush=True)
if os.environ.get("DEVICE") == "demo":
    print("CLAIMED demo", flush=True)
os.execv("/bin/sh", ["sh", "-i"])
''')
        scope = "from pathlib import Path; print('\\n'.join(p for p in Path('module.d').read_text().split() if p.endswith('.c')))"
        self.sections = {
            "build": {"module_cmd": "cc -shared -fPIC -MMD -MF module.d driver/skeleton.c -o module.so",
                      "cmd": "cc -DTEST driver/skeleton.c -o os.elf",
                      "scope_cmd": "python3 -c " + shlex.quote(scope),
                      "module_artifacts": ["module.so"], "image_artifacts": ["os.elf"],
                      "timeout_full_sec": 10, "timeout_inc_sec": 5},
            "boot": {"boot": {"cmd": "python3 guest.py", "timeout_sec": 5,
                              "log_is_stdout": True, "success_pattern": "BOOTED",
                              "panic_pattern": "PANIC",
                              "interaction": {"prompt_pattern": "[$#] ", "shutdown_cmd": "exit"}},
                     "inject_device": {"mechanism": "env", "env": {"DEVICE": "<DEVICE_ARGS>"},
                                       "example_args": {"block": "demo"},
                                       "driver_success_pattern": "CLAIMED demo"}},
            "unit_test": {"mechanism": "native", "scope": "driver", "test_names": ["skeleton_test"],
                          "cmd": "cc -DTEST driver/skeleton.c -o tests && ./tests",
                          "smoke_cmd": "cc -DTEST driver/skeleton.c -o tests && ./tests",
                          "timeout_sec": 5, "success_pattern": "skeleton_test PASS"}}
        for name in ("porter.bootstrap.candidates.record_candidate", "porter.common.vcs.commit_target"):
            patcher = mock.patch(name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def provider(self, message, **kwargs):
        cap = kwargs["task"]["step"].removeprefix("t3-")
        self.calls.append(cap)
        out = self.ws / "P0/reports/out"
        (out / f"{cap}.json").write_text(json.dumps(self.sections[cap]))
        (out / f"{cap}.md").write_text("\n\n".join(a + "\n\nCompiler/shell fixture evidence."
                                                    for a in extract._required_anchors(cap)))
        return 0, json.dumps({"type": "step_start", "sessionID": f"session-{cap}"})

    def run_loops(self):
        self.calls = []
        with mock.patch.object(extract.agent, "_opencode_json_runner", side_effect=self.provider):
            return extract.extract_env(self.ws, self.target, [], ["block"])

    def test_three_loops_real_execution_gate_and_reuse(self):
        self.assertEqual(self.run_loops(), 0)
        self.assertEqual(self.calls, ["build", "boot", "unit_test"])
        self.assertTrue((self.target / "module.so").is_file())
        self.assertTrue((self.target / "os.elf").is_file())
        self.assertEqual(scaffold.load_manifest(self.ws)["status"], "verified")
        with mock.patch.object(probe, "_run", side_effect=AssertionError("T5 must not execute commands")):
            self.assertTrue(gate.run_gate(self.ws))
        self.assertEqual(self.run_loops(), 0)
        self.assertEqual(self.calls, [])
        (self.target / "driver/skeleton.c").write_text("changed source")
        self.assertFalse(gate.run_gate(self.ws))

    def test_loop_sessions_receive_handoffs_and_timeout_is_terminal(self):
        from porter.handoff import HandoffManager, TaskSpec, prepare_agent_prompt
        manager = HandoffManager(self.ws)
        with manager.start(TaskSpec("p0.scaffold.apply")) as execution:
            execution.complete("Test skeleton applied", artifacts=(self.manifest_path,))
        original = self.provider
        seen = []
        failed = False

        def provider(message, **kwargs):
            nonlocal failed
            seen.append((kwargs["task"]["step"], kwargs["session_id"],
                         prepare_agent_prompt(message, new_session=kwargs["session_id"] is None)))
            if kwargs["task"]["step"] == "t3-boot" and not failed:
                failed = True
                return -1, json.dumps({"type": "step_start", "sessionID": "failed-boot"})
            return original(message, **kwargs)

        self.calls = []
        with mock.patch.object(extract.agent, "_opencode_json_runner", side_effect=provider):
            with self.assertRaisesRegex(RuntimeError, "provider rc=-1"), manager.start(TaskSpec("p0")):
                extract.extract_env(self.ws, self.target, [], ["block"])
            with manager.start(TaskSpec("p0")) as execution:
                self.assertEqual(extract.extract_env(self.ws, self.target, [], ["block"]), 0)
                execution.complete("P0 passed")
        self.assertEqual([s[0] for s in seen], ["t3-build", "t3-boot", "t3-boot", "t3-unit_test"])
        self.assertTrue(all(s[1] is None for s in seen))
        self.assertIn("Test skeleton applied", seen[0][2])
        self.assertIn("P0 build: converged", seen[1][2])
        self.assertIn("Failed handoff", seen[2][2])
        self.assertIn("P0 boot: converged", seen[3][2])
        self.assertTrue(manager.require_success("p0.loop.unit_test"))

    def test_module_failure_stops_full_build(self):
        section = {**self.sections["build"], "module_cmd": "exit 9"}
        result, _ = extract._final_verify(self.ws, self.ws / "P0", "build", section, [], 1, self.target)
        self.assertFalse(result["ok"])
        self.assertFalse((self.target / "os.elf").exists())

    def test_module_cannot_build_image_or_omit_compilation_scope(self):
        for change in ({"module_cmd": self.sections["build"]["module_cmd"] + " && echo image > os.elf"},
                       {"scope_cmd": "true"}):
            with self.subTest(change=change):
                result, _ = extract._final_verify(self.ws, self.ws / "P0", "build",
                                                  {**self.sections["build"], **change}, [], 1, self.target)
                self.assertFalse(result["ok"])
                self.assertIsNone(result["full"])

    def test_stale_module_artifact_cannot_satisfy_build(self):
        section = self.sections["build"]
        result, _ = extract._final_verify(self.ws, self.ws / "P0", "build", section, [], 1, self.target)
        self.assertTrue(result["ok"])
        os.utime(self.target / "module.so", ns=(1, 1))
        result, _ = extract._final_verify(self.ws, self.ws / "P0", "build",
                                          {**section, "module_cmd": "true"}, [], 2, self.target)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["full"])

    def test_boot_echo_or_missing_device_cannot_pass(self):
        self.assertTrue(extract._check_p0_section(
            "boot", {**self.sections["boot"], "build": {"cmd": "unverified"}}))
        bad = self.target / "echo.py"
        bad.write_text('''import sys
print("BOOTED\\nCLAIMED demo\\n# ", flush=True)
print(sys.stdin.readline(), flush=True)
''')
        section = copy.deepcopy(self.sections["boot"])
        section["boot"]["cmd"] = "python3 echo.py"
        result, _ = extract._final_verify(self.ws, self.ws / "P0", "boot", section, ["block"], 1, self.target)
        self.assertFalse(result["ok"])
        self.assertFalse(result["interaction_ok"])
        section = copy.deepcopy(self.sections["boot"])
        section["inject_device"]["example_args"]["block"] = "absent"
        result, _ = extract._final_verify(self.ws, self.ws / "P0", "boot", section, ["block"], 2, self.target)
        self.assertFalse(result["ok"])
        self.assertTrue(result["interaction_ok"])

    def test_exited_guest_does_not_wait_for_background_pipe_holder(self):
        rc, _, replied = probe._run_interactive(
            "sleep 30 & python3 guest.py", self.target, dict(os.environ), 3,
            self.ws / "P0/logs/background-pipe.log", None,
            {"prompt_pattern": "[$#] ", "shutdown_cmd": "exit"})
        self.assertTrue(replied)
        self.assertEqual(rc, 0)

    def test_unrelated_tests_or_failed_assertions_cannot_pass(self):
        for change in ({"test_names": ["other_test"]}, {"scope": "other"},
                       {"smoke_cmd": "echo 'skeleton_test PASS'; exit 1"}):
            result, _ = extract._final_verify(self.ws, self.ws / "P0", "unit_test",
                                              {**self.sections["unit_test"], **change}, [], 1, self.target)
            self.assertFalse(result["ok"])

    def test_qualified_test_name_matches_source_and_full_runtime_name(self):
        source = self.target / "driver/skeleton.c"
        source.write_text(source.read_text().replace(
            'puts("skeleton_test PASS");',
            'fputs("demo::tests::", stdout); puts("skeleton_test PASS");'))
        section = {**self.sections["unit_test"],
                   "test_names": ["demo::tests::skeleton_test"]}
        result, _ = extract._final_verify(self.ws, self.ws / "P0", "unit_test",
                                         section, [], 1, self.target)
        self.assertTrue(result["ok"])
        section["test_names"] = ["other::tests::skeleton_test"]
        result, _ = extract._final_verify(self.ws, self.ws / "P0", "unit_test",
                                         section, [], 2, self.target)
        self.assertFalse(result["ok"])

    def test_later_source_repair_restarts_build(self):
        original = self.provider
        repaired = False
        def provider(message, **kwargs):
            nonlocal repaired
            if kwargs["task"]["step"] == "t3-boot" and not repaired:
                with (self.target / "driver/skeleton.c").open("a") as f:
                    f.write("\n/* repaired wiring */\n")
                repaired = True
            return original(message, **kwargs)
        self.calls = []
        with mock.patch.object(extract.agent, "_opencode_json_runner", side_effect=provider), \
             mock.patch.object(extract, "_final_verify", wraps=extract._final_verify) as verify:
            self.assertEqual(extract.extract_env(self.ws, self.target, [], ["block"]), 0)
        self.assertEqual(self.calls, ["build", "boot", "boot", "unit_test"])
        self.assertEqual([call.args[2] for call in verify.call_args_list],
                         ["build", "build", "boot", "unit_test"])

    def test_failed_reverification_returns_evidence_to_agent(self):
        original = self.provider
        source = self.target / "driver/skeleton.c"
        good_source = source.read_text()
        damaged = False

        def provider(message, **kwargs):
            nonlocal damaged
            cap = kwargs["task"]["step"]
            if cap == "t3-boot" and not damaged:
                source.write_text("invalid C source")
                damaged = True
            elif cap == "t3-build" and damaged:
                self.assertIn("T3_build_verify_r0", message)
                self.assertIn("False", message)
                self.assertTrue((self.ws / "P0/reports/out/build.json").exists())
                source.write_text(good_source + "\n/* repaired */\n")
            return original(message, **kwargs)

        self.calls = []
        with mock.patch.object(extract.agent, "_opencode_json_runner", side_effect=provider), \
             mock.patch.object(extract, "_final_verify", wraps=extract._final_verify) as verify:
            self.assertEqual(extract.extract_env(self.ws, self.target, [], ["block"]), 0)
        self.assertEqual(self.calls, ["build", "boot", "build", "boot", "unit_test"])
        self.assertEqual([call.args[2] for call in verify.call_args_list],
                         ["build", "build", "build", "boot", "unit_test"])

    def test_same_test_command_can_retry_after_script_repair(self):
        original = self.provider
        attempts = 0

        def provider(message, **kwargs):
            nonlocal attempts
            if kwargs["task"]["step"] == "t3-build" and attempts < 2:
                (self.target / "check.sh").write_text(f"exit {1 if attempts == 0 else 0}\n")
                (self.ws / "P0/reports/out/build_test.json").write_text(
                    json.dumps({"cmd": "sh check.sh", "timeout_sec": 5}))
                attempts += 1
                return 0, json.dumps({"type": "step_start", "sessionID": "retry-build"})
            self.assertNotIn("测试请求内容未变化", message)
            return original(message, **kwargs)

        self.calls = []
        with mock.patch.object(extract.agent, "_opencode_json_runner", side_effect=provider):
            self.assertEqual(extract.extract_env(self.ws, self.target, [], ["block"]), 0)
        for n, rc in ((1, 1), (2, 0)):
            result = json.loads((self.ws / f"P0/logs/T3_build_test_r{n}.result.json").read_text())
            self.assertEqual(result["rc"], rc)

    def test_old_runner_does_not_skip_new_acceptance(self):
        (self.ws / "runner.json").write_text("{}")
        self.assertEqual(self.run_loops(), 0)
        self.assertEqual(self.calls, ["build", "boot", "unit_test"])

    def test_default_p2_skips_map_and_scaffold_preserves_mapping(self):
        self.assertEqual(self.run_loops(), 0)
        (self.ws / "P1/modules").mkdir(parents=True)
        (self.ws / "P1/modules/deps.json").write_text('{"order": []}')
        mapping = self.ws / "P2/mapping.json"
        from porter.handoff import HandoffManager, TaskSpec
        from porter.handoff.integration import execute_cli
        from types import SimpleNamespace
        manager = HandoffManager(self.ws)
        for task_id, artifact in (("p0", self.manifest_path), ("p1.resolve", self.ws / "P1/modules/deps.json")):
            with manager.start(TaskSpec(task_id)) as execution:
                execution.complete("Accepted test fixture phase.", artifacts=[artifact],
                                   verification=["Local acceptance executed."])
        args = SimpleNamespace(phase="p2", output_dir=str(self.ws))
        with mock.patch("porter.divide.pruning.require_ready", return_value=0), \
             mock.patch.object(bootstrap.mapping, "run_map", side_effect=AssertionError("P2a disabled")), \
             mock.patch.object(scaffold, "run_scaffold", side_effect=AssertionError("P0 owns skeleton")), \
             mock.patch("porter.loop.gates.checkpoint_enabled", return_value=False):
            self.assertEqual(execute_cli(args, lambda: bootstrap.run_p2(self.ws, self.root, self.target)), 0)
            self.assertEqual(json.loads(mapping.read_text())["entries"], [])
            mapping.write_text('{"entries": [], "retained": true}')
            self.assertEqual(execute_cli(args, lambda: bootstrap.run_p2(self.ws, self.root, self.target)), 0)
            self.assertTrue(json.loads(mapping.read_text())["retained"])
        from types import SimpleNamespace
        spec = cli_spec(SimpleNamespace(phase="p2-probes"), self.ws)
        self.assertEqual(spec.dependencies, ("p0", "p1.resolve"))
        self.assertIn(self.manifest_path, output_artifacts(self.ws, "p0"))


if __name__ == "__main__":
    unittest.main()
