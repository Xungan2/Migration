"""Durable handoff module tests (no provider, network, Docker, or QEMU)."""

from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from porter.handoff import (HandoffError, HandoffManager, Material, NotReady,
                            StaleInputs, TaskSpec, current_execution,
                            prepare_agent_prompt, run_task)
from porter.common import agent
from porter.handoff.core import _agent_summary
from porter.handoff.integration import cli_spec, module_dependencies


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        (self.ws / "project.json").write_text("{}\n", encoding="utf-8")
        self.manager = HandoffManager(self.ws)

    def tearDown(self):
        self.tmp.cleanup()

    def _success(self, task_id, *, deps=(), summary="done", artifact=None):
        spec = TaskSpec(task_id, tuple(deps), ("project.json",))
        with self.manager.start(spec) as execution:
            prepare_agent_prompt("work", new_session=True)
            execution.complete(summary, artifacts=([artifact] if artifact else []),
                               verification=["business validator accepted output"])
            return execution

    def test_missing_handoff_blocks_before_transport(self):
        called = []
        with self.assertRaises(NotReady):
            with self.manager.start(TaskSpec("down", ("missing",))):
                called.append(True)
        self.assertEqual([], called)

    def test_fan_in_delivers_exact_document_versions(self):
        a = self._success("a", summary="A result")
        b = self._success("b", summary="B result")
        with self.manager.start(TaskSpec("join", ("a", "b"))) as run:
            injected = prepare_agent_prompt("join work", new_session=True)
            self.assertIn("A result", injected)
            self.assertIn("B result", injected)
            delivered = run.record["inputs"]["delivered"]
            self.assertEqual({"a", "b"}, {d["task_id"] for d in delivered})
            self.assertEqual(
                {a.execution_id, b.execution_id},
                {d["execution_id"] for d in delivered})
            run.complete("joined", verification=["fan-in accepted"])

    def test_failure_is_persisted_and_retry_is_a_fresh_execution(self):
        with self.manager.start(TaskSpec("retry-me")) as first:
            first.fail("provider timeout", summary="Work state unknown")
        failure = first.directory / "handoff-fail.md"
        self.assertTrue(failure.is_file())

        with self.manager.start(TaskSpec("retry-me")) as second:
            self.assertNotEqual(first.execution_id, second.execution_id)
            prompt = prepare_agent_prompt("retry", new_session=True)
            self.assertIn("provider timeout", prompt)
            second.complete("recovered", verification=["retry accepted"])
        self.assertTrue(failure.is_file(), "old failure must not be overwritten")

    def test_history_never_overwrites_and_latest_success_advances(self):
        first = self._success("stable", summary="v1")
        second = self._success("stable", summary="v2")
        records = self.manager.inspect("stable")
        self.assertEqual(2, len(records))
        self.assertTrue((first.directory / "handoff.md").is_file())
        index = json.loads((second.directory.parent.parent / "index.json")
                           .read_text(encoding="utf-8"))
        self.assertEqual(second.execution_id, index["latest_success"])

    def test_corrupt_dependency_document_blocks(self):
        upstream = self._success("up")
        (upstream.directory / "handoff.md").write_text("tampered\n",
                                                       encoding="utf-8")
        with self.assertRaises(NotReady):
            self.manager.prepare(TaskSpec("down", ("up",)))

    def test_corrupt_record_shape_is_reported_as_not_ready(self):
        upstream = self._success("bad-shape")
        record_path = upstream.directory / "record.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["inputs"] = []
        record_path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(NotReady, "inputs is not an object"):
            self.manager.prepare(TaskSpec("consumer", ("bad-shape",)))

    def test_material_change_before_finish_fails_stale(self):
        material = self.ws / "input.md"
        material.write_text("one\n", encoding="utf-8")
        with self.assertRaises(StaleInputs):
            with self.manager.start(TaskSpec(
                    "mutating-input", materials=(Material(material, True),))) as run:
                material.write_text("two\n", encoding="utf-8")
                run.complete("bad", verification=["would otherwise pass"])
        records = self.manager.inspect("mutating-input")
        self.assertEqual("failed", records[0]["status"])

    def test_nested_child_has_parent_link_without_dependency_deadlock(self):
        with self.manager.start(TaskSpec("parent")) as parent:
            with self.manager.start(TaskSpec("child")) as child:
                self.assertEqual(parent.execution_id,
                                 child.record["parent_execution"]["execution_id"])
                child.complete("child done", verification=["accepted"])
            self.assertIs(parent, current_execution())
            parent.complete("parent done", verification=["accepted"])
        rec = self.manager.inspect("parent")[0]
        self.assertEqual("child", rec["children"][0]["task_id"])

    def test_explicit_historical_import_requires_real_evidence(self):
        artifact = self.ws / "legacy.json"
        artifact.write_text('{"ok": true}\n', encoding="utf-8")
        path = self.manager.import_history(
            "legacy", summary="Imported existing accepted output.",
            artifacts=[artifact], verification=["legacy acceptance report says PASS"])
        self.assertTrue(path.is_file())
        self.manager.prepare(TaskSpec("consumer", ("legacy",)))

    def test_concurrent_attempts_get_unique_directories(self):
        ids = []
        errors = []

        def worker():
            try:
                task_id = f"parallel.{threading.get_ident()}"
                with self.manager.start(TaskSpec(task_id)) as run:
                    ids.append(run.execution_id)
                    run.complete("done", verification=["accepted"])
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], errors)
        self.assertEqual(4, len(set(ids)))
        self.assertEqual(4, len([r for r in self.manager.inspect()
                                if str(r.get("task_id", "")).startswith("parallel.")]))

    def test_same_task_cannot_run_concurrently(self):
        first = self.manager.prepare(TaskSpec("exclusive"))
        with self.assertRaises(NotReady):
            self.manager.prepare(TaskSpec("exclusive"))
        first.fail("test cleanup", summary="not executed")

    def test_historical_import_cannot_race_active_same_task(self):
        artifact = self.ws / "legacy.json"
        artifact.write_text("{}", encoding="utf-8")
        active = self.manager.prepare(TaskSpec("active-import"))
        with self.assertRaises(NotReady):
            self.manager.import_history(
                "active-import", summary="legacy",
                artifacts=(artifact,), verification=("checked",))
        active.fail("test cleanup", summary="not executed")

    def test_missing_declared_artifact_propagates_publication_error(self):
        with self.assertRaises(HandoffError):
            run_task(self.ws, TaskSpec("publisher"), lambda: 0,
                     artifacts=(self.ws / "absent.json",))
        self.assertEqual("failed", self.manager.inspect("publisher")[0]["status"])

    def test_superseded_transitive_dependency_blocks_new_consumer(self):
        self._success("source", summary="source v1")
        self._success("middle", deps=("source",), summary="middle v1")
        self._success("source", summary="source v2")
        with self.assertRaises(NotReady):
            self.manager.prepare(TaskSpec("consumer", ("middle",)))

    def test_transitive_supersession_during_execution_prevents_completion(self):
        self._success("source", summary="source v1")
        self._success("middle", deps=("source",), summary="middle v1")
        with self.assertRaises(StaleInputs):
            with self.manager.start(TaskSpec("consumer", ("middle",))) as run:
                with self.manager.start(TaskSpec(
                        "source", materials=("project.json",),
                        inherit_parent_inputs=False)) as source_v2:
                    source_v2.complete("source v2", verification=["accepted"])
                run.complete("must not publish", verification=["not accepted"])
        self.assertEqual("failed", self.manager.inspect("consumer")[0]["status"])

    def test_running_new_transitive_dependency_blocks_start_and_finish(self):
        self._success("source", summary="source v1")
        self._success("middle", deps=("source",), summary="middle v1")
        with self.manager.start(TaskSpec("consumer", ("middle",))) as consumer:
            with self.manager.start(TaskSpec(
                    "source", inherit_parent_inputs=False)) as source_v2:
                with self.assertRaises(NotReady):
                    self.manager.prepare(TaskSpec("late-consumer", ("middle",)))
                with self.assertRaises(StaleInputs):
                    consumer.complete("unsafe", verification=["should fail"])
                source_v2.fail("new attempt aborted", summary="no result")
        self.assertEqual("failed", self.manager.inspect("consumer")[0]["status"])

    def test_rejected_result_with_missing_artifact_returns_original_result(self):
        result = run_task(
            self.ws, TaskSpec("rejected"), lambda: 3,
            artifacts=(self.ws / "absent.json",))
        self.assertEqual(3, result)
        record = self.manager.inspect("rejected")[0]
        self.assertEqual("failed", record["status"])
        self.assertIn("declared output artifact missing", record["reason"])

    def test_child_inherits_parent_inputs_and_materials_in_provider_prompt(self):
        self._success("upstream", summary="required upstream result")
        material = self.ws / "original.txt"
        material.write_text("source material", encoding="utf-8")
        with self.manager.start(TaskSpec(
                "parent", ("upstream",), (material,))) as parent:
            with self.manager.start(TaskSpec("p3-child")) as child:
                prompt = prepare_agent_prompt("child work", new_session=True)
                self.assertIn("required upstream result", prompt)
                self.assertIn(str(material.resolve()), prompt)
                delivered = child.record["inputs"]["delivered"]
                self.assertEqual(["upstream"], [d["task_id"] for d in delivered])
                self.assertEqual(parent.execution_id,
                                 child.record["inherited_inputs_from"]["execution_id"])
                child.complete("child done", verification=["accepted"])
            parent.complete("parent done", verification=["accepted"])

    def test_parent_input_inheritance_cannot_create_self_dependency(self):
        self._success("shared", summary="shared v1")
        with self.manager.start(TaskSpec("parent", ("shared",))) as parent:
            with self.assertRaises(NotReady):
                self.manager.prepare(TaskSpec("shared"))
            parent.fail("test cleanup", summary="child was rejected")

    def test_large_child_index_stays_bounded_and_consumable(self):
        with self.manager.start(TaskSpec("large-parent")) as parent:
            for number in range(100):
                with self.manager.start(TaskSpec(f"child.{number}")) as child:
                    child.complete("done", verification=["accepted"])
            parent.complete("parent complete", verification=["accepted"])
        document = (parent.directory / "handoff.md").read_text(encoding="utf-8")
        self.assertLess(len(document), 24_000)
        self.assertIn("earlier child executions omitted", document)
        with self.manager.start(TaskSpec(
                "large-consumer", ("large-parent",))) as consumer:
            consumer.complete("accepted", verification=["read parent handoff"])

    def test_independent_module_bypass_uses_only_actual_shared_producer(self):
        self._success("p2.probes", summary="probe substrate")
        deps_path = self.ws / "P1" / "modules" / "deps.json"
        deps_path.parent.mkdir(parents=True)
        deps_path.write_text(json.dumps({
            "order": ["parked", "independent"],
            "edges": {"parked": [], "independent": []},
        }), encoding="utf-8")
        deps = module_dependencies(self.ws, "independent", "p3")
        self.assertEqual(("p2.probes",), deps)
        with self.manager.start(TaskSpec(
                "loop.module.independent.p3", deps)) as run:
            run.complete("bypass accepted", verification=["source deps empty"])

        deps_path.write_text(json.dumps({
            "order": ["parked", "dependent"],
            "edges": {"parked": [], "dependent": ["parked"]},
        }), encoding="utf-8")
        strict = module_dependencies(self.ws, "dependent", "p3")
        self.assertIn("loop.module.parked.p5", strict)
        with self.assertRaises(NotReady):
            self.manager.prepare(TaskSpec("loop.module.dependent.p3", strict))

    def test_damaged_actual_shared_producer_cannot_fall_back(self):
        self._success("p2.probes", summary="probe substrate")
        producer = self._success("loop.module.first.p5",
                                 summary="shared target state")
        deps_path = self.ws / "P1" / "modules" / "deps.json"
        deps_path.parent.mkdir(parents=True)
        deps_path.write_text(json.dumps({
            "order": ["first", "second"],
            "edges": {"first": [], "second": []},
        }), encoding="utf-8")
        (producer.directory / "handoff.md").write_text(
            "damaged after publication\n", encoding="utf-8")

        deps = module_dependencies(self.ws, "second", "p3")
        self.assertIn("loop.module.first.p5", deps)
        with self.assertRaises(NotReady):
            self.manager.prepare(TaskSpec("loop.module.second.p3", deps))

    def test_defect_ledger_task_does_not_require_p1_graph(self):
        args = SimpleNamespace(
            phase="p6", defect_add="D-1", defect_close=None,
            defect_park=None, defect_diagnose=None, defect_fix=None,
            draft_l4=False, finalize_l4=False, execute=False, l4=False)
        spec = cli_spec(args, self.ws)
        self.assertEqual("p6.defect.D-1.add", spec.task_id)
        self.assertNotIn("P1/modules/deps.json",
                         {str(item) for item in spec.materials})

    def test_provider_evidence_write_error_fails_active_transport(self):
        with self.manager.start(TaskSpec("evidence-required")):
            with mock.patch.object(
                    HandoffManager, "_record_provider",
                    side_effect=OSError("archive unavailable")):
                with mock.patch.object(agent.subprocess, "run", return_value=mock.Mock(
                        returncode=0, stdout="ok", stderr="")):
                    with self.assertRaisesRegex(OSError, "archive unavailable"):
                        agent.run_agent("work", self.ws,
                                        str(self.ws / "logs" / "agent"))

    def test_structured_blocked_result_creates_failure_handoff(self):
        blocked = '```json\n{"phase":"done","status":"blocked"}\n```\n'
        with self.manager.start(TaskSpec("structured-parent")) as parent:
            with mock.patch.object(agent.subprocess, "run", return_value=mock.Mock(
                    returncode=0, stdout=blocked, stderr="")):
                rc, _out, parsed = agent.run_agent_structured(
                    "work", self.ws, str(self.ws / "structured"),
                    gen_schema={"items": "list"}, max_tries=1,
                    task={"task_id": "structured-child"})
            self.assertEqual(0, rc)
            self.assertEqual("blocked", parsed["status"])
            child = self.manager.inspect("structured-child")[0]
            self.assertEqual("failed", child["status"])
            self.assertIn("provider reported blocked", child["verification"][0])
            parent.fail("blocked child", summary="parent parked")

    def test_terminal_execution_rejects_late_delivery_and_provider_writes(self):
        run = self._success("terminal")
        with self.assertRaises(HandoffError):
            self.manager.record_delivery(run)
        with self.assertRaises(HandoffError):
            run.record_provider(
                invocation_id="late", provider="fake", session_id=None, rc=0,
                log_path=self.ws / "none", prompt_path=self.ws / "none",
                output="late")

    def test_abandoned_attempt_gets_failure_before_fresh_execution(self):
        old = self.manager.prepare(TaskSpec("crashed"))
        record_path = old.directory / "record.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["pid"] = 99999999
        record_path.write_text(json.dumps(record), encoding="utf-8")
        fresh = self.manager.prepare(TaskSpec("crashed"))
        self.assertNotEqual(old.execution_id, fresh.execution_id)
        old_record = json.loads(record_path.read_text(encoding="utf-8"))
        self.assertEqual("failed", old_record["status"])
        self.assertTrue((old.directory / "handoff-fail.md").is_file())
        self.assertIn("host process ended", fresh.input_text)
        fresh.fail("test cleanup", summary="not executed")

    def test_provider_evidence_is_snapshotted_inside_execution(self):
        log = self.ws / "shared.log"
        prompt = self.ws / "shared.prompt.md"
        log.write_text("raw output", encoding="utf-8")
        prompt.write_text("raw prompt", encoding="utf-8")
        with self.manager.start(TaskSpec("snapshot")) as run:
            run.record_provider(invocation_id="inv1", provider="fake",
                                session_id="s1", rc=0, log_path=log,
                                prompt_path=prompt, output="agent summary")
            log.write_text("overwritten", encoding="utf-8")
            run.complete("done", verification=["accepted"])
        saved = run.directory / "provider" / "inv1" / "raw.log"
        self.assertEqual("raw output", saved.read_text(encoding="utf-8"))

    def test_explicit_handoff_summary_precedes_other_json_notes(self):
        output = ('```json\n{"handoff_summary":"semantic work",'
                  '"notes":"incidental note"}\n```')
        self.assertEqual("semantic work", _agent_summary(output))

    def test_context_is_reset_even_when_failure_persistence_raises(self):
        with self.assertRaisesRegex(RuntimeError, "primary"):
            with mock.patch.object(self.manager, "_finish",
                                   side_effect=OSError("disk full")):
                with self.manager.start(TaskSpec("reset-context")):
                    raise RuntimeError("primary")
        self.assertIsNone(current_execution())

    def test_terminal_seq_timeout_retries_in_fresh_session_with_fail_handoff(self):
        def event(session, payload):
            text = "```json\n" + json.dumps(payload) + "\n```"
            return json.dumps({"type": "text", "sessionID": session,
                               "part": {"text": text}}) + "\n"

        first_partial = event("dead-session", {"phase": "run_static"})
        timeout = subprocess.TimeoutExpired(
            cmd=["opencode"], timeout=2, output=first_partial, stderr="")
        with mock.patch.object(agent.subprocess, "run", side_effect=timeout):
            first = run_task(
                self.ws, TaskSpec("seq-recovery"),
                lambda: agent.run_agent_seq(
                    "do work", self.ws, str(self.ws / "seq"),
                    agent_budget_sec=30, gen_schema={"files": "list"}),
                success=lambda value: value["status"] == "done",
                summary=lambda value: f"seq status={value['status']}")
        self.assertEqual("failed", first["status"])
        failure_record = self.manager.inspect("seq-recovery")[0]
        self.assertEqual("failed", failure_record["status"])

        captured = {}

        def fresh_transport(args, **kwargs):
            captured["args"] = args
            captured["input"] = kwargs["input"]
            return mock.Mock(returncode=0,
                             stdout=event("fresh-session", {
                                 "phase": "done", "files": ["x.rs"]}),
                             stderr="")

        with mock.patch.object(agent.subprocess, "run",
                               side_effect=fresh_transport):
            second = run_task(
                self.ws, TaskSpec("seq-recovery"),
                lambda: agent.run_agent_seq(
                    "do work", self.ws, str(self.ws / "seq"),
                    agent_budget_sec=30, gen_schema={"files": "list"}),
                success=lambda value: value["status"] == "done",
                summary=lambda value: f"seq status={value['status']}")
        self.assertEqual("done", second["status"])
        self.assertNotIn("--session", captured["args"])
        self.assertIn("Failed handoff: `seq-recovery`", captured["input"])
        self.assertIn("dead-session", captured["input"])


if __name__ == "__main__":
    unittest.main()
