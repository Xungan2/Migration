"""Business-path tests for durable handoffs.

These tests keep Porter's orchestration and handoff implementation real.  Only
the provider process transport and host build/boot effects are replaced, so the
records, documents, prompts, validators, and retry decisions all cross their
normal production seams.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from porter.bootstrap import mapping
from porter.common import agent
from porter.divide import index
from porter.divide import run as divide_run
from porter.env import extract
from porter.handoff import HandoffManager, TaskSpec, run_task
from porter.loop import errorloop
from porter.loop import p4
from porter.loop import probes


def _completed(stdout: str = "", returncode: int = 0):
    return mock.Mock(returncode=returncode, stdout=stdout, stderr="")


def _json_block(value: dict) -> str:
    return "```json\n" + json.dumps(value, ensure_ascii=False) + "\n```"


def _event(session_id: str, value: dict) -> str:
    return json.dumps({
        "type": "text",
        "sessionID": session_id,
        "part": {"text": _json_block(value)},
    }) + "\n"


class HandoffPipelineTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="porter_handoff_pipeline_")
        self.root = Path(self.temp.name)
        self.ws = self.root / "workspace"
        self.ws.mkdir()
        # Keep all optional knowledge drafts inside the disposable workspace.
        (self.ws / "knowledge" / "temp").mkdir(parents=True)
        self.manager = HandoffManager(self.ws)

    def tearDown(self):
        self.temp.cleanup()

    def _publish(self, task_id: str, artifact: Path, summary: str):
        return self.manager.import_history(
            task_id,
            summary=summary,
            artifacts=(artifact,),
            verification=("Fixture represents an accepted upstream business result.",),
        )

    def test_p1_provider_retry_gets_failure_handoff_and_reviewed_strategy(self):
        p1_dir = self.ws / "P1"
        (p1_dir / "logs").mkdir(parents=True)
        strategy = p1_dir / "strategy.md"
        strategy_text = "# REVIEWED STRATEGY\nAssign the probe path to core."
        strategy.write_text(strategy_text, encoding="utf-8")
        (self.ws / "project.json").write_text(
            json.dumps({"linux_driver": str(self.root / "driver"),
                        "category": ["net"]}), encoding="utf-8")
        self._publish("p1.strategy", strategy, "The divide strategy was reviewed.")

        entries = [index.Entry(line=1, kind="func", symbol="probe",
                               start=1, end=3)]
        provider_outputs = iter([
            _json_block({"assignments": {}}),
            _json_block({"whole_file": "core",
                         "module_desc": {"core": "probe lifecycle"}}),
        ])
        prompts: list[str] = []

        def transport(args, **kwargs):
            self.assertEqual("opencode", args[0])
            prompts.append(kwargs["input"])
            return _completed(next(provider_outputs))

        with self.manager.start(TaskSpec(
                "p1.divide.pipeline", ("p1.strategy",), (strategy,))) as parent:
            with mock.patch.object(agent.subprocess, "run", side_effect=transport), \
                    redirect_stdout(StringIO()):
                decision, descriptions = divide_run._assign_one_file(
                    "P1 DIVIDE SKILL", strategy_text, "probe.c", entries, p1_dir)
            parent.complete("P1 assignment pipeline accepted the file decision.",
                            verification=("index validator accepted the retry",))

        self.assertEqual({"whole_file": "core",
                          "module_desc": {"core": "probe lifecycle"}}, decision)
        self.assertEqual({"core": "probe lifecycle"}, descriptions)
        self.assertEqual(2, len(prompts))
        self.assertIn("# Failed handoff: `p1.divide.file.probe.c`", prompts[1])
        self.assertIn(strategy_text, prompts[1])
        self.assertLess(prompts[1].index(strategy_text),
                        prompts[1].index("## 任务数据"))

        records = self.manager.inspect("p1.divide.file.probe.c")
        self.assertEqual({"failed", "success"}, {r["status"] for r in records})
        self.assertEqual(2, len({r["execution_id"] for r in records}))
        failed = next(r for r in records if r["status"] == "failed")
        fail_doc = Path(failed["document"]["absolute_path"])
        self.assertTrue(fail_doc.is_file())
        self.assertEqual("handoff-fail.md", fail_doc.name)
        delivered = next(r for r in records if r["status"] == "success") \
            ["inputs"]["delivered"]
        self.assertIn("p1.strategy", {item["task_id"] for item in delivered})

    def test_p2_batches_have_stable_ids_and_chain_real_mapping_handoffs(self):
        driver_root = self.root / "linux-driver"
        target_os = self.root / "target-os"
        driver_root.mkdir()
        target_os.mkdir()
        evidence = target_os / "api.rs"
        evidence.write_text("pub fn mapped_api() {}\n", encoding="utf-8")
        (self.ws / "project.json").write_text(
            json.dumps({"linux_driver": str(driver_root),
                        "target_os": str(target_os)}), encoding="utf-8")

        p2_reports = self.ws / "P2" / "reports"
        p2_reports.mkdir(parents=True)
        symbols = [f"api_{number:02d}" for number in range(36)]
        spine = {
            "domains": {
                "net": {"driver_included": True, "symbols": symbols,
                        "included_by_modules": ["core"]},
            },
            "stats": {"unresolved": 0},
        }
        spine_path = p2_reports / "spine_api.json"
        spine_path.write_text(json.dumps(spine), encoding="utf-8")
        mapping_path = self.ws / "P2" / "mapping.json"
        mapping_path.write_text(json.dumps({
            "entries": [],
            "redesigns": [{"id": "seed", "target_approach": "already reviewed"}],
            "meta": {},
        }), encoding="utf-8")
        self._publish("p1.resolve", self.ws / "project.json",
                      "P1 module ownership was accepted.")

        def entries_for(batch: list[str]) -> list[dict]:
            return [{
                "linux_api": symbol,
                "kind": "function",
                "verdict": "direct",
                "target": "crate::mapped_api",
                "evidence": "api.rs:1",
                "notes": "verified by the target-tree fixture",
                "risk": "none",
                "confidence": "high",
                "domain": "net",
            } for symbol in batch]

        outputs = iter([
            _json_block({"entries": entries_for(symbols[:35])}),
            _json_block({"entries": entries_for(symbols[35:])}),
        ])
        prompts: list[str] = []

        def transport(args, **kwargs):
            self.assertEqual("opencode", args[0])
            prompts.append(kwargs["input"])
            return _completed(next(outputs))

        with self.manager.start(TaskSpec(
                "p2.map.pipeline", ("p1.resolve",), (spine_path,))) as parent:
            with mock.patch.object(agent.subprocess, "run", side_effect=transport), \
                    redirect_stdout(StringIO()):
                rc = mapping.run_map(self.ws, driver_root, target_os)
            self.assertEqual(0, rc)
            parent.complete("Both mapping batches passed target-tree validation.",
                            artifacts=(mapping_path,),
                            verification=("36 expected symbols are mapped",))

        batch_tasks = {record["task_id"] for record in self.manager.inspect()
                       if record["task_id"].startswith("p2.map.batch.net.")}
        self.assertEqual(2, len(batch_tasks))
        first_task = next(task for task in batch_tasks if ".net.0." in task)
        second_task = next(task for task in batch_tasks if ".net.1." in task)
        self.assertNotEqual(first_task, second_task)
        self.assertEqual("success", self.manager.inspect(first_task)[0]["status"])
        second_record = self.manager.inspect(second_task)[0]
        self.assertEqual("success", second_record["status"])
        self.assertIn(first_task, second_record["dependencies"])
        self.assertNotIn(second_task, second_record["dependencies"])
        self.assertEqual(2, len(prompts))
        self.assertIn(f"# Handoff: `{first_task}`", prompts[1])
        self.assertIn(str(spine_path.resolve()), prompts[1])

        accepted = json.loads(mapping_path.read_text(encoding="utf-8"))["entries"]
        self.assertEqual(set(symbols), {item["linux_api"] for item in accepted})
        self.assertTrue(all(item["evidence"] == "api.rs:1" for item in accepted))

        # A cache revalidation creates new executions for the same logical
        # batches.  It must neither rename the tasks nor call a provider.
        with self.manager.start(TaskSpec(
                "p2.map.pipeline.recheck", ("p1.resolve",),
                (spine_path, mapping_path))) as recheck:
            with mock.patch.object(
                    agent.subprocess, "run",
                    side_effect=AssertionError("cache recheck called provider")), \
                    redirect_stdout(StringIO()):
                self.assertEqual(0, mapping.run_map(self.ws, driver_root, target_os))
            recheck.complete("Existing mapping passed current batch validation.",
                             artifacts=(mapping_path,),
                             verification=("same logical batch IDs were reused",))
        rechecked_tasks = {
            record["task_id"] for record in self.manager.inspect()
            if record["task_id"].startswith("p2.map.batch.net.")
        }
        self.assertEqual(batch_tasks, rechecked_tasks)
        self.assertTrue(all(len(self.manager.inspect(task)) == 2
                            for task in batch_tasks))

    def test_p4_retry_reuses_first_slice_and_delivers_both_required_handoffs(self):
        module = "core"
        driver_root = self.root / "e1000"
        target_os = self.root / "target-os"
        driver_root.mkdir()
        crate = target_os / "kernel" / "core" / "comps" / driver_root.name
        (crate / "src").mkdir(parents=True)
        (crate / "src" / "lib.rs").write_text("pub fn existing() {}\n",
                                                encoding="utf-8")
        (self.ws / "project.json").write_text(json.dumps({
            "linux_driver": str(driver_root),
            "target_os": str(target_os),
        }), encoding="utf-8")
        (self.ws / "runner.json").write_text("{}\n", encoding="utf-8")

        module_dir = self.ws / "P1" / "modules" / module
        module_dir.mkdir(parents=True)
        source = module_dir / "driver.c"
        source.write_text("".join(f"/* source line {n} */\n" for n in range(1, 902)),
                          encoding="utf-8")
        (module_dir / "module.json").write_text(json.dumps({
            "name": module, "function": "device lifecycle",
        }), encoding="utf-8")

        reports = self.ws / "P3" / module / "reports"
        reports.mkdir(parents=True)
        surface_path = reports / "surface.json"
        surface_path.write_text(json.dumps({
            "mapped_by_verdict": {}, "missing_by_domain": {},
        }), encoding="utf-8")
        criteria_path = reports / "criteria.json"
        criteria_path.write_text(json.dumps({"criteria": []}), encoding="utf-8")
        gap_path = reports / "gap_decisions.json"
        gap_path.write_text(json.dumps({"decisions": []}), encoding="utf-8")
        p2_dir = self.ws / "P2"
        p2_dir.mkdir()
        mapping_path = p2_dir / "mapping.json"
        mapping_path.write_text(json.dumps({"entries": [], "redesigns": []}),
                                encoding="utf-8")
        (p2_dir / "mapping.md").write_text("# empty fixture mapping\n",
                                            encoding="utf-8")
        self._publish(f"loop.module.{module}.p3", criteria_path,
                      "P3 surface, gap decisions, and criteria were accepted.")

        calls: list[tuple[list[str], str]] = []
        replies: list[object] = [
            _completed(_event("slice-one", {
                "phase": "done", "status": "done",
                "files": ["src/lib.rs"], "notes": "first slice migrated",
            })),
            subprocess.TimeoutExpired(
                cmd=["opencode"], timeout=2,
                output=_event("dead-slice-two", {"phase": "run_static"}),
                stderr=""),
            _completed(_event("fresh-slice-two", {
                "phase": "done", "status": "done",
                "files": ["src/lib.rs"], "notes": "second slice migrated",
            })),
        ]

        def transport(args, **kwargs):
            self.assertEqual("opencode", args[0])
            calls.append((list(args), kwargs["input"]))
            reply = replies.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply

        phase_task = f"loop.module.{module}.p4"
        phase_spec = TaskSpec(
            phase_task, (f"loop.module.{module}.p3",),
            (mapping_path, criteria_path, gap_path, source),
            "P4 business migration",
        )

        def run_phase():
            return run_task(
                self.ws, phase_spec,
                lambda: p4.run_p4(self.ws, module, [module]),
                success=lambda value: value == 0,
                summary=lambda value: f"P4 business result rc={value}.",
                artifacts=lambda value: (crate,) if value == 0 else (),
                verification=lambda value: (f"run_p4 acceptance rc={value}",),
            )

        with mock.patch.object(agent.subprocess, "run", side_effect=transport), \
                mock.patch.object(p4.probe_mod, "probe_build",
                                  return_value={"ok": True, "detail": "rc=0"}), \
                mock.patch.object(p4.probe_lib, "boot_and_log",
                                  return_value=(True, "READY", "stdout")), \
                redirect_stdout(StringIO()):
            self.assertEqual(1, run_phase())
            self.assertEqual(0, run_phase())

        self.assertEqual([], replies)
        self.assertEqual(3, len(calls), "the accepted first slice must not run again")
        self.assertTrue(all("--session" not in args for args, _ in calls))
        retry_prompt = calls[2][1]
        first_slice_task = f"{phase_task}.slice.driver.c.1-900"
        second_slice_task = f"{phase_task}.slice.driver.c.901-901"
        self.assertIn(f"# Handoff: `{first_slice_task}`", retry_prompt)
        self.assertIn(f"# Failed handoff: `{second_slice_task}`", retry_prompt)
        self.assertIn(str(source.resolve()), retry_prompt)

        first_records = self.manager.inspect(first_slice_task)
        second_records = self.manager.inspect(second_slice_task)
        self.assertEqual(1, len(first_records))
        self.assertEqual("success", first_records[0]["status"])
        self.assertEqual({"failed", "success"},
                         {record["status"] for record in second_records})
        self.assertEqual(2, len({record["execution_id"]
                                 for record in second_records}))
        self.assertEqual("success", self.manager.inspect(phase_task)[0]["status"])

    def test_environment_probe_failure_becomes_input_to_fresh_agent_session(self):
        target_os = self.root / "target-os"
        target_os.mkdir()
        material = self.root / "build-notes.md"
        material.write_text("The boot marker is READY.\n", encoding="utf-8")
        project = self.ws / "project.json"
        project.write_text(json.dumps({
            "target_os": str(target_os),
            "linux_driver": str(self.root / "driver"),
        }), encoding="utf-8")

        runner = {
            "build": {"cmd": "fixture-build", "timeout_full_sec": 5,
                      "timeout_inc_sec": 2},
            "boot": {"cmd": "fixture-boot", "timeout_sec": 5,
                     "log_is_stdout": True, "success_pattern": "READY",
                     "panic_pattern": "PANIC"},
            "inject_device": {"mechanism": "env",
                              "env": {"EXTRA": "<DEVICE_ARGS>"},
                              "example_args": {"net": "-device fixture"},
                              "driver_success_pattern": "driver ready",
                              "driver_fail_pattern": "driver failed"},
        }
        provider_reply = _json_block({"runner": runner, "missing": []})
        prompts: list[str] = []
        build_calls = 0

        def transport(args, **kwargs):
            nonlocal build_calls
            if args[0] == "opencode":
                prompts.append(kwargs["input"])
                return _completed(provider_reply)
            self.assertEqual(["bash", "-c"], args[:2])
            command = args[2]
            if command == "fixture-build":
                build_calls += 1
                if build_calls == 1:
                    return _completed("compile error: missing fixture symbol", 1)
                return _completed("build passed")
            if command == "fixture-boot":
                return _completed("READY\ndriver ready\n")
            self.fail(f"unexpected subprocess command: {args}")

        with self.manager.start(TaskSpec(
                "p0.pipeline", materials=(project, target_os, material))) as parent:
            with mock.patch.object(agent.subprocess, "run", side_effect=transport), \
                    redirect_stdout(StringIO()):
                rc = extract.extract_env(self.ws, target_os, [material], ["net"])
            self.assertEqual(0, rc)
            parent.complete("Environment was accepted after all host probes passed.",
                            artifacts=(self.ws / "runner.json",),
                            verification=("build, boot, and device boot passed",))

        self.assertEqual(2, len(prompts))
        self.assertIn("# Failed handoff: `p0.environment.agent`", prompts[1])
        self.assertIn("environment probes did not all pass", prompts[1])
        self.assertIn(str(project.resolve()), prompts[1])
        self.assertIn(str(target_os.resolve()), prompts[1])
        self.assertIn(str(material.resolve()), prompts[1])
        records = self.manager.inspect("p0.environment.agent")
        self.assertEqual({"failed", "success"}, {r["status"] for r in records})
        self.assertEqual(2, len({r["execution_id"] for r in records}))
        final_report = json.loads((self.ws / "P0" / "reports" /
                                   "T3_development.json").read_text(encoding="utf-8"))
        self.assertTrue(final_report["hard_gate_pass"])
        self.assertEqual(["build", "boot", "boot_with_device"],
                         [result["item"] for result in final_report["results"]])

    def test_errorloop_failed_verification_is_handed_to_fresh_repair_session(self):
        target_os = self.root / "target-os"
        target_os.mkdir()
        project = self.ws / "project.json"
        project.write_text(json.dumps({
            "target_os": str(target_os),
            "linux_driver": str(self.root / "driver"),
        }), encoding="utf-8")
        failure = {
            "source": "p5",
            "subject": "core.ready",
            "module": "core",
            "kind": "log_pattern",
            "expr": "READY",
            "detail": "initial boot missed READY",
            "boot_log": "boot stopped before driver init",
            "_workdir": target_os,
        }
        verdict = {
            "status": "done",
            "circuit": "migration",
            "action": "fix-code",
            "evidence": [],
            "summary": "wired the driver initialization path",
            "confidence": 0.9,
        }
        prompts: list[str] = []

        def transport(args, **kwargs):
            self.assertEqual("opencode", args[0])
            prompts.append(kwargs["input"])
            return _completed(_json_block(verdict))

        verification_results = iter([
            (False, {"detail": "post-fix verification still missed READY",
                     "boot_log": "kernel up; driver remained silent"}),
            (True, None),
        ])

        with self.manager.start(TaskSpec(
                "p5.pipeline", materials=(project, target_os))) as parent:
            with mock.patch.dict(os.environ, {"PORTER_SELF_DIAGNOSIS": "1"}), \
                    mock.patch.object(agent.subprocess, "run",
                                      side_effect=transport), \
                    redirect_stdout(StringIO()):
                outcome = errorloop.run_solve_loop(
                    self.ws, failure, lambda: next(verification_results))
            self.assertEqual("solved", outcome["status"])
            parent.complete("The repair passed the second host verification.",
                            verification=("errorloop returned solved",))

        task_id = "errorloop.p5.core.ready.agent"
        self.assertEqual(2, len(prompts))
        self.assertIn(f"# Failed handoff: `{task_id}`", prompts[1])
        self.assertIn("post-fix verification still missed READY", prompts[1])
        records = self.manager.inspect(task_id)
        self.assertEqual({"failed", "success"}, {r["status"] for r in records})
        self.assertEqual(2, len({r["execution_id"] for r in records}))
        failed = next(r for r in records if r["status"] == "failed")
        failure_document = Path(failed["document"]["absolute_path"])
        self.assertTrue(failure_document.is_file())
        self.assertIn("post-fix verification still missed READY",
                      failure_document.read_text(encoding="utf-8"))

    def test_errorloop_escalation_is_a_failed_round_without_host_verification(self):
        target_os = self.root / "target-os"
        target_os.mkdir()
        project = self.ws / "project.json"
        project.write_text(json.dumps({
            "target_os": str(target_os),
            "linux_driver": str(self.root / "driver"),
        }), encoding="utf-8")
        failure = {
            "source": "p5",
            "subject": "core.manual",
            "module": "core",
            "kind": "log_pattern",
            "expr": "READY",
            "detail": "requires a developer decision",
            "_workdir": target_os,
        }
        escalate = {
            "status": "done",
            "circuit": "migration",
            "action": "escalate",
            "evidence": [],
            "summary": "automatic repair cannot choose the hardware policy",
            "confidence": 0.9,
        }
        verify = mock.Mock(side_effect=AssertionError(
            "escalation must not invoke host verification"))

        with self.manager.start(TaskSpec(
                "p5.escalation.pipeline", materials=(project, target_os))) as parent:
            with mock.patch.dict(os.environ, {"PORTER_SELF_DIAGNOSIS": "1"}), \
                    mock.patch.object(
                        agent.subprocess, "run",
                        return_value=_completed(_json_block(escalate))), \
                    redirect_stdout(StringIO()):
                outcome = errorloop.run_solve_loop(self.ws, failure, verify)
            self.assertEqual("escalated", outcome["status"])
            parent.fail("Expected escalation requires external resolution.",
                        verification=("solver returned escalated",))

        verify.assert_not_called()
        round_records = self.manager.inspect("errorloop.p5.core.manual.agent")
        self.assertEqual(1, len(round_records))
        self.assertEqual("failed", round_records[0]["status"])
        self.assertTrue(Path(round_records[0]["document"]["absolute_path"]).is_file())

    def test_probe_compile_fix_reads_previous_host_validation_failure(self):
        module = "modA"
        target_os = self.root / "target-os"
        target_os.mkdir()
        boot_ws = self.ws / "P3" / module
        (boot_ws / "reports").mkdir(parents=True)
        logs_dir = boot_ws / "logs" / "agent"
        logs_dir.mkdir(parents=True)

        project = self.ws / "project.json"
        project.write_text(json.dumps({
            "target_os": str(target_os),
            "linux_driver": str(self.root / "drv"),
            "category": ["net"],
        }), encoding="utf-8")
        boot_log = self.root / "boot.log"
        runner = {"boot": {"log_file": str(boot_log)}}
        (self.ws / "runner.json").write_text(json.dumps(runner),
                                              encoding="utf-8")
        (boot_ws.parent / "runner.json").write_text(json.dumps(runner),
                                                     encoding="utf-8")

        p2_reports = self.ws / "P2" / "reports"
        p2_reports.mkdir(parents=True)
        mapping_path = self.ws / "P2" / "mapping.json"
        mapping_entry = {
            "linux_api": "api_a",
            "kind": "function",
            "verdict": "direct",
            "target": "crate::api_a",
            "evidence": "api.rs:1",
            "notes": "target-tree mapping",
            "risk": "med",
            "confidence": "high",
            "domain": "net",
        }
        mapping_path.write_text(json.dumps({
            "entries": [mapping_entry], "redesigns": [],
        }), encoding="utf-8")
        dormitory = "kernel/core/comps/drv/src/probes.rs"
        (p2_reports / "scaffold_manifest.json").write_text(json.dumps({
            "dormitory": dormitory,
            "language": "rust",
        }), encoding="utf-8")
        criteria = boot_ws / "reports" / "criteria.json"
        criteria.write_text(json.dumps({"criteria": []}), encoding="utf-8")
        self._publish(f"loop.module.{module}.p3.criteria", criteria,
                      "P3 criteria generation was accepted.")

        replies = iter([
            _json_block({"probes": [{
                "name": "p_a", "code": "fn p_a() { broken_v1(); }",
                "claim": "api_a",
            }]}),
            _json_block({"probes": [{
                "name": "p_a", "code": "fn p_a() { broken_v2(); }",
                "claim": "api_a",
            }]}),
            _json_block({"probes": [{
                "name": "p_a", "code": "fn p_a() { fixed(); }",
                "claim": "api_a",
            }]}),
        ])
        provider_prompts: list[str] = []

        def transport(args, **kwargs):
            self.assertEqual("opencode", args[0])
            provider_prompts.append(kwargs["input"])
            return _completed(next(replies))

        build_round = 0

        def host_build(phase_ws, _target_os, _runner, *, label):
            nonlocal build_round
            build_round += 1
            build_output = ("error[E0425]: unresolved probe helper "
                            f"during host validation round {build_round}")
            dorm_path = target_os / dormitory
            source_lines = dorm_path.read_text(encoding="utf-8").splitlines()
            probe_line = next(i for i, line in enumerate(source_lines, 1)
                              if "fn p_a" in line)
            log_path = Path(phase_ws) / "logs" / f"{label}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                f"{build_output}\n --> {dorm_path}:{probe_line}:5\n",
                encoding="utf-8")
            return {"ok": build_round >= 3,
                    "detail": ("rc=0" if build_round >= 3
                               else f"rc=1 {build_output}")}

        def host_boot(*_args, **_kwargs):
            boot_log.write_text("READY\nPROBE_p_a PASS\n", encoding="utf-8")
            return {"ok": True}

        registry = boot_ws / "reports" / "probes.json"
        parent_spec = TaskSpec(
            "probe.pipeline", (f"loop.module.{module}.p3.criteria",),
            (project, mapping_path, p2_reports / "scaffold_manifest.json"),
            "probe generation and host validation pipeline",
        )
        with self.manager.start(parent_spec) as parent:
            with mock.patch.object(agent.subprocess, "run", side_effect=transport), \
                    mock.patch.object(probes.probe_mod, "probe_build",
                                      side_effect=host_build), \
                    mock.patch.object(probes.probe_mod, "probe_boot_with_device",
                                      side_effect=host_boot), \
                    redirect_stdout(StringIO()):
                rc = probes.run_probe_lifecycle(
                    self.ws, target_os,
                    {"linux_driver": str(self.root / "drv"),
                     "category": ["net"]},
                    [module], registry, label="PIPE",
                    todo_entries=[mapping_entry], logs_dir=logs_dir,
                    boot_ws=boot_ws,
                )
            self.assertEqual(0, rc)
            parent.complete("Probe passed after two compile repair proposals.",
                            artifacts=(registry, target_os / dormitory),
                            verification=("third host build and boot passed",))

        self.assertEqual(3, len(provider_prompts))
        second_fix_prompt = provider_prompts[2]
        validation_task = "probe.P3modA.validation"
        fix_task = "probe.compile-fix.PIPE"
        self.assertIn(f"# Failed handoff: `{validation_task}`",
                      second_fix_prompt)
        self.assertIn("host validation round 2", second_fix_prompt)
        validation_records = self.manager.inspect(validation_task)
        self.assertEqual(2, sum(record["status"] == "failed"
                                for record in validation_records))
        self.assertEqual(1, sum(record["status"] == "success"
                                for record in validation_records))
        self.assertEqual(3, len({record["execution_id"]
                                 for record in validation_records}))
        fix_records = self.manager.inspect(fix_task)
        self.assertEqual(2, len(fix_records))
        self.assertTrue(all(record["status"] == "success"
                            for record in fix_records))
        second_fix = next(
            record for record in fix_records
            if any("_fix_r2_" in run["original_prompt_path"]
                   for run in record["provider_runs"])
        )
        delivered_failures = [item for item in second_fix["inputs"]["delivered"]
                              if item["task_id"] == validation_task]
        self.assertEqual(1, len(delivered_failures))
        self.assertEqual("failed", delivered_failures[0]["status"])


if __name__ == "__main__":
    unittest.main()
