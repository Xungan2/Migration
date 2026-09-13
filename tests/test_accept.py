"""test_accept.py — accept 子命令的功能性测试（全 mock agent，真实 invoke）。

覆盖（节文件+消费脚本对形态：Tier2 §5/§6 + Tier3 §7 + t1 骨架留空 +
双关口 + 七节索引）：
  1. 全链：t1 跳过（未实现）→ Tier2 → 关口② → 放行 → Tier3 → 关口③
     → 放行 → 七节索引（bound 3/7；§1-§4 missing）
  2. t1 骨架：--tier t1 rc 2；§1-§4 不被生成；缺节时 --execute rc 2
  3. 结构校验回炉：坏节 JSON → 同 session 续修 → 连坏 → invalid 停车
  4. invoke 判定未过（check.py exit 1）→ unverified rc 1
  5. blocked 停车；PORTER_NO_AGENT rc 2；前置缺失 rc 2；exp-mono 未全
     pass rc 2
  6. 静态段小件：范围守卫（结构/越界/执行三态）
  7. 关口多文件联合指纹：放行后改任一节文件 → 重置 open
  8. 执行相位：阶梯 fail-fast / agent 修环 / 终态 / panic / 指纹冻结
     （fixture 预播 §1-§4 节对模拟未来 Tier1 产物，使索引 7/7 bound）

fixture：fake-tree / home/drv-x / echo 型命令 + 真 tiny check.py
（模板化，逐节参数化）——invoke 走真实子进程，不 mock supervisor。
"""
import contextlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.exp import accept as accept_mod
from porter.exp import accept_gate as gate_mod

CHECK_PY = '''#!/usr/bin/env python3
import json, os, subprocess, sys
doc = json.loads(open(sys.argv[1], encoding="utf-8").read())
out = []
for c in doc.get("commands") or []:
    cmd = c if isinstance(c, str) else c.get("cmd", "")
    p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                       env=dict(os.environ))
    out.append((p.stdout or "") + (p.stderr or ""))
text = "\\n".join(out)
ok = True
for t in (doc.get("expect") or {}).get("log_contains") or []:
    ok = ok and (t in text)
print("VERDICT:", "pass" if ok else "fail")
sys.exit(0 if ok else 1)
'''

# 模拟未来 Tier1 提取产物（执行相位测试用：预播进 acceptance/）
SECS_T1 = {
    1: ("module-build", "echo module-build-ok", "module-build-ok"),
    2: ("unit-test", "echo unit-test-ok", "unit-test-ok"),
    3: ("image-build", "echo image-build-ok", "image-build-ok"),
    4: ("boot-autostart", "echo DRV autostart ok", "DRV autostart ok"),
}
SECS_INJECT = {
    5: ("device-inject", "echo DRV matched device", "DRV matched device"),
    6: ("simple-interact", "echo DRV interact ok", "DRV interact ok"),
}
SEC_E2E = (7, "e2e", "echo E2E receipt ok", "E2E receipt ok")

# 树依赖变体：§5 检查 driver.cx 里 probe-ok 标记（执行相位红→修→绿用）
S5_TREE_DEP = ("device-inject",
               "grep probe-ok ${PORTER_DRIVER_HOME}/driver.cx", "probe-ok")


def _sec_doc(num, slug, cmd, contains):
    return {"section": num, "title": slug,
            "commands": [{"cmd": cmd, "timeout_sec": 60}],
            "expect": {"rc": 0, "log_contains": [contains]},
            "paths": [], "notes": "fixture"}


def _write_pair(acc_dir: Path, num, slug, cmd, contains, bad=False):
    j = acc_dir / f"{num}-{slug}.json"
    j.write_text("NOT-JSON{{{"
                 if bad else json.dumps(
                     _sec_doc(num, slug, cmd, contains), ensure_ascii=False,
                     indent=1), encoding="utf-8")
    c = acc_dir / f"{num}-{slug}.check.py"
    c.write_text(CHECK_PY, encoding="utf-8")
    return j


