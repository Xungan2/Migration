"""Functional scope and pruning pipeline regressions, with no model or OS build."""

import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

from porter.common import scope
from porter.divide import fragments, import_p, index, pruning, resolve, run


class FunctionalScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="porter-functional-scope-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.driver = self.root / "driver"
        self.ws = self.root / "ws"
        self.driver.mkdir()
        (self.ws / "P1/reports").mkdir(parents=True)
        (self.ws / "P1/strategy.md").write_text("# m: core\n")
        (self.ws / "project.json").write_text(json.dumps({
            "linux_driver": str(self.driver), "target_os": str(self.root / "os")}))

    def source(self, name, text):
        path = self.driver / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def set_scope(self, files, features=None):
        obj = {"driver_name": "test-driver",
               "modules": [{"name": "m", "files": files}]}
        if features is not None:
            obj["features"] = features
        errors = scope.validate_and_normalize(obj, self.driver, self.ws)
        self.assertEqual(errors, [])
        return obj

    def build_pruning_case(self):
        self.source("core.c", "int start(void)\n{\n    return helper() + outside_init();\n}\n")
        self.source("helper.c", "int helper(void)\n{\n    return 7;\n}\n")
        self.source("outside.c", "int outside_init(void)\n{\n    return 3;\n}\n")
        self.set_scope(["core.c", "helper.c"], {
            "include": ["core startup"], "exclude": ["other plugin"],
            "constraints": ["keep helper result"]})
        (self.ws / "goals.md").write_text("core startup only\n")
        plan, _audit = index.expand(index.build_index(self.driver, {"core.c"}),
                                    {"core.c": {"whole_file": "m"}})
        pruning._write(self.ws / "P1/reports/P1D_plan.json", plan)
        fragments.extract_modules(self.ws, self.driver, plan)
        self.assertEqual(resolve.run_resolve(self.ws, self.driver), 0)
        pruning._write(self.ws / "P1/reports/P1D_inputs.json",
                       {"fingerprint": scope.input_fingerprint(self.ws, self.driver)})
        return plan

    def decision(self, candidate, action):
        definition = candidate["definitions"][0]
        result = {
            "id": candidate["id"], "action": action,
            "reason": "Preserve helper semantics; excluded plugin has no consumer.",
            "evidence": [f"{definition['file']}:{definition['line']}", candidate["uses"][0]],
            "implementation": "Keep helper() returning 7 and remove the outside_init() call.",
            "verification": "start() returns 7 and does not initialize the excluded plugin.",
        }
        if action == "restore":
            result.update(source=definition["file"], module="m")
        return result

    def test_declared_scope_never_falls_back_to_whole_tree(self):
        (self.ws / "goals.md").write_text("only one feature")
        with self.assertRaises(scope.ScopeError):
            scope.load_scope(self.ws)
        path = self.ws / "P1/scope.json"
        for text in ("{bad", '{"modules":[]}', '{"modules":[{"name":"m","files":[]}]}', "[]"):
            path.write_text(text)
            with self.subTest(text=text), self.assertRaises(scope.ScopeError):
                scope.load_scope(self.ws)

    def test_function_metadata_survives_normalization(self):
        self.source("a.c", "int a(void)\n{\n return 0;\n}\n")
        features = {"include": ["read", "read"], "exclude": ["write"],
                    "constraints": ["preserve errors"]}
        self.set_scope(["a.c"], features)
        stored = pruning._read(self.ws / "P1/scope.json")
        self.assertEqual(stored["features"]["include"], ["read"])
        self.assertEqual(stored["features"]["exclude"], ["write"])
        self.assertEqual(scope.load_scope(self.ws), {"a.c"})

    def test_symlink_cannot_escape_driver(self):
        outside = self.root / "outside.c"
        outside.write_text("int outside;\n")
        (self.driver / "escape.c").symlink_to(outside)
        obj = {"driver_name": "test-driver",
               "modules": [{"name": "m", "files": ["escape.c"]}]}
        self.assertTrue(scope.validate_and_normalize(obj, self.driver, self.ws))

    def test_nested_files_keep_distinct_identity(self):
        for folder in ("one", "two"):
            self.source(f"{folder}/part.c", f"int {folder}(void)\n{{\n return 0;\n}}\n")
        files = {"one/part.c", "two/part.c"}
        self.set_scope(sorted(files))
        idx = index.build_index(self.driver, files)
        self.assertEqual(set(idx), files)
        plan, _audit = index.expand(idx, {f: {"whole_file": "m"} for f in files})
        fragments.extract_modules(self.ws, self.driver, plan)
        self.assertTrue((self.ws / "P1/modules/m/one__part.c").is_file())
        self.assertTrue((self.ws / "P1/modules/m/two__part.c").is_file())
        from porter.bootstrap.extract_spine import _orig_driver_defs
        from porter.loop.surface import _orig_defs
        for scanner in (_orig_driver_defs, _orig_defs):
            self.assertEqual(scanner(self.driver, {"two/part.c"}), {"two"})

    def test_initcall_registration_follows_its_function(self):
        for macro in ("late_initcall", "subsys_initcall", "device_initcall_sync",
                      "rootfs_initcall", "console_initcall", "__initcall"):
            self.source("init.c", f"int startup(void)\n{{\n return 0;\n}}\n{macro}(startup);\n")
            idx = index.build_index(self.driver)
            entry = idx["init.c"][-1]
            with self.subTest(macro=macro):
                self.assertEqual((entry.kind, entry.refs), ("reg", ["startup"]))
                plan, _ = index.expand(idx, {"init.c": {"assignments": {"startup": None}}})
                self.assertEqual(plan["modules"], [])
                plan, _ = index.expand(idx, {"init.c": {"assignments": {"startup": "m"}}})
                fragments.extract_modules(self.ws, self.driver, plan)
                self.assertIn(f"{macro}(startup)", (self.ws / "P1/modules/m/init.c").read_text())

    def test_resume_refuses_changed_scope_or_source(self):
        self.source("a.c", "int a(void)\n{\n return 0;\n}\n")
        self.source("b.c", "int b(void)\n{\n return 0;\n}\n")
        self.set_scope(["a.c"])
        with mock.patch.object(run, "_assign_one_file", return_value=({"whole_file": "m"}, {})):
            self.assertEqual(run.run_divide(self.ws, self.driver), 0)
        old = (self.ws / "P1/reports/P1D_plan.json").read_bytes()
        self.set_scope(["b.c"])
        with mock.patch.object(run, "_assign_one_file") as agent_call:
            self.assertEqual(run.run_divide(self.ws, self.driver), 2)
            agent_call.assert_not_called()
        self.assertEqual((self.ws / "P1/reports/P1D_plan.json").read_bytes(), old)
        self.set_scope(["a.c"])
        self.source("a.c", "int a(void)\n{\n return 99;\n}\n")
        self.assertEqual(run.run_divide(self.ws, self.driver), 2)

    def test_resume_rebuilds_partially_missing_modules(self):
        self.source("a.c", "int a(void)\n{\n return 0;\n}\n")
        with mock.patch.object(run, "_assign_one_file", return_value=({"whole_file": "m"}, {})):
            self.assertEqual(run.run_divide(self.ws, self.driver), 0)
        output = self.ws / "P1/modules/m/a.c"
        original = output.read_bytes()
        output.unlink()
        self.assertEqual(run.run_divide(self.ws, self.driver), 0)
        self.assertEqual(output.read_bytes(), original)

    def test_scoped_plan_without_source_fingerprint_requires_revalidation(self):
        self.source("a.c", "int a(void)\n{\n return 0;\n}\n")
        self.set_scope(["a.c"])
        with mock.patch.object(run, "_assign_one_file", return_value=({"whole_file": "m"}, {})):
            self.assertEqual(run.run_divide(self.ws, self.driver), 0)
        (self.ws / "P1/reports/P1D_inputs.json").unlink()
        self.assertEqual(run.run_divide(self.ws, self.driver), 2)

    def test_isolated_module_is_in_migration_order(self):
        self.source("alone.c", "int alone(void)\n{\n return 42;\n}\n")
        plan, _ = index.expand(index.build_index(self.driver), {"alone.c": {"whole_file": "alone"}})
        fragments.extract_modules(self.ws, self.driver, plan)
        pruning._write(self.ws / "P1/reports/P1D_plan.json", plan)
        self.assertEqual(resolve.run_resolve(self.ws, self.driver), 0)
        deps = pruning._read(self.ws / "P1/modules/deps.json")
        self.assertEqual(deps["modules"], ["alone"])
        self.assertEqual(deps["order"], ["alone"])

    def test_import_accepts_an_equivalent_topological_order(self):
        actual = {"modules": ["a", "b"], "edges": {"a": [], "b": []}, "order": ["a", "b"]}
        equivalent = {"modules": [], "edges": {}, "order": ["b", "a"]}
        self.assertEqual(import_p._diff_deps(equivalent, actual), [])
        self.assertTrue(import_p._diff_deps({"order": ["a", "a"]}, actual))

    def test_pruning_finds_excluded_file_dependencies(self):
        self.build_pruning_case()
        found = {c["symbol"]: c for c in pruning.discover(self.ws, self.driver)}
        self.assertEqual(set(found), {"helper", "outside_init"})
        self.assertFalse(found["outside_init"]["definitions"][0]["in_scope"])
        self.assertTrue(found["helper"]["definitions"][0]["in_scope"])
        self.assertEqual(pruning.require_ready(self.ws, self.driver), 2)

    def test_pruning_restores_in_one_call_and_hands_off_rewrites(self):
        self.build_pruning_case()
        calls = []

        def decide(*args, **kwargs):
            self.assertEqual(kwargs["max_tries"], 1)
            candidates = pruning.discover(self.ws, self.driver)
            calls.append({c["symbol"] for c in candidates})
            rows = [self.decision(c, "restore" if c["symbol"] == "helper" else "remove_path")
                    for c in candidates]
            return 0, "", {"phase": "done", "decisions": rows}

        with mock.patch.object(pruning.agent, "run_agent_structured", side_effect=decide):
            self.assertEqual(pruning.run_pruning(self.ws, self.driver), 0)
        self.assertEqual(calls, [{"helper", "outside_init"}])
        self.assertIn("return 7", (self.ws / "P1/modules/m/helper.c").read_text())
        self.assertEqual(pruning.require_ready(self.ws, self.driver), 0)
        context = pruning.module_context(self.ws, "m")
        self.assertIn("outside_init", context)
        self.assertIn("verification", context)
        report = pruning._read(self.ws / pruning.REPORT)
        self.assertEqual(report["mode"], "single_pass")
        self.assertTrue(all(not e.startswith("P1/modules/")
                            for d in report["proposal"] for e in d["evidence"]))
        with mock.patch.object(pruning.agent, "run_agent_structured") as again:
            self.assertEqual(pruning.run_pruning(self.ws, self.driver), 0)
            again.assert_not_called()
        with (self.ws / "P1/modules/m/core.c").open("a") as f:
            f.write("/* changed */\n")
        self.assertEqual(pruning.require_ready(self.ws, self.driver), 2)

    def build_pruning_chain(self):
        self.build_pruning_case()
        self.source("helper.c", "int helper(void)\n{\n return tail();\n}\n"
                    "int tail(void)\n{\n return leaf();\n}\n"
                    "int leaf(void)\n{\n return outside_init();\n}\n")
        pruning._write(self.ws / "P1/reports/P1D_inputs.json",
                       {"fingerprint": scope.input_fingerprint(self.ws, self.driver)})

    def test_single_proposal_restores_transitive_dependencies(self):
        self.build_pruning_chain()

        def decide(*args, **kwargs):
            context = pruning._read(self.ws / pruning.CONTEXT)
            nodes = context["dependencies"]
            self.assertIn("tail", nodes["helper"]["references"])
            self.assertIn("leaf", nodes["tail"]["references"])
            self.assertIn("outside_init", nodes["leaf"]["references"])
            self.assertEqual(nodes["outside_init"]["restore_spans"], [])
            rows = [self.decision(c, "restore" if c["symbol"] == "helper" else "remove_path")
                    for c in context["candidates"]]
            for symbol in ("tail", "leaf"):
                rows.append({"id": f"m:{symbol}", "action": "restore", "source": "helper.c",
                             "module": "m", "reason": "Required by restored helper chain",
                             "evidence": [f"helper.c:{nodes[symbol]['definitions'][0]['line']}"],
                             "implementation": "Restore the complete definition",
                             "verification": "start preserves the helper chain result"})
            return 0, "", {"decisions": rows}

        with (mock.patch.object(pruning.agent, "run_agent_structured", side_effect=decide) as call,
              mock.patch.object(resolve, "run_resolve") as resolve_agent):
            self.assertEqual(pruning.run_pruning(self.ws, self.driver), 0)
            call.assert_called_once()
            resolve_agent.assert_not_called()
        content = (self.ws / "P1/modules/m/helper.c").read_text()
        for symbol in ("helper", "tail", "leaf"):
            self.assertIn(f"int {symbol}(void)", content)
        self.assertEqual(pruning.require_ready(self.ws, self.driver), 0)
        self.assertIn("outside_init", pruning.module_context(self.ws, "m"))

    def test_incomplete_restore_chain_stops_without_retry_or_publishing(self):
        self.build_pruning_chain()
        before = {p.relative_to(self.ws).as_posix(): p.read_bytes()
                  for p in (self.ws / "P1/modules").rglob("*") if p.is_file()}
        plan = (self.ws / "P1/reports/P1D_plan.json").read_bytes()
        rows = [self.decision(c, "restore" if c["symbol"] == "helper" else "remove_path")
                for c in pruning.discover(self.ws, self.driver)]
        with (mock.patch.object(pruning.agent, "run_agent_structured",
                                return_value=(0, "", {"decisions": rows})) as call,
              mock.patch.object(resolve, "run_resolve") as resolve_agent):
            self.assertEqual(pruning.run_pruning(self.ws, self.driver), 1)
            call.assert_called_once()
            resolve_agent.assert_not_called()
        report = pruning._read(self.ws / pruning.REPORT)
        self.assertEqual(report["status"], "blocked")
        self.assertIn("m:tail", " ".join(report["errors"]))
        self.assertEqual(report["proposal"][0]["action"], "restore")
        self.assertEqual((self.ws / "P1/reports/P1D_plan.json").read_bytes(), plan)
        after = {p.relative_to(self.ws).as_posix(): p.read_bytes()
                 for p in (self.ws / "P1/modules").rglob("*") if p.is_file()}
        self.assertEqual(after, before)
        self.assertEqual(pruning.require_ready(self.ws, self.driver), 2)

    def test_restoration_cycle_stops_without_a_resolve_agent(self):
        self.source("a.c", "int a_start(void)\n{\n return helper();\n}\n")
        self.source("b.c", "int b_start(void)\n{\n return a_start();\n}\n")
        self.source("helper.c", "int helper(void)\n{\n return b_start();\n}\n")
        self.set_scope(["a.c", "b.c", "helper.c"])
        plan, _ = index.expand(index.build_index(self.driver, {"a.c", "b.c"}),
                               {f + ".c": {"whole_file": f} for f in ("a", "b")})
        pruning._write(self.ws / "P1/reports/P1D_plan.json", plan)
        fragments.extract_modules(self.ws, self.driver, plan)
        self.assertEqual(resolve.run_resolve(self.ws, self.driver), 0)
        pruning._write(self.ws / "P1/reports/P1D_inputs.json",
                       {"fingerprint": scope.input_fingerprint(self.ws, self.driver)})
        before = (self.ws / "P1/modules/deps.json").read_bytes()
        rows = [dict(self.decision(c, "restore"), module="a")
                for c in pruning.discover(self.ws, self.driver)]
        with (mock.patch.object(pruning.agent, "run_agent_structured",
                                return_value=(0, "", {"decisions": rows})) as call,
              mock.patch.object(resolve, "run_resolve") as resolve_agent):
            self.assertEqual(pruning.run_pruning(self.ws, self.driver), 1)
            call.assert_called_once()
            resolve_agent.assert_not_called()
        report = pruning._read(self.ws / pruning.REPORT)
        self.assertIn("依赖环", " ".join(report["errors"]))
        self.assertEqual((self.ws / "P1/modules/deps.json").read_bytes(), before)
        self.assertFalse((self.ws / "P1/modules/a/helper.c").exists())

    def test_shared_cut_definition_is_restored_once_for_multiple_consumers(self):
        for name in ("a", "b"):
            self.source(name + ".c", f"int {name}_start(void)\n{{\n return helper();\n}}\n")
        self.source("helper.c", "int helper(void)\n{\n return 7;\n}\n")
        self.set_scope(["a.c", "b.c", "helper.c"])
        plan, _ = index.expand(index.build_index(self.driver, {"a.c", "b.c"}),
                               {f + ".c": {"whole_file": f} for f in ("a", "b")})
        pruning._write(self.ws / "P1/reports/P1D_plan.json", plan)
        fragments.extract_modules(self.ws, self.driver, plan)
        self.assertEqual(resolve.run_resolve(self.ws, self.driver), 0)
        pruning._write(self.ws / "P1/reports/P1D_inputs.json",
                       {"fingerprint": scope.input_fingerprint(self.ws, self.driver)})
        candidates = pruning.discover(self.ws, self.driver)
        rows = [dict(self.decision(c, "restore"), module=c["module"]) for c in candidates]
        with mock.patch.object(pruning.agent, "run_agent_structured",
                               return_value=(0, "", {"decisions": rows})):
            self.assertEqual(pruning.run_pruning(self.ws, self.driver), 0)
        helpers = list((self.ws / "P1/modules").glob("*/helper.c"))
        self.assertEqual(len(helpers), 1)
        deps = pruning._read(self.ws / "P1/modules/deps.json")
        self.assertEqual(deps["edges"]["b"], ["a"])
        self.assertEqual(pruning.require_ready(self.ws, self.driver), 0)

    def test_shared_restore_preserves_other_consumers_path_removal(self):
        for name in ("a", "b"):
            self.source(name + ".c", f"int {name}_start(void)\n{{\n return helper();\n}}\n")
        self.source("helper.c", "int helper(void)\n{\n return 7;\n}\n")
        self.set_scope(["a.c", "b.c", "helper.c"])
        plan, _ = index.expand(index.build_index(self.driver, {"a.c", "b.c"}),
                               {f + ".c": {"whole_file": f} for f in ("a", "b")})
        pruning._write(self.ws / "P1/reports/P1D_plan.json", plan)
        fragments.extract_modules(self.ws, self.driver, plan)
        self.assertEqual(resolve.run_resolve(self.ws, self.driver), 0)
        pruning._write(self.ws / "P1/reports/P1D_inputs.json",
                       {"fingerprint": scope.input_fingerprint(self.ws, self.driver)})
        rows = [dict(self.decision(c, "restore" if c["module"] == "a" else "remove_path"),
                     module="a") for c in pruning.discover(self.ws, self.driver)]
        with mock.patch.object(pruning.agent, "run_agent_structured",
                               return_value=(0, "", {"decisions": rows})) as call:
            self.assertEqual(pruning.run_pruning(self.ws, self.driver), 0)
            call.assert_called_once()
        self.assertEqual(pruning.discover(self.ws, self.driver), [])
        self.assertEqual(pruning.require_ready(self.ws, self.driver), 0)
        self.assertIn("b:helper", pruning.module_context(self.ws, "b"))
        self.assertTrue(pruning.criteria_errors(self.ws, "b", []))

    def test_single_proposal_hands_off_new_dependency_in_restoration_owner(self):
        self.source("a.c", "int a_start(void)\n{\n return helper();\n}\n")
        self.source("b.c", "int b_start(void)\n{\n return 1;\n}\n")
        self.source("helper.c", "int helper(void)\n{\n return excluded_init();\n}\n")
        self.source("outside.c", "int excluded_init(void)\n{\n return 3;\n}\n")
        self.set_scope(["a.c", "b.c", "helper.c"])
        plan, _ = index.expand(index.build_index(self.driver, {"a.c", "b.c"}),
                               {f + ".c": {"whole_file": f} for f in ("a", "b")})
        pruning._write(self.ws / "P1/reports/P1D_plan.json", plan)
        fragments.extract_modules(self.ws, self.driver, plan)
        self.assertEqual(resolve.run_resolve(self.ws, self.driver), 0)
        pruning._write(self.ws / "P1/reports/P1D_inputs.json",
                       {"fingerprint": scope.input_fingerprint(self.ws, self.driver)})
        rows = [dict(self.decision(c, "restore"), module="b")
                for c in pruning.discover(self.ws, self.driver)]
        rows.append({"id": "b:excluded_init", "action": "remove_path",
                     "reason": "The restored helper contains an excluded initialization path",
                     "evidence": ["helper.c:3", "outside.c:1"],
                     "implementation": "Remove the excluded init from the target helper",
                     "verification": "a_start does not initialize the excluded feature"})
        with mock.patch.object(pruning.agent, "run_agent_structured",
                               return_value=(0, "", {"decisions": rows})) as call:
            self.assertEqual(pruning.run_pruning(self.ws, self.driver), 0)
            call.assert_called_once()
        self.assertEqual(pruning.require_ready(self.ws, self.driver), 0)
        self.assertIn("b:excluded_init", pruning.module_context(self.ws, "b"))
        self.assertTrue(pruning.criteria_errors(self.ws, "b", []))

    def test_pruning_detects_a_cut_type_used_only_by_a_struct_field(self):
        self.source("core.c", "int start(void)\n{\n return 0;\n}\n")
        self.source("core.h", "struct core {\n struct optional *state;\n};\n")
        self.source("optional.h", "struct optional {\n int state;\n};\n")
        self.set_scope(["core.c", "core.h"])
        plan, _ = index.expand(index.build_index(self.driver, {"core.c", "core.h"}),
                               {f: {"whole_file": "m"} for f in ("core.c", "core.h")})
        fragments.extract_modules(self.ws, self.driver, plan)
        found = pruning.discover(self.ws, self.driver)
        self.assertEqual([c["symbol"] for c in found], ["optional"])
        self.assertFalse(found[0]["definitions"][0]["in_scope"])

    def test_incomplete_agent_decisions_cannot_publish_ready(self):
        self.build_pruning_case()
        plan = (self.ws / "P1/reports/P1D_plan.json").read_bytes()
        with mock.patch.object(pruning.agent, "run_agent_structured",
                               return_value=(0, "", {"decisions": []})) as call:
            self.assertEqual(pruning.run_pruning(self.ws, self.driver), 1)
            call.assert_called_once()
        self.assertEqual(pruning._read(self.ws / pruning.REPORT)["status"], "blocked")
        self.assertEqual((self.ws / "P1/reports/P1D_plan.json").read_bytes(), plan)
        self.assertEqual(pruning.require_ready(self.ws, self.driver), 2)

    def test_blocked_agent_cannot_publish_decisions_as_ready(self):
        self.build_pruning_case()
        rows = [self.decision(c, "rewrite")
                for c in pruning.discover(self.ws, self.driver)]
        with mock.patch.object(pruning.agent, "run_agent_structured",
                               return_value=(0, "", {"phase": "done", "status": "blocked",
                                                    "reason": "missing evidence",
                                                    "decisions": rows})):
            self.assertEqual(pruning.run_pruning(self.ws, self.driver), 1)
        self.assertEqual(pruning._read(self.ws / pruning.REPORT)["status"], "blocked")
        self.assertEqual(pruning.require_ready(self.ws, self.driver), 2)

    def test_pruning_cannot_recertify_old_fragments_against_changed_source(self):
        self.build_pruning_case()
        self.source("helper.c", "int helper(void)\n{\n return 99;\n}\n")
        with mock.patch.object(pruning.agent, "run_agent_structured") as call:
            self.assertEqual(pruning.run_pruning(self.ws, self.driver), 2)
            call.assert_not_called()
        self.assertEqual(pruning.require_ready(self.ws, self.driver), 2)

    def test_ready_report_cannot_omit_an_existing_candidate(self):
        self.build_pruning_case()
        rows = [self.decision(c, "rewrite")
                for c in pruning.discover(self.ws, self.driver)]
        with mock.patch.object(pruning.agent, "run_agent_structured",
                               return_value=(0, "", {"decisions": rows})):
            self.assertEqual(pruning.run_pruning(self.ws, self.driver), 0)
        path = self.ws / pruning.REPORT
        report = pruning._read(path)
        report["candidates"] = []
        report["decisions"] = []
        pruning._write(path, report)
        self.assertEqual(pruning.require_ready(self.ws, self.driver), 2)

    def test_corrupt_single_pass_proposal_is_not_reusable(self):
        self.build_pruning_case()
        rows = [self.decision(c, "rewrite")
                for c in pruning.discover(self.ws, self.driver)]
        with mock.patch.object(pruning.agent, "run_agent_structured",
                               return_value=(0, "", {"decisions": rows})):
            self.assertEqual(pruning.run_pruning(self.ws, self.driver), 0)
        path = self.ws / pruning.REPORT
        report = pruning._read(path)
        for malformed in (None, {}, [None], [{"id": "m:helper"}]):
            with self.subTest(proposal=malformed):
                report["proposal"] = malformed
                pruning._write(path, report)
                self.assertEqual(pruning.require_ready(self.ws, self.driver), 2)

    def test_file_assignments_resume_without_repeating_successful_calls(self):
        self.source("a.c", "int a(void)\n{\n return 0;\n}\n")
        self.source("b.c", "int b(void)\n{\n return 0;\n}\n")
        calls = []

        def assign(_skill, _strategy, fname, _entries, _p1):
            calls.append(fname)
            if len(calls) == 2:
                return None, {}
            return {"whole_file": "m"}, {}

        with mock.patch.object(run, "_assign_one_file", side_effect=assign):
            self.assertEqual(run.run_divide(self.ws, self.driver), 1)
            self.assertEqual(run.run_divide(self.ws, self.driver), 0)
        self.assertEqual(calls, ["a.c", "b.c", "b.c"])

    def test_agent_json_strings_can_contain_markdown_fences_and_braces(self):
        fence = chr(96) * 3
        obj = {"whole_file": "m", "phase": "done",
               "handoff_summary": "Use " + fence + 'json and literal } { with "quotes".'}
        encoded = json.dumps(obj)
        for output in (encoded + "\n> build · model\n→ Read core.c",
                       fence + "json\n" + encoded + "\n" + fence):
            with self.subTest(output=output):
                self.assertEqual(run.agent.extract_json(output), obj)
                self.assertEqual(run.agent._parse_phase(output), obj)
        truncated = fence + 'json\n{"whole_file":"m","handoff_summary":"unfinished'
        self.assertIsNone(run.agent.extract_json(truncated))
        self.assertIsNone(run.agent._parse_phase(truncated))

    def test_file_assignment_receives_current_functional_scope(self):
        self.source("a.c", "int a(void)\n{\n return 0;\n}\n")
        self.set_scope(["a.c"], {"include": ["read data"],
                               "exclude": ["wake on LAN"],
                               "constraints": ["release on failure"]})
        fence = chr(96) * 3
        response = fence + 'json\n{"whole_file":"m"}\n' + fence
        with mock.patch.object(run.agent, "run_agent", return_value=(0, response)) as call:
            self.assertEqual(run.run_divide(self.ws, self.driver), 0)
        prompt = call.call_args.args[0]
        self.assertIn("read data", prompt)
        self.assertIn("wake on LAN", prompt)
        self.assertIn("release on failure", prompt)
        context = pruning.module_context(self.ws, "m")
        self.assertIn("read data", context)
        self.assertIn("wake on LAN", context)
        self.assertIn("release on failure", context)

    def test_p3_requires_behavioural_coverage_of_pruning_decisions(self):
        from porter.loop import p3
        self.build_pruning_case()
        candidates = pruning.discover(self.ws, self.driver)
        decisions = [self.decision(c, "rewrite") for c in candidates]
        with mock.patch.object(pruning.agent, "run_agent_structured",
                               return_value=(0, "", {"decisions": decisions})):
            self.assertEqual(pruning.run_pruning(self.ws, self.driver), 0)
        p3m = self.ws / "P3/m"
        (p3m / "reports").mkdir(parents=True)
        surface = {"files": ["core.c"], "stats": {"os_api": 0, "mapped": 0, "missing": 0}}
        criterion = {"id": "m.semantic", "kind": "unit_test", "layer": "L0",
                     "expr": "check_start_result", "deferred_by": None}
        fence = chr(96) * 3
        response = fence + "json\n" + json.dumps({"criteria": [criterion]}) + "\n" + fence
        with mock.patch.object(p3.agent, "run_agent", return_value=(0, response)):
            self.assertEqual(p3._step_criteria(self.ws, "m", p3m, surface), 1)
        self.assertFalse((p3m / "reports/criteria.json").exists())
        criterion["pruning_ids"] = [d["id"] for d in decisions]
        response = fence + "json\n" + json.dumps({"criteria": [criterion]}) + "\n" + fence
        with mock.patch.object(p3.agent, "run_agent", return_value=(0, response)):
            self.assertEqual(p3._step_criteria(self.ws, "m", p3m, surface), 0)
        stored = pruning._read(p3m / "reports/criteria.json")["criteria"]
        self.assertEqual(pruning.criteria_errors(self.ws, "m", stored), [])
        self.assertEqual(stored[-1]["pruning_ids"], criterion["pruning_ids"])

    def test_p1_composite_runs_pruning_after_resolve(self):
        from porter import main
        from porter.loop import gates
        stages = []
        def record(name):
            def call(*_args, **_kwargs):
                stages.append(name)
                return 0
            return call
        with (mock.patch.object(main.p1s, "run_strategy", side_effect=record("strategy")),
              mock.patch.object(main.p1a, "run_divide", side_effect=record("divide")),
              mock.patch.object(main.p1r, "run_resolve", side_effect=record("resolve")),
              mock.patch.object(pruning, "run_pruning", side_effect=record("prune")),
              mock.patch.object(gates, "strategy_checkpoint", return_value=0)):
            self.assertEqual(main.cmd_p1(Namespace(output_dir=str(self.ws))), 0)
        self.assertEqual(stages, ["strategy", "divide", "resolve", "prune"])

    def test_invalid_evidence_and_outside_restoration_are_rejected(self):
        self.build_pruning_case()
        candidates = pruning.discover(self.ws, self.driver)
        rows = [self.decision(c, "rewrite") for c in candidates]
        rows[0]["evidence"] = ["core.c:999"]
        self.assertTrue(pruning._validate(rows, candidates, self.ws, self.driver))
        rows = [self.decision(c, "restore") for c in candidates]
        self.assertTrue(pruning._validate(rows, candidates, self.ws, self.driver))


if __name__ == "__main__":
    unittest.main()
