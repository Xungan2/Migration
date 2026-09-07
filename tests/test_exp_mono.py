"""test_exp_mono.py — exp-mono 子命令的功能性测试（全 mock，零真实 agent）。

覆盖：
  1. 拓扑推进：全 loop 两模块按 order 依次迁移 → 全 pass + 目标树按
     模块 commit + 终局（全量单测 + 启动冒烟留档）+ report 就位
  2. 幂等跳过：ledger 已 pass 的模块不再调 agent
  3. gate ① 失败短路：产物守卫失败时 build/ut 均不执行
  4. gate ③ 测试标记检测：代码/登记/build/ut 全过但标记零增量 → 判败
  5. 缺键降级：无 driver_scope_cmd → 全量 cmd；无 integration → 跳登记
  6. blocked 停车与重入：agent 报 blocked → rc 1 不 commit；修复后重跑
     从断点续（前一失败模块重跑成功）
  7. 前置守卫：缺 deps.json / 模块不在 order / 依赖未 pass → rc 2
  8. 改动范围守卫：越出白名单的改动 → gate ① 判败
  9. 小件：预算 clamp / 非注释行计数 / 占位符替换

fixture 全虚构：driver-x / fake-tree / 模块 fx-a→fx-b / 扩展名 .cx /
测试标记 @MARK / 登记模板 `decl {stem};`——不承载任何真实平台事实。
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.exp import mono as mono_mod


def _mk_fixture(tmp: Path) -> dict:
    """合成工作区 + 目标树 + 参考树。返回路径字典。"""
    linux = tmp / "linux-x"
    linux.mkdir()
    (linux / "drv.c").write_text("int fictitious(void)\n{\n\treturn 0;\n}\n",
                                 encoding="utf-8")
    tree = tmp / "fake-tree"
    home = tree / "home" / "drv-x"
    home.mkdir(parents=True)
    (home / "reg.txt").write_text("decl scaffold;\n", encoding="utf-8")
    (home / "scaffold.cx").write_text("unit scaffold;\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tree, check=True,
                   capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=tree, check=True,
                   capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "init"], cwd=tree, check=True,
                   capture_output=True)
    ws = tmp / "ws"
    for sub in ("P1/modules/fx-a", "P1/modules/fx-b", "P2/reports"):
        (ws / sub).mkdir(parents=True)
    (ws / "project.json").write_text(json.dumps({
        "name": "ws-fx", "linux_driver": str(linux),
        "target_os": str(tree)}), encoding="utf-8")
    (ws / "runner.json").write_text(json.dumps({
        "build": {"cmd": "echo BUILD-DONE", "timeout_full_sec": 60,
                  "success_pattern": "BUILD-DONE"},
        "unit_test": {"cmd": 'echo "UT-DONE full"',
                      "driver_scope_cmd":
                          'echo "UT-DONE scope {PORTER_DRIVER_HOME}"',
                      "timeout_sec": 60,
                      "success_pattern": "UT-DONE",
                      "fail_pattern": "UT-BAD"}}), encoding="utf-8")
    (ws / "P1" / "modules" / "deps.json").write_text(json.dumps({
        "modules": ["fx-a", "fx-b"],
        "edges": {"fx-a": [], "fx-b": ["fx-a"]},
        "order": ["fx-a", "fx-b"]}), encoding="utf-8")
    (ws / "P1" / "modules" / "fx-a" / "module.json").write_text(json.dumps({
        "name": "fx-a", "function": "虚构核心词汇"}), encoding="utf-8")
    (ws / "P1" / "modules" / "fx-a" / "spec_a.c").write_text(
        "\n".join(f"int fa_{i}(void) {{ return {i}; }}" for i in range(40))
        + "\n", encoding="utf-8")
    (ws / "P1" / "modules" / "fx-b" / "module.json").write_text(json.dumps({
        "name": "fx-b", "function": "虚构叶逻辑"}), encoding="utf-8")
    (ws / "P1" / "modules" / "fx-b" / "spec_b.c").write_text(
        "\n".join(f"int fb_{i}(void) {{ return {i}; }}" for i in range(40))
        + "\n", encoding="utf-8")
    (ws / "P2" / "reports" / "scaffold_manifest.json").write_text(
        json.dumps({
            "driver": "driver-x", "driver_home": "home/drv-x",
            "source_ext": ".cx",
            "integration": {"list_file": "reg.txt",
                            "entry_template": "decl {stem};"},
            "test_substrate": {"marker": "@MARK",
                               "how": "虚构基质：标记置于测试单元之首"},
            "commit_paths": ["top.txt"]}), encoding="utf-8")
    return {"tmp": tmp, "ws": ws, "tree": tree, "home": home,
            "linux": linux}


def _write_module_product(home: Path, stem: str, *, marker=True,
                          register=True, lines=12, stray=None):
    """模拟 agent 交付：driver_home 内新文件 + 登记 + 测试标记。"""
    body = [f"unit {stem};"]
    body += [f"line_{stem}_{i} = {i};" for i in range(lines)]
    if marker:
        body += ["probe_unit_a() {", "\t@MARK check truth table;",
                 "\tassert(1 + 1 == 2);", "}"]
    (home / f"{stem}.cx").write_text("\n".join(body) + "\n",
                                     encoding="utf-8")
    if register:
        reg = home / "reg.txt"
        reg.write_text(reg.read_text(encoding="utf-8")
                       + f"decl {stem};\n", encoding="utf-8")
    if stray:
        stray.write_text("stray\n", encoding="utf-8")


class _FakeSeq:
    """run_agent_seq 的 mock：做点活 → 跑一次静态段 → done。

    decl = callable(module) -> 记账声明（默认与 _write_module_product
    的默认产物一致：1 个已测单元 + 1 个带标记测试）。
    """

    def __init__(self, work, decl=None, static_fail_status="stalled"):
        self.work = work                 # callable(module) -> None
        self._decl = decl
        self.static_fail_status = static_fail_status
        self.calls = []                  # (module, prompt, budget)
        self.static_results = []

    def __call__(self, prompt, workdir, log_stem, static=None,
                 gen_schema=None, final_static=False, agent_budget_sec=0,
                 task=None, model=None, resume_session=None):
        module = (task or {})["module"]
        self.calls.append({"module": module, "prompt": prompt,
                           "budget": agent_budget_sec,
                           "gen_schema": gen_schema,
                           "static": static,
                           "resume_session": resume_session})
        self.work(module)
        if static is not None:
            ok, out = static["fn"]()
            self.static_results.append({"module": module, "ok": ok,
                                        "out": out})
            if not ok:
                return {"status": self.static_fail_status,
                        "session_id": None, "fallback": False,
                        "rounds": [{"seg": 1}], "parsed": None,
                        "total_agent_sec": 0.1}
        stem = module.replace("-", "_")
        decl = (self._decl or (lambda m: {}))(module)
        parsed = {"status": "done",
                  "files": [f"home/drv-x/{stem}.cx"],
                  "notes": "虚构完成",
                  "migrated_functions": decl.get(
                      "migrated_functions", [f"{stem}_logic"]),
                  "tests": decl.get(
                      "tests", [{"fn": f"{stem}_logic",
                                 "aspect": "truth table",
                                 "name": f"probe_{stem}_a"}]),
                  "untested": decl.get("untested", [])}
        return {"status": "done", "session_id": "ses_fx",
                "fallback": False, "rounds": [{"seg": 1}, {"seg": 2}],
                "parsed": parsed,
                "total_agent_sec": 1.5}


def _run_with_fakes(test, fx, work, *, build_ok=True, boot_ok=True,
                    decl=None, session=None):
    """通用环境：patch seq/build/ut 执行器/commit/boot 后跑全 loop。"""
    seq = _FakeSeq(work, decl=decl)
    ut_calls = []

    def _shell_ut(cmd, cwd, env, timeout_sec, log_path):
        ut_calls.append(cmd)
        out = "UT-DONE mocked\n"
        (Path(log_path)).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(out, encoding="utf-8")
        return 0, out

    commits = []

    def _commit(ws, msg, paths=None, phase=None):
        commits.append(msg)
        return ["deadbeef0"]

    with mock.patch.object(mono_mod.agent, "run_agent_seq", seq), \
            mock.patch.object(mono_mod.probe_mod, "probe_build",
                              return_value={"item": "build",
                                            "ok": build_ok,
                                            "detail": "rc=0"}), \
            mock.patch.object(mono_mod, "_shell_ut", _shell_ut), \
            mock.patch("porter.common.vcs.commit_target", _commit), \
            mock.patch.object(
                mono_mod.probe_mod, "probe_boot",
                return_value={"item": "final_boot", "ok": boot_ok,
                              "detail": "rc=0"}):
        rc = mono_mod.run_exp_mono(fx["ws"], session=session)
    return {"rc": rc, "seq": seq, "commits": commits, "ut_calls": ut_calls}


class TestExpMonoLoop(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="exp_mono_"))
        self.fx = _mk_fixture(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _ledger(self):
        return json.loads((self.fx["ws"] / "exp-mono" / "ledger.json")
                          .read_text(encoding="utf-8"))

    def test_full_loop_topo_commits_terminal(self):
        def work(module):
            stem = module.replace("-", "_")
            _write_module_product(self.fx["home"], stem)

        r = _run_with_fakes(self, self.fx, work)
        self.assertEqual(r["rc"], 0)
        self.assertEqual([c["module"] for c in r["seq"].calls],
                         ["fx-a", "fx-b"])
        led = self._ledger()
        for m in ("fx-a", "fx-b"):
            self.assertEqual(led["modules"][m]["status"], "pass")
        self.assertEqual(len(r["commits"]), 2)
        self.assertIn("fx-a", r["commits"][0])
        self.assertIn("fx-b", r["commits"][1])
        self.assertIn("build+ut green", r["commits"][0])
        # 终局：最后一次 ut 是全量 cmd（无占位符、非 scope 形态）
        self.assertNotIn("{PORTER", r["ut_calls"][-1])
        self.assertIn("full", r["ut_calls"][-1])
        # scope 形态在模块轮中出现过且占位符已替换
        scope_calls = [c for c in r["ut_calls"] if "scope" in c]
        self.assertEqual(len(scope_calls), 2)
        self.assertIn("home/drv-x", scope_calls[0])
        report = (self.fx["ws"] / "exp-mono" / "report.md").read_text(
            encoding="utf-8")
        self.assertIn("fx-a", report)
        self.assertIn("全量单测：PASS", report)
        self.assertIn("函数覆盖台账", report)
        self.assertIn("truth table", report)
        # gen_schema 含记账三字段（声明面由编排器核对）
        self.assertEqual(
            r["seq"].calls[0]["gen_schema"],
            {"status": "str", "files": "list", "notes": "str",
             "migrated_functions": "list", "tests": "list",
             "untested": "list"})
        # 词典/泊车文件已被初始化
        self.assertTrue((self.fx["ws"] / "exp-mono" / "mapping-notes.md")
                        .exists())
        self.assertTrue((self.fx["ws"] / "exp-mono" / "parking.md").exists())
        # prompt 注入了模块上下文与词典路径
        prompt = r["seq"].calls[0]["prompt"]
        self.assertIn("fx-a", prompt)
        self.assertIn("mapping-notes.md", prompt)
        self.assertIn("decl {stem};", prompt)
        self.assertIn("@MARK", prompt)
        self.assertIn("虚构核心词汇", prompt)

    def test_idempotent_skip(self):
        def work(module):
            _write_module_product(self.fx["home"], module.replace("-", "_"))

        r1 = _run_with_fakes(self, self.fx, work)
        self.assertEqual(r1["rc"], 0)
        r2 = _run_with_fakes(self, self.fx, work)
        self.assertEqual(r2["rc"], 0)
        self.assertEqual([c["module"] for c in r2["seq"].calls], [])

    def test_gate_short_circuit(self):
        def work(module):        # 什么都不写 → 产物守卫必败
            pass

        r = _run_with_fakes(self, self.fx, work)
        self.assertEqual(r["rc"], 1)
        self.assertEqual(r["ut_calls"], [])
        self.assertEqual(r["commits"], [])
        self.assertNotEqual(self._ledger()["modules"]["fx-a"]["status"],
                            "pass")
        self.assertFalse(r["seq"].static_results[0]["ok"])
        self.assertIn("产物守卫", r["seq"].static_results[0]["out"])

    def test_decl_marker_consistency(self):
        # 声明了 1 个测试但产物零标记（三段 gate 全过）→ 声明面判败
        def work(module):
            _write_module_product(self.fx["home"], module.replace("-", "_"),
                                  marker=False)

        r = _run_with_fakes(self, self.fx, work)
        self.assertEqual(r["rc"], 1)
        # 三段 gate 在 session 内全绿（build/ut 都跑了），败在记账核对
        self.assertEqual(len(r["ut_calls"]), 1)
        self.assertTrue(r["seq"].static_results[0]["ok"])
        self.assertEqual(r["commits"], [])
        led = self._ledger()["modules"]["fx-a"]
        self.assertEqual(led["status"], "decl-mismatch")
        self.assertEqual(led["marker_delta"], 0)
        self.assertEqual(len(led["tests"]), 1)

    def test_decl_accounting_incomplete(self):
        # migrated 里 fa_math 未出现在 tests ∪ untested → 记账不完备
        def work(module):
            stem = module.replace("-", "_")
            _write_module_product(self.fx["home"], stem)

        def decl(module):
            stem = module.replace("-", "_")
            return {"migrated_functions": [f"{stem}_logic", f"{stem}_math"],
                    "tests": [{"fn": f"{stem}_logic",
                               "aspect": "truth table",
                               "name": f"probe_{stem}_a"}],
                    "untested": []}

        r = _run_with_fakes(self, self.fx, work, decl=decl)
        self.assertEqual(r["rc"], 1)
        led = self._ledger()["modules"]["fx-a"]
        self.assertEqual(led["status"], "decl-mismatch")
        self.assertEqual(r["commits"], [])

    def test_decl_overlap_rejected(self):
        # 同一单元同时挂 tests 与 untested → 判败
        def work(module):
            stem = module.replace("-", "_")
            _write_module_product(self.fx["home"], stem)

        def decl(module):
            stem = module.replace("-", "_")
            return {"migrated_functions": [f"{stem}_logic"],
                    "tests": [{"fn": f"{stem}_logic",
                               "aspect": "truth table",
                               "name": f"probe_{stem}_a"}],
                    "untested": [{"fn": f"{stem}_logic",
                                  "reason": "平台绑定"}]}

        r = _run_with_fakes(self, self.fx, work, decl=decl)
        self.assertEqual(r["rc"], 1)
        self.assertEqual(self._ledger()["modules"]["fx-a"]["status"],
                         "decl-mismatch")

    def test_decl_all_exempted_is_legal(self):
        # 零测试 + 全部豁免带理由 = 合法（个数不是指标），report 留档
        def work(module):
            stem = module.replace("-", "_")
            _write_module_product(self.fx["home"], stem, marker=False)

        def decl(module):
            stem = module.replace("-", "_")
            return {"migrated_functions": [f"{stem}_logic"],
                    "tests": [],
                    "untested": [{"fn": f"{stem}_logic",
                                  "reason": "纯声明单元"}]}

        r = _run_with_fakes(self, self.fx, work, decl=decl)
        self.assertEqual(r["rc"], 0)
        led = self._ledger()["modules"]["fx-a"]
        self.assertEqual(led["status"], "pass")
        self.assertEqual(led["marker_delta"], 0)
        report = (self.fx["ws"] / "exp-mono" / "report.md").read_text(
            encoding="utf-8")
        self.assertIn("纯声明单元", report)

    def test_change_scope_guard(self):
        def work(module):
            _write_module_product(self.fx["home"], module.replace("-", "_"),
                                  stray=self.fx["tree"] / "stray.txt")

        r = _run_with_fakes(self, self.fx, work)
        self.assertEqual(r["rc"], 1)
        self.assertFalse(r["seq"].static_results[0]["ok"])
        self.assertIn("越出白名单", r["seq"].static_results[0]["out"])

    def test_missing_keys_degrade(self):
        runner_p = self.fx["ws"] / "runner.json"
        runner = json.loads(runner_p.read_text(encoding="utf-8"))
        del runner["unit_test"]["driver_scope_cmd"]
        runner_p.write_text(json.dumps(runner), encoding="utf-8")
        man_p = (self.fx["ws"] / "P2" / "reports" /
                 "scaffold_manifest.json")
        man = json.loads(man_p.read_text(encoding="utf-8"))
        del man["integration"]
        man_p.write_text(json.dumps(man), encoding="utf-8")

        def work(module):        # 无登记要求：不写登记也应通过
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"), register=False)

        r = _run_with_fakes(self, self.fx, work)
        self.assertEqual(r["rc"], 0)
        # 降级为全量 cmd
        self.assertTrue(all("full" in c for c in r["ut_calls"]))

    def test_blocked_stop_and_resume(self):
        def _blocked_seq(prompt, workdir, log_stem, static=None,
                         gen_schema=None, final_static=False,
                         agent_budget_sec=0, task=None, model=None,
                         resume_session=None):
            return {"status": "done", "session_id": "s",
                    "fallback": False, "rounds": [{"seg": 1}],
                    "parsed": {"status": "blocked",
                               "notes": "词典缺虚构条目"},
                    "total_agent_sec": 0.2}

        with mock.patch.object(mono_mod.agent, "run_agent_seq",
                               _blocked_seq), \
                mock.patch("porter.common.vcs.commit_target") as _c:
            rc = mono_mod.run_exp_mono(self.fx["ws"])
        self.assertEqual(rc, 1)
        self.assertEqual(self._ledger()["modules"]["fx-a"]["status"],
                         "blocked")
        _c.assert_not_called()

        def work(module):
            _write_module_product(self.fx["home"], module.replace("-", "_"))

        r2 = _run_with_fakes(self, self.fx, work)
        self.assertEqual(r2["rc"], 0)
        self.assertEqual([c["module"] for c in r2["seq"].calls],
                         ["fx-a", "fx-b"])

    def test_budget_and_session_override(self):
        def work(module):
            _write_module_product(self.fx["home"], module.replace("-", "_"))

        seq = _FakeSeq(work)

        def _shell_ut(cmd, cwd, env, timeout_sec, log_path):
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text("UT-DONE x\n", encoding="utf-8")
            return 0, "UT-DONE x\n"

        with mock.patch.object(mono_mod.agent, "run_agent_seq", seq), \
                mock.patch.object(mono_mod.probe_mod, "probe_build",
                                  return_value={"item": "build", "ok": True,
                                                "detail": "rc=0"}), \
                mock.patch.object(mono_mod, "_shell_ut", _shell_ut), \
                mock.patch("porter.common.vcs.commit_target",
                           return_value=["deadbeef0"]), \
                mock.patch.object(mono_mod.probe_mod, "probe_boot",
                                  return_value={"item": "final_boot",
                                                "ok": True,
                                                "detail": "rc=0"}):
            rc = mono_mod.run_exp_mono(self.fx["ws"], module="fx-a",
                                       budget=1800,
                                       session="ses_old_run")
        self.assertEqual(rc, 0)
        self.assertEqual(seq.calls[0]["budget"], 1800)
        self.assertEqual(seq.calls[0]["resume_session"], "ses_old_run")
        entry = self._ledger()["modules"]["fx-a"]
        self.assertEqual(entry["budget_sec"], 1800)
        self.assertEqual(entry["session_id"], "ses_fx")

    def test_resume_baseline_cumulative(self):
        # 续跑基线重建：上次已交付产物 + snap_base 在 ledger——本轮
        # 零新增代码也必须过增量守卫（累计 delta 语义），不误杀
        _write_module_product(self.fx["home"], "fx_a_logic")
        led_p = self.fx["ws"] / "exp-mono" / "ledger.json"
        led_p.parent.mkdir(parents=True, exist_ok=True)
        led = {"modules": {}} if not led_p.exists() else \
            json.loads(led_p.read_text(encoding="utf-8"))
        led["modules"]["fx-a"] = {
            "status": "decl-mismatch",
            "snap_base": {"code_lines": 0, "marker": 0},
            "snap_end": {"code_lines": 12, "marker": 1}}
        led_p.write_text(json.dumps(led), encoding="utf-8")

        def work(module):
            if module != "fx-a":     # fx-a 测零新增续跑；fx-b 正常交付
                _write_module_product(self.fx["home"],
                                      module.replace("-", "_"))

        r = _run_with_fakes(self, self.fx, work=work,
                            session="ses_prev")
        self.assertEqual(r["rc"], 0)
        self.assertEqual(self._ledger()["modules"]["fx-a"]["status"],
                         "pass")

    def test_resume_legacy_entry_baseline(self):
        # 旧式条目（无 snap_base/snap_end，仅 marker_delta）→ marker
        # 基线保守取 0、代码基线不可知 → 增量守卫跳过
        _write_module_product(self.fx["home"], "fx_a_logic")
        led_p = self.fx["ws"] / "exp-mono" / "ledger.json"
        led_p.parent.mkdir(parents=True, exist_ok=True)
        led_p.write_text(json.dumps(
            {"modules": {"fx-a": {"status": "decl-mismatch",
                                  "marker_delta": 1}}}), encoding="utf-8")

        def work(module):
            if module != "fx-a":
                _write_module_product(self.fx["home"],
                                      module.replace("-", "_"))

        r = _run_with_fakes(self, self.fx, work=work, session="ses_prev")
        self.assertEqual(r["rc"], 0)
        self.assertEqual(self._ledger()["modules"]["fx-a"]["status"],
                         "pass")

    def test_preconditions_rc2(self):
        (self.fx["ws"] / "P1" / "modules" / "deps.json").unlink()
        self.assertEqual(mono_mod.run_exp_mono(self.fx["ws"]), 2)
        self.setUp()                     # 重建 fixture
        self.assertEqual(
            mono_mod.run_exp_mono(self.fx["ws"], module="nope"), 2)
        # 依赖未 pass：只允许按序迁移
        self.assertEqual(
            mono_mod.run_exp_mono(self.fx["ws"], module="fx-b"), 2)


class TestExpMonoUnits(unittest.TestCase):

    def test_budget_clamp(self):
        self.assertEqual(mono_mod._budget_sec(100), 900)
        self.assertEqual(mono_mod._budget_sec(653), 900)
        self.assertEqual(mono_mod._budget_sec(2064), 2683)
        self.assertEqual(mono_mod._budget_sec(2719), 3534)
        self.assertEqual(mono_mod._budget_sec(9000), 4200)

    def test_code_lines_strips_comments(self):
        p = Path(tempfile.mkdtemp(prefix="exp_mono_u_")) / "f.cx"
        p.write_text("// note\n/* block\n * still */\ncode_a = 1;\n\n"
                     "# script note\ncode_b = 2;\n", encoding="utf-8")
        self.assertEqual(mono_mod._code_lines(p), 2)

    def test_run_ut_placeholder_substitution(self):
        tmp = Path(tempfile.mkdtemp(prefix="exp_mono_ut_"))
        calls = []

        def _shell_ut(cmd, cwd, env, timeout_sec, log_path):
            calls.append({"cmd": cmd, "env": env})
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text("UT-DONE x\n", encoding="utf-8")
            return 0, "UT-DONE x\n"

        runner = {"unit_test": {
            "driver_scope_cmd": "run {PORTER_TARGET_OS_ROOT} "
                                "{PORTER_DRIVER_HOME}",
            "cmd": "run-full", "timeout_sec": 5,
            "success_pattern": "UT-DONE", "fail_pattern": "UT-BAD"}}
        with mock.patch.object(mono_mod, "_shell_ut", _shell_ut):
            ok, detail, _ = mono_mod._run_ut(
                tmp, tmp / "tree", runner, "home/drv-x", "ut_label")
        self.assertTrue(ok)
        self.assertEqual(calls[0]["cmd"], f"run {tmp / 'tree'} home/drv-x")
        self.assertEqual(calls[0]["env"]["PORTER_DRIVER_HOME"], "home/drv-x")

    def test_run_ut_shell_form_preserved(self):
        # 命令模板里的 shell 形态 ${PORTER_TARGET_OS_ROOT} 必须原样保留
        # （由 env 展开），不能被单花括号占位符替换咬坏
        tmp = Path(tempfile.mkdtemp(prefix="exp_mono_ut2_"))
        (tmp / "tree").mkdir()
        runner = {"unit_test": {
            "cmd": "echo ${PORTER_TARGET_OS_ROOT}/x UT-DONE",
            "timeout_sec": 5,
            "success_pattern": "UT-DONE", "fail_pattern": "UT-BAD"}}
        ok, detail, log_path = mono_mod._run_ut(
            tmp, tmp / "tree", runner, "home/drv-x", "ut_full",
            which="full")
        self.assertTrue(ok)
        body = Path(log_path).read_text(encoding="utf-8")
        # shell 形态经 env 正确展开（无 "$/…" 咬坏痕迹）
        self.assertIn(f"{tmp / 'tree'}/x UT-DONE", body)
        self.assertNotIn("${", body)


if __name__ == "__main__":
    unittest.main()