def _mk_fixture(tmp: Path) -> dict:
    tree = tmp / "fake-tree"
    home = tree / "home" / "drv-x"
    home.mkdir(parents=True)
    (home / "driver.cx").write_text("unit scaffold;\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tree, check=True,
                   capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=tree, check=True,
                   capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "init"], cwd=tree, check=True,
                   capture_output=True)
    ws = tmp / "ws"
    ws.mkdir()
    (ws / "project.json").write_text(json.dumps({
        "target_os": str(tree), "linux_driver": str(tmp / "linux-x")}),
        encoding="utf-8")
    # 刻意不建 runner.json——accept 已全链脱钩（boot 事实走 runner.md
    # 指针），fixture 证明零依赖。
    (ws / "module-divsion.json").write_text(json.dumps({
        "order": ["m1"],
        "modules": {"m1": {"source_files": ["drv.c"], "depends_on": [],
                           "verification": ["fx"], "status": "planned"}},
        "driver_home": "home/drv-x", "unknown": []}), encoding="utf-8")
    (ws / "exp-mono").mkdir()
    (ws / "exp-mono" / "ledger.json").write_text(json.dumps({
        "modules": {"m1": {"status": "pass"}}}), encoding="utf-8")
    return {"ws": ws, "tree": tree, "home": home}


def _make_agent(calls, bad_inject=False, bad_e2e_expect=False,
                s5_tree_dep=False, exe_fix=None, exe_blocked=None,
                exe_seq=None):
    """run_agent_seq mock：按 task.step 写节文件对 + 改树，返回 done。

    s5_tree_dep：§5 判据依赖 driver.cx 的 probe-ok 标记（inject 步会写
    入标记使设计期验证通过）；exe_* 控制 execute 步行为：
    exe_seq='budget-exhausted' 等非 done 终态 / exe_blocked=blocked
    notes / exe_fix=True 修树 / exe_fix=False 说谎 done。
    """
    state = {"ws": "", "tree": ""}

    def fake(prompt, workdir, log_stem, **kw):
        step = (kw.get("task") or {}).get("step")
        calls.append({"step": step, "prompt": prompt,
                      "resume": kw.get("resume_session")})
        acc = Path(state["ws"]) / "exp-accept" / "acceptance"
        acc.mkdir(parents=True, exist_ok=True)
        drv = Path(state["tree"]) / "home" / "drv-x" / "driver.cx"
        if step == "inject":
            secs = dict(SECS_INJECT)
            if s5_tree_dep:
                secs[5] = S5_TREE_DEP
            for num, (slug, cmd, contains) in secs.items():
                _write_pair(acc, num, slug, cmd, contains,
                            bad=(bad_inject and num == 5))
            if s5_tree_dep:
                drv.write_text(drv.read_text(encoding="utf-8")
                               + "// +probe log (accept)\nprobe-ok\n",
                               encoding="utf-8")
            else:
                drv.write_text(drv.read_text(encoding="utf-8")
                               + "// +probe log (accept)\n", encoding="utf-8")
        elif step == "e2e":
            num, slug, cmd, contains = SEC_E2E
            contains = "never-appears" if bad_e2e_expect else contains
            _write_pair(acc, num, slug, cmd, contains)
        elif step == "execute":
            if exe_seq is not None:
                return {"status": exe_seq, "session_id": "ses-exe",
                        "parsed": {}, "rounds": [],
                        "total_agent_sec": 1.0}
            if exe_blocked is not None:
                return {"status": "done", "session_id": "ses-exe",
                        "parsed": {"status": "blocked",
                                   "notes": exe_blocked},
                        "rounds": [], "total_agent_sec": 0.1}
            if exe_fix:
                drv.write_text(drv.read_text(encoding="utf-8")
                               + "probe-ok\n", encoding="utf-8")
            return {"status": "done", "session_id": "ses-exe",
                    "parsed": {"status": "done", "notes": "fx"},
                    "rounds": [], "total_agent_sec": 0.1}
        else:
            raise AssertionError(f"未知 step {step}")
        return {"status": "done", "session_id": f"ses-{step}",
                "parsed": {"status": "done", "deliverable": str(acc),
                           "notes": "fx-notes"},
                "rounds": [{"seg": 1}], "total_agent_sec": 0.1}

    fake.state = state
    return fake


