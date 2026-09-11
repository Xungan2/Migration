"""test_exp_mono.py — exp-mono 子命令的功能性测试（全 mock，零真实 agent）。

覆盖（研究/翻译拆分形态）：
  1. 拓扑推进：两模块按 order 各跑 研究任务→翻译任务 → 全 pass +
     词典/契约登记表收割 + 目标树按模块 commit + 终局 + report
  2. 幂等跳过：ledger 已 pass 的研究/翻译分别跳过
  3. 研究交付物校验回灌：首写坏块 → 同 session 续修 → 过；连坏 →
     invalid-deliverable 停车（1+RESEARCH_RETRIES 次）
  4. gate ① 失败短路 / ③ 启动失败短路 / 四段全绿
  5. 声明面自动重试：首败 → 同 session 续修 → 过（decl_retries 记账）；
     连败 → 重试上限后停车
  6. blocked 停车与重入；缺键降级；改动范围守卫；前置守卫 rc 2
  7. 双模型接线：研究调 reasoning、翻译调 coding（config 数据面）
  8. 仅研究模式（--module-research）：只研究不翻译
  9. 小件：双预算 clamp / 非注释行计数 / 占位符替换

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

FX_MODELS = ("fx/reasoning-model", "fx/coding-model")


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
    """模拟翻译 agent 交付：driver_home 内新文件 + 登记 + 测试标记。"""
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


def _deliverable_text(module: str, valid: bool = True, oq: bool = False) -> str:
    """合成研究交付物（坏块 = verdict 出词表；oq = 带 open_questions）。"""
    stem = module.replace("-", "_")
    data = {
        "module": module,
        "mappings": [{"symbol": f"api_{stem}",
                      "verdict": "equivalent" if valid else "SAME",
                      "usage": f"目标等价物 {stem}_eq",
                      "evidence": "home/drv/reg.txt:1",
                      "notes": "语义一致"}],
        "contracts": [
            {"name": f"Hook{stem}",
             "signature": f"type Hook{stem} = fn(&mut Dev) -> i32",
             "anchor": "home/drv/scaffold.cx:1",
             "consumers": module, "conflicts_with": ""}],
        "prunes": [], "parking": [],
        "read_list": [{"path": "home/drv/reg.txt", "purpose": "登记先例"}],
        "negatives": [],
        "open_questions": ([{"question": "锁习语取舍",
                             "verify": "对照先例"}] if oq else [])}
    return ("## 研究叙事（fx 合成）\n\n### 适配架构与整合方案\n\n"
            "（合成）直译。\n\n### 可测面评估\n\n（合成）全部可执行"
            "验证。\n\n```json\n"
            + json.dumps(data, ensure_ascii=False) + "\n```\n")


class _FakeSplitSeq:
    """run_agent_seq 的双任务 mock：按 task.step 分派研究/翻译。

    - research_bad：<模块名> 首写坏交付物的次数（校验回灌测试用）
    - decl：callable(module) -> 翻译记账声明（默认与产物一致）
    - decl_seq：{module: [decl, ...]} 按调用序消费（自动重试测试用）
    """

    def __init__(self, translate_work, decl=None, static_fail_status="stalled",
                 research_bad=None, adhoc=None, research_oq=None):
        self.translate_work = translate_work
        self._decl = decl
        self.static_fail_status = static_fail_status
        self.research_bad = dict(research_bad or {})
        self.adhoc = dict(adhoc or {})          # 模块 → 兜底 mappings 列表
        self.research_oq = set(research_oq or ())  # 带 open_questions 的模块
        self.calls = []              # 全部调用（含 step/model/session）
        self.static_results = []
        self._decl_seq = {}

    def __call__(self, prompt, workdir, log_stem, static=None,
                 gen_schema=None, final_static=False, agent_budget_sec=0,
                 task=None, model=None, resume_session=None,
                 fix_floor_sec=0, stall_meta_rounds=0):
        task = task or {}
        step = task.get("step", "translate")
        module = task.get("module")
        rec = {"step": step, "module": module, "prompt": prompt,
               "budget": agent_budget_sec, "gen_schema": gen_schema,
               "static": static, "resume_session": resume_session,
               "model": model}
        self.calls.append(rec)
        if step == "research":
            exp_dir = Path(log_stem).parent.parent
            dpath = exp_dir / "research" / f"{module}.md"
            valid = self.research_bad.get(module, 0) <= 0
            if not valid:
                self.research_bad[module] -= 1
            dpath.write_text(_deliverable_text(
                module, valid=valid, oq=module in self.research_oq),
                encoding="utf-8")
            return {"status": "done", "session_id": f"ses_r_{module}",
                    "fallback": False, "rounds": [{"seg": 1}],
                    "parsed": {"status": "done",
                               "deliverable": str(dpath),
                               "notes": "研究完成"},
                    "total_agent_sec": 0.3}
        # ---- 翻译任务（旧 _FakeSeq 行为） ----
        self.translate_work(module)
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
        if module in self._decl_seq and self._decl_seq[module]:
            d = self._decl_seq[module].pop(0)
            decl = d if d is not None else {}
        else:
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
        if self.adhoc.get(module):
            parsed["mappings"] = self.adhoc[module]
        return {"status": "done", "session_id": "ses_fx",
                "fallback": False, "rounds": [{"seg": 1}, {"seg": 2}],
                "parsed": parsed,
                "total_agent_sec": 1.5}

    def steps(self, step, module=None):
        return [c for c in self.calls if c["step"] == step
                and (module is None or c["module"] == module)]


def _run_with_fakes(test, fx, translate_work, *, build_ok=True, boot_ok=True,
                    decl=None, decl_seq=None, session=None,
                    research_bad=None, adhoc=None, research_oq=None, **kw):
    """通用环境：patch seq/build/ut/commit/boot/模型加载后跑 loop。"""
    seq = _FakeSplitSeq(translate_work, decl=decl,
                        research_bad=research_bad, adhoc=adhoc,
                        research_oq=research_oq)
    if decl_seq:
        seq._decl_seq = {k: list(v) for k, v in decl_seq.items()}
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
            mock.patch.object(mono_mod, "_load_models",
                              return_value=FX_MODELS), \
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
        rc = mono_mod.run_exp_mono(fx["ws"], session=session, **kw)
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
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"))

        r = _run_with_fakes(self, self.fx, work)
        self.assertEqual(r["rc"], 0)
        self.assertEqual([(c["step"], c["module"]) for c in r["seq"].calls],
                         [("research", "fx-a"), ("translate", "fx-a"),
                          ("research", "fx-b"), ("translate", "fx-b")])
        led = self._ledger()
        for m in ("fx-a", "fx-b"):
            self.assertEqual(led["modules"][m]["status"], "pass")
            self.assertEqual(led["modules"][m]["research"]["status"],
                             "pass")
        # notes 持久化：done 路径也留档（矛盾上报/兜底裁定的通道）
        self.assertEqual(led["modules"]["fx-a"]["notes"], "虚构完成")
        self.assertEqual(len(r["commits"]), 2)
        self.assertIn("build+ut green", r["commits"][0])
        # 终局：最后一次 ut 是全量 cmd；scope 形态只在翻译轮出现
        self.assertNotIn("{PORTER", r["ut_calls"][-1])
        self.assertEqual(len([c for c in r["ut_calls"] if "scope" in c]), 2)
        # 知识收割：词典节 + 契约登记表
        notes = (self.fx["ws"] / "exp-mono" / "mapping-notes.md").read_text(
            encoding="utf-8")
        self.assertIn("## fx-a", notes)
        self.assertIn("- api_fx_a | equivalent |", notes)
        reg = (self.fx["ws"] / "exp-mono" / "contracts.md").read_text(
            encoding="utf-8")
        self.assertIn("- **Hookfx_a**：", reg)
        # prompt 组装：研究 prompt 注入词典/契约/泊车/交付物路径，
        # 且输入面 ⊇ mono 输入面∩研究相关（登记模板/测试基质/现有
        # 文件清单——防拆分后漂移的回归守卫）
        rp = r["seq"].steps("research", "fx-a")[0]["prompt"]
        self.assertIn("迁移词典", rp)
        self.assertIn("契约登记表", rp)
        self.assertIn("泊车记录", rp)
        self.assertIn("research/fx-a.md", rp)
        self.assertIn("虚构核心词汇", rp)
        self.assertIn("decl {stem};", rp)       # 登记约定（构建入口）
        self.assertIn("@MARK", rp)              # 测试基质（可测面依据）
        self.assertIn("scaffold.cx", rp)        # driver_home 现有文件清单
        # 翻译 prompt 注入交付物全文/契约/泊车/基质，但不注入词典
        tp = r["seq"].steps("translate", "fx-a")[0]["prompt"]
        self.assertIn("研究交付物", tp)
        self.assertIn("api_fx_a", tp)
        self.assertIn("Hookfx_a", tp)
        self.assertIn("@MARK", tp)
        self.assertIn("decl {stem};", tp)
        self.assertIn("泊车记录", tp)
        self.assertIn("接线白名单", tp)         # 白名单注入（硬性①引用对齐）
        self.assertIn("top.txt", tp)
        self.assertNotIn("迁移词典（JIT 接力）", tp)
        # 双任务 gen_schema
        self.assertEqual(r["seq"].steps("research")[0]["gen_schema"],
                         {"status": "str", "deliverable": "str",
                          "notes": "str"})
        self.assertEqual(r["seq"].steps("translate")[0]["gen_schema"],
                         {"status": "str", "files": "list", "notes": "str",
                          "migrated_functions": "list", "tests": "list",
                          "untested": "list"})
        report = (self.fx["ws"] / "exp-mono" / "report.md").read_text(
            encoding="utf-8")
        self.assertIn("全量单测：PASS", report)
        self.assertIn("函数覆盖台账", report)
        self.assertIn("词典+1", report)        # 研究列的收割统计

    def test_models_wiring(self):
        def work(module):
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"))

        r = _run_with_fakes(self, self.fx, work)
        self.assertEqual(r["rc"], 0)
        self.assertTrue(all(c["model"] == FX_MODELS[0]
                            for c in r["seq"].steps("research")))
        self.assertTrue(all(c["model"] == FX_MODELS[1]
                            for c in r["seq"].steps("translate")))

    def test_models_missing_rc2(self):
        with mock.patch.object(mono_mod, "_load_models",
                               return_value=(None, "config 缺 models 块")):
            rc = mono_mod.run_exp_mono(self.fx["ws"])
        self.assertEqual(rc, 2)

    def test_idempotent_skip(self):
        def work(module):
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"))

        r1 = _run_with_fakes(self, self.fx, work)
        self.assertEqual(r1["rc"], 0)
        r2 = _run_with_fakes(self, self.fx, work)
        self.assertEqual(r2["rc"], 0)
        self.assertEqual(r2["seq"].calls, [])

    def test_gate_short_circuit(self):
        def work(module):        # 什么都不写 → 翻译产物守卫必败
            pass

        r = _run_with_fakes(self, self.fx, work)
        self.assertEqual(r["rc"], 1)
        self.assertEqual(r["ut_calls"], [])
        self.assertEqual(r["commits"], [])
        self.assertNotEqual(self._ledger()["modules"]["fx-a"]["status"],
                            "pass")
        self.assertFalse(r["seq"].static_results[0]["ok"])
        self.assertIn("产物守卫", r["seq"].static_results[0]["out"])

    def test_gate_boot_fail_short_circuits_ut(self):
        def work(module):
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"))

        r = _run_with_fakes(self, self.fx, work, boot_ok=False)
        self.assertEqual(r["rc"], 1)
        self.assertEqual(r["ut_calls"], [])          # ④ 单测未触达
        self.assertFalse(r["seq"].static_results[0]["ok"])
        self.assertIn("③ 启动 FAIL", r["seq"].static_results[0]["out"])
        self.assertEqual(r["commits"], [])

    def test_gate_four_stage_all_green(self):
        def work(module):
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"))

        r = _run_with_fakes(self, self.fx, work)
        self.assertEqual(r["rc"], 0)
        self.assertTrue(r["seq"].static_results[0]["ok"])
        self.assertIn("四段全绿", r["seq"].static_results[0]["out"])

    def test_decl_marker_consistency_exhausts_retries(self):
        # 声明 1 测试但产物零标记：自动重试 2 次仍不过 → 停车
        def work(module):
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"), marker=False)

        r = _run_with_fakes(self, self.fx, work)
        self.assertEqual(r["rc"], 1)
        # 每次翻译调用都跑四段 gate（重试也全量验证）→ ut 3 次
        self.assertEqual(len(r["ut_calls"]), 3)
        led = self._ledger()["modules"]["fx-a"]
        self.assertEqual(led["status"], "decl-mismatch")
        self.assertEqual(led.get("decl_retries"), 2)
        # 自动重试走同 session 续接
        tr = r["seq"].steps("translate", "fx-a")
        self.assertEqual(len(tr), 3)
        self.assertEqual(tr[1]["resume_session"], "ses_fx")
        self.assertIn("声明面核对未通过", tr[1]["prompt"])

    def test_decl_auto_retry_recovers(self):
        # 首轮记账漏挂 1 单元 → 自动重试补齐 → pass（进程内闭环，零人工）
        def work(module):
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"))

        seq_decl = {"fx-a": [
            {"migrated_functions": ["fx_a_logic", "fx_a_math"],
             "tests": [{"fn": "fx_a_logic", "aspect": "truth table",
                        "name": "probe_fx_a_a"}],
             "untested": []},
            None]}      # None → 回退默认（完备记账）
        r = _run_with_fakes(self, self.fx, work, decl_seq=seq_decl)
        self.assertEqual(r["rc"], 0)
        led = self._ledger()["modules"]["fx-a"]
        self.assertEqual(led["status"], "pass")
        self.assertEqual(led.get("decl_retries"), 1)

    def test_decl_fixed_leftover(self):
        # 首轮记账漏挂被打回、重试补齐 → 被修掉的问题清单留 decl_fixed
        # （此前成功后只存计数，"当时哪几条对不上"蒸发）
        def work(module):
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"))

        seq_decl = {"fx-a": [
            {"migrated_functions": ["fx_a_logic", "fx_a_math"],
             "tests": [{"fn": "fx_a_logic", "aspect": "truth table",
                        "name": "probe_fx_a_a"}],
             "untested": []},
            None]}
        r = _run_with_fakes(self, self.fx, work, decl_seq=seq_decl)
        self.assertEqual(r["rc"], 0)
        led = self._ledger()["modules"]["fx-a"]
        self.assertTrue(led.get("decl_fixed"))
        self.assertTrue(any("fx_a_math" in p for p in led["decl_fixed"]))

    def test_adhoc_mappings_reflux(self):
        # 兜底 mappings 回流词典：合法条目入「翻译兜底」节带标注，
        # verdict 出表的条目静默跳过
        def work(module):
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"))

        adhoc = {"fx-a": [
            {"symbol": "api_z", "verdict": "helper",
             "usage": "组合 SpinLock 加 timer",
             "evidence": "home/drv/reg.txt:2"},
            {"symbol": "api_bad", "verdict": "SAME",
             "usage": "x", "evidence": "home/drv/reg.txt:3"}]}
        r = _run_with_fakes(self, self.fx, work, adhoc=adhoc)
        self.assertEqual(r["rc"], 0)
        notes = (self.fx["ws"] / "exp-mono" / "mapping-notes.md").read_text(
            encoding="utf-8")
        self.assertIn("## fx-a 翻译兜底", notes)
        self.assertIn("- api_z | helper | 组合 SpinLock 加 timer | "
                      "home/drv/reg.txt:2（兜底：翻译期发现）", notes)
        self.assertNotIn("api_bad", notes)
        led = self._ledger()["modules"]["fx-a"]
        self.assertEqual(led.get("dict_adhoc"), 1)

    def test_research_notes_and_open_questions_in_ledger(self):
        # 研究侧 notes pass 路径持久化（对称化）+ open_questions 摘录入
        # ledger（人首次可见"研究者在哪些点没把握"）
        def work(module):
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"))

        r = _run_with_fakes(self, self.fx, work, research_oq={"fx-a"})
        self.assertEqual(r["rc"], 0)
        res = self._ledger()["modules"]["fx-a"]["research"]
        self.assertEqual(res.get("notes"), "研究完成")
        self.assertEqual(res.get("open_questions"),
                         [{"question": "锁习语取舍", "verify": "对照先例"}])
        self.assertNotIn("open_questions",
                         self._ledger()["modules"]["fx-b"]["research"])

    def test_decl_overlap_rejected(self):
        def work(module):
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"))

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
        def work(module):
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"), marker=False)

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
        self.assertNotIn("decl_retries", led)

    def test_change_scope_guard(self):
        def work(module):
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"),
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
        self.assertTrue(all("full" in c for c in r["ut_calls"]))

    def test_blocked_stop_and_resume(self):
        def _blocked_translate(prompt, workdir, log_stem, static=None,
                               gen_schema=None, final_static=False,
                               agent_budget_sec=0, task=None, model=None,
                               resume_session=None, fix_floor_sec=0,
                               stall_meta_rounds=0):
            task = task or {}
            if task.get("step") == "research":
                module = task.get("module")
                exp_dir = Path(log_stem).parent.parent
                dpath = exp_dir / "research" / f"{module}.md"
                dpath.write_text(_deliverable_text(module),
                                 encoding="utf-8")
                return {"status": "done", "session_id": "s",
                        "fallback": False, "rounds": [{"seg": 1}],
                        "parsed": {"status": "done",
                                   "deliverable": str(dpath),
                                   "notes": "研究完成"},
                        "total_agent_sec": 0.2}
            return {"status": "done", "session_id": "s",
                    "fallback": False, "rounds": [{"seg": 1}],
                    "parsed": {"status": "blocked",
                               "notes": "交付物缺虚构条目"},
                    "total_agent_sec": 0.2}

        with mock.patch.object(mono_mod.agent, "run_agent_seq",
                               _blocked_translate), \
                mock.patch.object(mono_mod, "_load_models",
                                  return_value=FX_MODELS), \
                mock.patch("porter.common.vcs.commit_target") as _c:
            rc = mono_mod.run_exp_mono(self.fx["ws"])
        self.assertEqual(rc, 1)
        led = self._ledger()["modules"]["fx-a"]
        self.assertEqual(led["status"], "blocked")
        self.assertEqual(led["research"]["status"], "pass")  # 研究已过
        _c.assert_not_called()

        def work(module):
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"))

        r2 = _run_with_fakes(self, self.fx, work)
        self.assertEqual(r2["rc"], 0)
        # fx-a 研究已 pass 跳过；翻译重跑；fx-b 研究+翻译
        self.assertEqual([(c["step"], c["module"]) for c in r2["seq"].calls],
                         [("translate", "fx-a"),
                          ("research", "fx-b"), ("translate", "fx-b")])

    def test_budget_and_session_override(self):
        def work(module):
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"))

        seq = _FakeSplitSeq(work)

        def _shell_ut(cmd, cwd, env, timeout_sec, log_path):
            Path(log_path).parent.mkdir(parents=True, exist_ok=True)
            Path(log_path).write_text("UT-DONE x\n", encoding="utf-8")
            return 0, "UT-DONE x\n"

        with mock.patch.object(mono_mod.agent, "run_agent_seq", seq), \
                mock.patch.object(mono_mod, "_load_models",
                                  return_value=FX_MODELS), \
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
                                       budget=1800, budget_research=555,
                                       session="ses_old_run")
        self.assertEqual(rc, 0)
        res = seq.steps("research", "fx-a")[0]
        tr = seq.steps("translate", "fx-a")[0]
        # 研究从未跑过 → session 不给研究（留给翻译，旧语义）
        self.assertIsNone(res["resume_session"])
        self.assertEqual(res["budget"], 555)
        self.assertEqual(tr["resume_session"], "ses_old_run")
        self.assertEqual(tr["budget"], 1800)
        led = self._ledger()["modules"]["fx-a"]
        self.assertEqual(led["budget_sec"], 1800)
        self.assertEqual(led["research"]["budget_sec"], 555)

    def test_research_validation_retry_then_pass(self):
        def work(module):
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"))

        r = _run_with_fakes(self, self.fx, work,
                            research_bad={"fx-a": 1})
        self.assertEqual(r["rc"], 0)
        res = r["seq"].steps("research", "fx-a")
        self.assertEqual(len(res), 2)
        self.assertEqual(res[1]["resume_session"], "ses_r_fx-a")
        self.assertIn("交付物校验未通过", res[1]["prompt"])
        led = self._ledger()["modules"]["fx-a"]
        self.assertEqual(led["research"]["status"], "pass")
        self.assertEqual(led["research"]["validate_attempts"], 2)

    def test_research_validation_exhausted(self):
        def work(module):
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"))

        r = _run_with_fakes(self, self.fx, work,
                            research_bad={"fx-a": 99})
        self.assertEqual(r["rc"], 1)
        self.assertEqual(len(r["seq"].steps("research", "fx-a")),
                         1 + mono_mod.RESEARCH_RETRIES)
        led = self._ledger()["modules"]["fx-a"]
        self.assertEqual(led["research"]["status"],
                         "invalid-deliverable")
        # 研究未过 → 翻译不跑
        self.assertEqual(r["seq"].steps("translate"), [])

    def test_module_research_mode(self):
        def work(module):
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"))

        r = _run_with_fakes(self, self.fx, work,
                            module_research="fx-a")
        self.assertEqual(r["rc"], 0)
        self.assertEqual([(c["step"], c["module"]) for c in r["seq"].calls],
                         [("research", "fx-a")])
        led = self._ledger()["modules"]["fx-a"]
        self.assertEqual(led["research"]["status"], "pass")
        self.assertNotIn("status", led)         # 翻译未跑，无翻译状态
        report = (self.fx["ws"] / "exp-mono" / "report.md").read_text(
            encoding="utf-8")
        self.assertIn("未到达", report)          # 终局未触达

    def test_resume_baseline_cumulative(self):
        # 翻译续跑基线：上次已交付产物 + snap_base 在 ledger——本轮
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

        r = _run_with_fakes(self, self.fx, work,
                            session="ses_prev")
        self.assertEqual(r["rc"], 0)
        self.assertEqual(self._ledger()["modules"]["fx-a"]["status"],
                         "pass")
        # 翻译续跑拿到 session（研究从未跑过，不消费）
        tr = r["seq"].steps("translate", "fx-a")
        self.assertEqual(tr[0]["resume_session"], "ses_prev")

    def test_research_resume_gets_session(self):
        # 研究续跑（上次 invalid-deliverable）→ session 给研究任务
        led_p = self.fx["ws"] / "exp-mono" / "ledger.json"
        led_p.parent.mkdir(parents=True, exist_ok=True)
        led_p.write_text(json.dumps(
            {"modules": {"fx-a": {
                "research": {"status": "invalid-deliverable"}}}}),
            encoding="utf-8")

        def work(module):
            _write_module_product(self.fx["home"],
                                  module.replace("-", "_"))

        r = _run_with_fakes(self, self.fx, work, session="ses_res")
        self.assertEqual(r["rc"], 0)
        res = r["seq"].steps("research", "fx-a")
        self.assertEqual(res[0]["resume_session"], "ses_res")
        # 研究消费后，翻译不再拿 session
        tr = r["seq"].steps("translate", "fx-a")
        self.assertIsNone(tr[0]["resume_session"])

    def test_preconditions_rc2(self):
        (self.fx["ws"] / "P1" / "modules" / "deps.json").unlink()
        self.assertEqual(mono_mod.run_exp_mono(self.fx["ws"]), 2)
        self.setUp()                     # 重建 fixture
        with mock.patch.object(mono_mod, "_load_models",
                               return_value=FX_MODELS):
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
        self.assertEqual(mono_mod._budget_sec(9000), 4200)

    def test_budget_research_clamp(self):
        self.assertEqual(mono_mod._budget_research(100), 600)
        self.assertEqual(mono_mod._budget_research(800), 1200)
        self.assertEqual(mono_mod._budget_research(9000), 2400)

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
        self.assertIn(f"{tmp / 'tree'}/x UT-DONE", body)
        self.assertNotIn("${", body)


if __name__ == "__main__":
    unittest.main()