class _ctx:

    def __init__(self, patches):
        self._patches, self._stack = patches, None

    def __enter__(self):
        self._stack = contextlib.ExitStack()
        for p in self._patches:
            self._stack.enter_context(p)
        return self._stack

    def __exit__(self, *exc):
        self._stack.close()
        return False


class _Base(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="accept_fx_"))
        self.fx = _mk_fixture(self.tmp)
        self.calls: list[dict] = []
        self.agent = _make_agent(self.calls)
        self.agent.state["ws"] = str(self.fx["ws"])
        self.agent.state["tree"] = str(self.fx["tree"])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _patches(self, agent_fn=None):
        return [mock.patch.object(accept_mod.agent, "run_agent_seq",
                                  agent_fn or self.agent),
                mock.patch("porter.common.vcs.commit_target",
                           lambda *a, **k: ["fxhash"])]

    def _run(self, agent_fn=None, **kw):
        with _ctx(self._patches(agent_fn)):
            return accept_mod.run_accept(self.fx["ws"], **kw)

    def _answer(self, gate_id, verdict, note=None):
        with (self.fx["ws"] / "answers.md").open("a", encoding="utf-8") as fh:
            fh.write(f"## @{gate_id}\nverdict: {verdict}\n"
                     + (f"note: {note}\n" if note else "") + "\n")

    def _ledger(self) -> dict:
        return json.loads((self.fx["ws"] / "exp-accept" /
                           "ledger.json").read_text(encoding="utf-8"))

    def _ready_index(self, agent_fn=None):
        """走完设计期全流程 → 七节索引登记（rc 0）。

        预播 §1-§4 节对模拟未来 Tier1 提取产物（t1 机制未实现，执行
        相位测试需要 7/7 bound 才能进 --execute）。
        """
        acc = self.fx["ws"] / "exp-accept" / "acceptance"
        acc.mkdir(parents=True, exist_ok=True)
        for num, (slug, cmd, contains) in SECS_T1.items():
            _write_pair(acc, num, slug, cmd, contains)
        self.assertEqual(self._run(agent_fn), 3)
        self._answer("exp-accept.inject", "approve")
        self.assertEqual(self._run(agent_fn), 3)
        self._answer("exp-accept.plan", "approve")
        self.assertEqual(self._run(agent_fn), 0)

    def _revert_driver(self):
        """回退 driver.cx 到 HEAD（去掉 probe-ok 标记 → §5 树依赖变体红）。"""
        subprocess.run(["git", "-C", str(self.fx["tree"]), "checkout",
                        "--", "home/drv-x/driver.cx"], check=True,
                       capture_output=True)


class TestAcceptFlow(_Base):

    def test_full_flow_to_index(self):
        self.assertEqual(self._run(), 3)              # 关口② 待答
        acc = self.fx["ws"] / "exp-accept" / "acceptance"
        for n in (5, 6):
            self.assertTrue(list(acc.glob(f"{n}-*.json")),
                            f"§{n} 缺文件")
        for n in (1, 2, 3, 4):                        # t1 未实现：不生成
            self.assertFalse(list(acc.glob(f"{n}-*.json")),
                             f"§{n} 不应存在")
        led = self._ledger()
        self.assertEqual(led["tiers"]["inject"]["status"], "pass")
        self.assertEqual(len(led["tiers"]["inject"]["verify"]), 2)
        self.assertEqual(led["tiers"]["inject"]["commits"], ["fxhash"])
        self.assertTrue((self.fx["ws"] / "exp-accept" /
                         "inject-review.md").exists())
        self._answer("exp-accept.inject", "approve")
        self.assertEqual(self._run(), 3)              # 关口③ 待答
        self._answer("exp-accept.plan", "approve")
        self.assertEqual(self._run(), 0)              # 七节索引
        led = self._ledger()
        secs = led["acceptance"]["sections"]
        self.assertEqual([s["section"] for s in secs], list(range(1, 8)))
        bound = {s["section"] for s in secs if s["status"] == "bound"}
        self.assertEqual(bound, {5, 6, 7})            # §1-§4 missing
        self.assertTrue(all(s.get("sha16") for s in secs
                            if s["status"] == "bound"))

    def test_driver_home_source_chain(self):
        """driver_home 单源 = module-divsion.json：在场即用 / 缺则
        rc 2（旧源 mono-input-manifest / P2 scaffold 在场也不算数）。"""
        ws = self.fx["ws"]
        # ① 新源在场 → 值进 inject prompt
        self.assertEqual(self._run(), 3)
        inj = next(c["prompt"] for c in self.calls
                   if c["step"] == "inject")
        self.assertIn("home/drv-x", inj)
        # ② 新源缺键/缺席 → rc 2 零 agent（旧源在场也不救）
        (ws / "module-divsion.json").write_text(json.dumps({
            "order": [], "modules": {}, "unknown": []}), encoding="utf-8")
        (ws / "mono-input-manifest.json").write_text(json.dumps({
            "driver_home": "home/drv-old"}), encoding="utf-8")
        (ws / "P2").mkdir()
        (ws / "P2" / "reports").mkdir()
        (ws / "P2" / "reports" / "scaffold_manifest.json").write_text(
            json.dumps({"driver_home": "home/drv-old2"}),
            encoding="utf-8")
        n_calls = len(self.calls)
        self.assertEqual(self._run(), 2)
        self.assertEqual(len(self.calls), n_calls)
        (ws / "mono-input-manifest.json").unlink()
        (ws / "P2" / "reports" / "scaffold_manifest.json").unlink()
        self.assertEqual(self._run(), 2)

    def test_t1_skeleton_not_ready(self):
        # --tier t1：机制未实现 rc 2，零 agent
        self.assertEqual(self._run(tier="t1"), 2)
        self.assertFalse(self.calls)
        # 默认路由：t1 跳过不阻断，inject 正常推进
        self.assertEqual(self._run(), 3)
        self.assertTrue([c for c in self.calls if c["step"] == "inject"])
        # 双批后 --execute 被缺节挡住（rc 2），§1-§4 未绑定
        self._answer("exp-accept.inject", "approve")
        self._run()
        self._answer("exp-accept.plan", "approve")
        self.assertEqual(self._run(), 0)
        with _ctx(self._patches()):
            rc = accept_mod.run_accept(self.fx["ws"], execute=True)
        self.assertEqual(rc, 2)
        self.assertFalse([c for c in self.calls if c["step"] == "execute"])

    def test_validation_rework_then_exhaustion(self):
        agent = _make_agent(self.calls, bad_inject=True)
        agent.state["ws"] = str(self.fx["ws"])
        agent.state["tree"] = str(self.fx["tree"])
        self.assertEqual(self._run(agent), 1)
        led = self._ledger()
        self.assertEqual(led["tiers"]["inject"]["status"], "invalid")
        self.assertEqual(led["tiers"]["inject"]["validate_attempts"],
                         1 + accept_mod.VERIFY_RETRIES)
        self.assertEqual(len([c for c in self.calls
                              if c["step"] == "inject"]),
                         1 + accept_mod.VERIFY_RETRIES)

    def test_invoke_red_is_unverified(self):
        self.assertEqual(self._run(), 3)
        self._answer("exp-accept.inject", "approve")
        agent = _make_agent(self.calls, bad_e2e_expect=True)
        agent.state["ws"] = str(self.fx["ws"])
        agent.state["tree"] = str(self.fx["tree"])
        self.assertEqual(self._run(agent), 1)
        led = self._ledger()
        self.assertEqual(led["tiers"]["e2e"]["status"], "unverified")
        self.assertTrue(any("§7" in p for p in
                            led["tiers"]["e2e"]["problems"]))

    def test_reject_then_tier_rerun_with_note(self):
        self.assertEqual(self._run(), 3)              # 关口② 登记
        self._answer("exp-accept.inject", "reject", note="换一个注入通路")
        self.assertEqual(self._run(), 3)              # 默认：不自动重做
        self.assertEqual(len([c for c in self.calls
                              if c["step"] == "inject"]), 1)
        self.assertEqual(self._run(tier="inject"), 3)  # 重做 → 重登记
        rerun = [c for c in self.calls if c["step"] == "inject"]
        self.assertEqual(len(rerun), 2)
        self.assertIn("换一个注入通路", rerun[-1]["prompt"])
        st, _ = gate_mod.gate_state(self.fx["ws"], "exp-accept.inject")
        self.assertEqual(st, "open")                  # 旧 reject 已消费
        self._answer("exp-accept.inject", "approve")
        st, _ = gate_mod.gate_state(self.fx["ws"], "exp-accept.inject")
        self.assertEqual(st, "approved")

    def test_blocked_parks(self):
        def agent(prompt, workdir, log_stem, **kw):
            return {"status": "done", "session_id": "ses-b",
                    "parsed": {"status": "blocked", "notes": "缺规格"},
                    "rounds": [], "total_agent_sec": 0.1}
        self.assertEqual(self._run(agent), 1)
        self.assertEqual(self._ledger()["tiers"]["inject"]["status"],
                         "blocked")

    def test_preconditions(self):
        (self.fx["ws"] / "module-divsion.json").unlink()
        self.assertEqual(self._run(), 2)
        (self.fx["ws"] / "exp-mono" / "ledger.json").write_text(
            json.dumps({"modules": {"m1": {"status": "blocked"}}}),
            encoding="utf-8")
        self.assertEqual(self._run(), 2)

    def test_no_agent(self):
        import os
        with mock.patch.dict(os.environ, {"PORTER_NO_AGENT": "1"}):
            self.assertEqual(self._run(), 2)


class TestAcceptUnits(_Base):

    def test_static_scope_guard(self):
        ws = self.fx["ws"]
        acc = ws / "exp-accept" / "acceptance"
        acc.mkdir(parents=True)
        for num, (slug, cmd, contains) in SECS_INJECT.items():
            _write_pair(acc, num, slug, cmd, contains)
        baseline = accept_mod._mono._git_status(self.fx["tree"])
        static = accept_mod._make_static(
            ws, self.fx["tree"], "home/drv-x", (5, 6), baseline, "FX")
        with _ctx([]):
            ok, out = static["fn"]()
        self.assertTrue(ok)                           # 范围内：正常执行
        self.assertIn("§5", out)
        (self.fx["tree"] / "elsewhere.txt").write_text("x", encoding="utf-8")
        with _ctx([]):
            ok2, out2 = static["fn"]()                # 越界 → False
        self.assertFalse(ok2)
        self.assertIn("越出白名单", out2)
        # 节文件缺失 → 结构问题
        for num in (5, 6):
            for p in acc.glob(f"{num}-*"):
                p.unlink()
        with _ctx([]):
            ok3, out3 = static["fn"]()
        self.assertFalse(ok3)
        self.assertIn("结构问题", out3)

    def test_kb_and_boot_pointer_faces(self):
        """知识注入面 = 指针形式：无 head-N 原文截断，缺席省略。"""
        ws = self.fx["ws"]
        kb = ws / "knowledgebase"
        (kb / "build").mkdir(parents=True)
        (kb / "build" / "README.md").write_text(
            "SECRET-BUILD-CONTENT\n" * 60, encoding="utf-8")
        out = accept_mod._kb_face(ws)
        self.assertIn(str(kb / "build" / "README.md"), out)   # 路径指针
        self.assertIn("先读", out)
        self.assertNotIn("SECRET-BUILD-CONTENT", out)         # 不注入正文
        self.assertNotIn(str(kb / "boot" / "README.md"), out)  # 缺席省略
        boot = accept_mod._boot_face(ws)
        self.assertIn("runner.md", boot)                      # boot 指针
        self.assertIn("exp-mono", boot)
        # prompt 消费：inject 段 prompt 含 runner.md 与 kb 指针
        self.assertEqual(self._run(), 3)
        inj_prompt = next(c["prompt"] for c in self.calls
                          if c["step"] == "inject")
        self.assertIn("runner.md", inj_prompt)
        self.assertIn("knowledgebase/build/README.md", inj_prompt)
        self.assertNotIn("SECRET-BUILD-CONTENT", inj_prompt)

    def test_mono_knowledge_face(self):
        """G4：契约登记表/词典指针注入——路径在场、正文不注入、
        缺席省略；inject 与 execute prompt 均消费。"""
        ws = self.fx["ws"]
        em = ws / "exp-mono"
        (em / "contracts.md").write_text("SECRET-CONTRACT-CONTENT\n",
                                         encoding="utf-8")
        out = accept_mod._mono_knowledge_face(ws)
        self.assertIn(str(em / "contracts.md"), out)      # 路径指针
        self.assertIn("必查", out)
        self.assertIn("以树内代码为准", out)               # 快照漂移警示
        self.assertNotIn("SECRET-CONTRACT-CONTENT", out)  # 不注入正文
        self.assertNotIn("mapping-notes", out)            # 缺席省略
        # inject prompt 消费
        self.assertEqual(self._run(), 3)
        inj = next(c["prompt"] for c in self.calls
                   if c["step"] == "inject")
        self.assertIn("contracts.md", inj)
        self.assertNotIn("SECRET-CONTRACT-CONTENT", inj)
        # execute prompt 消费（直接构造）
        exe = accept_mod._execute_prompt(
            ws, {"target_os": "/tmp/t"}, {"driver_home": "home/drv-x"}, {})
        self.assertIn("contracts.md", exe)

    def test_gate_multi_file_fingerprint(self):
        self.assertEqual(self._run(), 3)
        self._answer("exp-accept.inject", "approve")
        st, _ = gate_mod.gate_state(self.fx["ws"], "exp-accept.inject")
        self.assertEqual(st, "approved")
        j = next(iter((self.fx["ws"] / "exp-accept" / "acceptance")
                      .glob("5-*.json")))
        j.write_text(j.read_text(encoding="utf-8")
                     + "\n<!-- tampered -->\n", encoding="utf-8")
        st2, note2 = gate_mod.gate_state(self.fx["ws"],
                                         "exp-accept.inject")
        self.assertEqual(st2, "open")                 # 联合指纹不符 → 重置
        self.assertIn("重新表态", note2)

    def test_combined_sha16(self):
        d = self.fx["ws"]
        a = d / "a.json"
        b = d / "b.check.py"
        a.write_text("{}", encoding="utf-8")
        b.write_text("x=1", encoding="utf-8")
        s1 = gate_mod.combined_sha16([a, b])
        s2 = gate_mod.combined_sha16([b, a])          # 顺序无关
        self.assertEqual(s1, s2)
        self.assertNotEqual(s1, gate_mod.combined_sha16([a]))
        b.write_text("x=2", encoding="utf-8")
        self.assertNotEqual(s1, gate_mod.combined_sha16([a, b]))


class TestAcceptExecute(_Base):
    """执行相位：阶梯 fail-fast / agent 修环 / 终态 / panic / 指纹冻结。"""

    def _exe_agent(self, **kw):
        agent = _make_agent(self.calls, **kw)
        agent.state["ws"] = str(self.fx["ws"])
        agent.state["tree"] = str(self.fx["tree"])
        return agent

    def test_baseline_green_zero_agent(self):
        self._ready_index()
        n0 = len(self.calls)
        with _ctx(self._patches()):
            rc = accept_mod.run_accept(self.fx["ws"], execute=True)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls), n0)       # 零 agent
        led = self._ledger()
        self.assertEqual(led["execute"]["status"], "pass")
        self.assertTrue(led["execute"].get("zero_agent"))
        self.assertTrue((self.fx["ws"] / "exp-accept" /
                         "run-report.md").exists())

    def test_red_fix_green(self):
        agent = self._exe_agent(s5_tree_dep=True)
        self._ready_index(agent)
        self._revert_driver()                        # §5 红
        agent = self._exe_agent(s5_tree_dep=True, exe_fix=True)
        with _ctx(self._patches(agent)):
            rc = accept_mod.run_accept(self.fx["ws"], execute=True)
        self.assertEqual(rc, 0)
        led = self._ledger()
        ex = led["execute"]
        self.assertEqual(ex["status"], "pass")
        self.assertEqual(ex["commits"], ["fxhash"])
        self.assertEqual(ex["sections"]["5"]["status"], "pass")
        exe_calls = [c for c in self.calls if c["step"] == "execute"]
        self.assertEqual(len(exe_calls), 1)
        self.assertIn("首红节", exe_calls[0]["prompt"])

    def test_lying_done_is_unverified(self):
        agent = self._exe_agent(s5_tree_dep=True)
        self._ready_index(agent)
        self._revert_driver()
        agent = self._exe_agent(s5_tree_dep=True, exe_fix=False)
        with _ctx(self._patches(agent)):
            rc = accept_mod.run_accept(self.fx["ws"], execute=True)
        self.assertEqual(rc, 1)
        self.assertEqual(self._ledger()["execute"]["status"], "unverified")
        self.assertTrue(any("§5" in p for p in
                            self._ledger()["execute"]["problems"]))

    def test_criteria_defect_panics(self):
        self._ready_index(self._exe_agent(s5_tree_dep=True))
        self._revert_driver()
        agent = self._exe_agent(
            s5_tree_dep=True,
            exe_blocked=("criteria-defect: §5 判据要求 driver.cx 含 "
                         "probe-ok，但该文件设计期即为空脚手架——判据 "
                         "定义 vs 实测：期望 log_contains=probe-ok，"
                         "实测 grep 无输出且无补标记的合法途径。"))
        with _ctx(self._patches(agent)):
            rc = accept_mod.run_accept(self.fx["ws"], execute=True)
        self.assertEqual(rc, 1)
        self.assertEqual(self._ledger()["execute"]["status"], "panic")
        self.assertEqual(
            self._ledger()["execute"].get("panic_kind"), "standard")
        panic_md = self.fx["ws"] / "exp-accept" / "execute-panic.md"
        self.assertTrue(panic_md.exists())
        self.assertIn("人工选项", panic_md.read_text(encoding="utf-8"))

    def test_platform_gap_panics(self):
        self._ready_index(self._exe_agent(s5_tree_dep=True))
        self._revert_driver()
        agent = self._exe_agent(
            s5_tree_dep=True,
            exe_blocked=("platform-gap: §5 需要目标 OS 侧的总线注册"
                         "设施（断点位于 kernel/bus/registry，在"
                         " driver_home 与各节 paths 之外）——修环内无"
                         "合法闭合途径，建议平台侧补注册接口后重跑。"))
        with _ctx(self._patches(agent)):
            rc = accept_mod.run_accept(self.fx["ws"], execute=True)
        self.assertEqual(rc, 1)
        self.assertEqual(self._ledger()["execute"]["status"], "panic")
        self.assertEqual(
            self._ledger()["execute"].get("panic_kind"), "platform")
        panic_md = self.fx["ws"] / "exp-accept" / "execute-panic.md"
        text = panic_md.read_text(encoding="utf-8")
        self.assertIn("平台缺口", text)
        self.assertIn("platform-gap", text)
        self.assertIn("扩大允许改动范围", text)   # 处置三选在场

    def test_plain_blocked_parks(self):
        self._ready_index(self._exe_agent(s5_tree_dep=True))
        self._revert_driver()
        agent = self._exe_agent(s5_tree_dep=True,
                                exe_blocked="平台缺口：目标 OS 无对应设施")
        with _ctx(self._patches(agent)):
            rc = accept_mod.run_accept(self.fx["ws"], execute=True)
        self.assertEqual(rc, 1)
        self.assertEqual(self._ledger()["execute"]["status"], "parked")

    def test_budget_exhausted_interrupted(self):
        self._ready_index(self._exe_agent(s5_tree_dep=True))
        self._revert_driver()
        agent = self._exe_agent(s5_tree_dep=True, exe_seq="budget-exhausted")
        with _ctx(self._patches(agent)):
            rc = accept_mod.run_accept(self.fx["ws"], execute=True)
        self.assertEqual(rc, 1)
        ex = self._ledger()["execute"]
        self.assertEqual(ex["status"], "interrupted")
        self.assertEqual(ex["session_id"], "ses-exe")

    def test_fingerprint_drift_rc2(self):
        self._ready_index()
        j = next(iter((self.fx["ws"] / "exp-accept" / "acceptance")
                      .glob("5-*.json")))
        j.write_text(j.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with _ctx(self._patches()):
            rc = accept_mod.run_accept(self.fx["ws"], execute=True)
        self.assertEqual(rc, 2)

    def test_execute_without_index_rc2(self):
        with _ctx(self._patches()):
            rc = accept_mod.run_accept(self.fx["ws"], execute=True)
        self.assertEqual(rc, 2)
        self.assertFalse([c for c in self.calls
                          if c["step"] == "execute"])

    def test_exec_static_units(self):
        self._ready_index(self._exe_agent(s5_tree_dep=True))
        self._revert_driver()
        ws, tree = self.fx["ws"], self.fx["tree"]
        ledger = self._ledger()
        static = accept_mod._make_exec_static(
            ws, ws / "exp-accept", tree, "home/drv-x", ledger,
            accept_mod._mono._git_status(tree))
        with _ctx([]):
            ok, out = static["fn"]()                # §5 红 → 回灌结果
        self.assertTrue(ok)
        self.assertIn("§5", out)
        self.assertIn("未跑", out)                   # fail-fast：§6-7 skipped
        led2 = json.loads((ws / "exp-accept" / "ledger.json")
                          .read_text(encoding="utf-8"))
        self.assertEqual(led2["execute"]["sections"]["6"]["status"],
                         "skipped")
        with _ctx([]):
            ok2, out2 = static["fn"]()              # 仍红（幂等可重跑）
        self.assertTrue(ok2)
        (tree / "elsewhere.txt").write_text("x", encoding="utf-8")
        with _ctx([]):
            ok3, out3 = static["fn"]()              # 越界 → False
        self.assertFalse(ok3)
        self.assertIn("越出白名单", out3)
        (tree / "elsewhere.txt").unlink()
        j = next(iter((ws / "exp-accept" / "acceptance").glob("5-*.json")))
        j.write_text(j.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with _ctx([]):
            ok4, out4 = static["fn"]()              # 指纹漂移 → False
        self.assertFalse(ok4)
        self.assertIn("指纹不符", out4)


if __name__ == "__main__":
    unittest.main()
