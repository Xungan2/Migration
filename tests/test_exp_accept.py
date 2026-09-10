"""test_exp_accept.py — exp-accept 子命令的功能性测试（全 mock，零真实 agent）。

覆盖：
  1. 草案相位（文件即信号）：合法草案 → 冻结合并 → 审批关口 rc 3
  2. 草案回炉：schema 微反馈（同轮）与证据核查回炉（跨轮）
  3. 草案耗尽 → 失败关口 rc 3
  4. 放行 → execute 全绿：4 层判定 + 差分（裸 miss/注入 hit）+ 收据
     fill_sha256 + 脚手架落树接线 + report
  5. 红项 → 修环：agent 修复（真 git commit）→ 复验绿 → rc 0
  6. 修环 stalled → 泊车 rc 1；脚手架漂移被还原
  7. 指纹防篡改：放行后改 acceptance.json → rc 2
  8. 断点续跑：全绿后重跑零重执行；infra 瞬态重试 1 次
  9. 前置守卫 rc 2 各分支
  10. 小件：resolve_expect / judge 极性 / validate_draft 错误面 /
      cite 窗口 / 占位符替换 / 零硬编码 grep

fixture 全虚构：driver-x / fake-tree / boot 日志 fakeboot.log /
标记 BOOT-GREEN、WL-RAN——不承载任何真实平台事实。
"""
import hashlib
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


SHA512Z = hashlib.sha256(b"\x00" * 512).hexdigest()
SHA256Z = hashlib.sha256(b"\x00" * 256).hexdigest()


def _ev(session, text):
    lines = [
        {"type": "step_start", "sessionID": session,
         "part": {"type": "step-start"}},
        {"type": "text", "sessionID": session,
         "part": {"type": "text", "text": text}},
        {"type": "step_finish", "sessionID": session,
         "part": {"type": "step-finish", "reason": "stop"}},
    ]
    return "\n".join(json.dumps(x) for x in lines) + "\n"


def _anchor_tree(tree: Path) -> None:
    """驱动目录 + 带精确行号的锚点文件。"""
    src = tree / "home" / "drv-x" / "src"
    src.mkdir(parents=True, exist_ok=True)
    lines = ["// filler"] * 4
    lines.append('emit!("waiting-device-marker");')       # 5
    lines += ["// filler", "// filler"]
    lines.append('emit!("WARN-bad-target");')             # 8
    lines += ["// filler", "// filler"]
    lines.append('emit!("(devx) ready-marker");')         # 11
    (src / "insta.rs").write_text("\n".join(lines) + "\n",
                                  encoding="utf-8")


def _mk_fixture(tmp: Path) -> dict:
    linux = tmp / "linux-x"
    linux.mkdir()
    (linux / "drv.c").write_text("int fictitious(void) {return 0;}\n",
                                 encoding="utf-8")
    tree = tmp / "fake-tree"
    _anchor_tree(tree)
    subprocess.run(["git", "init", "-q"], cwd=tree, check=True,
                   capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=tree, check=True,
                   capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                    "commit", "-q", "-m", "init"], cwd=tree, check=True,
                   capture_output=True)
    ws = tmp / "ws"
    (ws / "P2" / "reports").mkdir(parents=True)
    (ws / "exp-mono").mkdir(parents=True)
    (ws / "project.json").write_text(json.dumps({
        "name": "ws-acc-fx", "linux_driver": str(linux),
        "target_os": str(tree)}), encoding="utf-8")
    (ws / "runner.json").write_text(json.dumps({
        "build": {"cmd": "echo BUILD-OK", "timeout_full_sec": 30,
                  "success_pattern": "BUILD-OK"},
        "boot": {"cmd": "echo BAREBOOT-CALLED", "timeout_sec": 30,
                 "log_file": "fakeboot.log", "log_is_stdout": False,
                 "success_pattern": "BOOT-GREEN",
                 "panic_pattern": "BOOT-DEAD"},
        "inject_device": {
            "mechanism": "cmd", "env": None,
            "cmd_suffix": "; echo INJECTSIDE <DEVICE_ARGS>",
            "example_args": {"block": "devx,,,rw, 0 8 zerotgt"},
            "driver_success_pattern": "DRIVER-SAW-DEVICE",
            "driver_fail_pattern": None},
        "unit_test": {"cmd": "echo UT-ALL-OK", "timeout_sec": 30,
                      "success_pattern": "UT-ALL-OK",
                      "fail_pattern": "UT-BAD"}}), encoding="utf-8")
    (ws / "P2" / "reports" / "scaffold_manifest.json").write_text(
        json.dumps({"driver": "driver-x", "driver_home": "home/drv-x",
                    "source_ext": ".cx", "commit_paths": ["top.txt"]}),
        encoding="utf-8")
    (ws / "exp-mono" / "ledger.json").write_text(json.dumps(
        {"modules": {"fx-a": {"status": "pass"}}}), encoding="utf-8")
    return {"tmp": tmp, "ws": ws, "tree": tree, "linux": linux}


def _valid_draft() -> dict:
    return {
        "boots": [
            {"id": "B_e2e",
             "cmd": "echo E2EBOOT {PORTER_TARGET_OS_ROOT}",
             "log_file": "fakeboot.log", "timeout_sec": 30,
             "success_pattern": "BOOT-GREEN"},
            {"id": "B_neg", "cmd": "echo NEGBOOT <DEVICE_ARGS>",
             "args": "devx,,,rw, 0 8 nosuch",
             "log_file": "fakeboot.log", "timeout_sec": 30,
             "success_pattern": "BOOT-GREEN"}],
        "scaffold": {
            "files": [{"path": "testroot/wl.sh",
                       "content": "#!/bin/sh\necho WL-RAN\n"
                                  "H=$(hash-of-read)\n"
                                  "echo WL-SHA256=$H\n"}],
            "wiring_cmd": "echo wiring-done {PORTER_TARGET_OS_ROOT}",
            "wiring_paths": ["testroot/registry.lst"]},
        "criteria": [
            {"id": "acc.wait.hit", "layer": "L3", "boot": "B_inject",
             "kind": "log", "polarity": "hit",
             "expr": "waiting-device-marker",
             "cite": {"tree": {"path": "home/drv-x/src/insta.rs",
                               "line": 5,
                               "quote": "waiting-device-marker"}},
             "evidence": "实例化链打印（虚构锚点）"},
            {"id": "acc.wait.miss", "layer": "L3", "boot": "B_bare",
             "kind": "log", "polarity": "miss",
             "expr": "waiting-device-marker",
             "cite": {"tree": {"path": "home/drv-x/src/insta.rs",
                               "line": 5,
                               "quote": "waiting-device-marker"}},
             "evidence": "差分：无载荷时该锚点必缺席"},
            {"id": "acc.proof", "layer": "L4", "boot": "B_e2e",
             "kind": "log", "polarity": "hit", "expr": "WL-RAN",
             "cite": {"scaffold": {"file": "testroot/wl.sh",
                                   "contains": "WL-RAN"}},
             "evidence": "工作负载运行证明行"},
            {"id": "acc.hash", "layer": "L4", "boot": "B_e2e",
             "kind": "receipt", "polarity": "hit",
             "expr": "WL-SHA256=([0-9a-f]{64})",
             "expect": {"fill_sha256": {"byte": 0, "length": 512}},
             "cite": {"scaffold": {"file": "testroot/wl.sh",
                                   "contains": "WL-SHA256="}},
             "evidence": "读内容摘要须等于 512 零字节的 sha256"},
            {"id": "acc.neg.warn", "layer": "L4", "boot": "B_neg",
             "kind": "log", "polarity": "hit", "expr": "WARN-bad-target",
             "cite": {"tree": {"path": "home/drv-x/src/insta.rs",
                               "line": 8, "quote": "WARN-bad-target"}},
             "evidence": "坏目标警告"},
            {"id": "acc.neg.noready", "layer": "L4", "boot": "B_neg",
             "kind": "log", "polarity": "miss", "expr": "ready-marker",
             "cite": {"tree": {"path": "home/drv-x/src/insta.rs",
                               "line": 11, "quote": "ready-marker"}},
             "evidence": "失败路径不得宣告就绪"},
            {"id": "acc.neg.noreceipt", "layer": "L4", "boot": "B_neg",
             "kind": "log", "polarity": "miss", "expr": "WL-SHA256=",
             "cite": {"scaffold": {"file": "testroot/wl.sh",
                                   "contains": "WL-SHA256="}},
             "evidence": "负向不得出现正向收据"}]}


class _DraftRunner:
    """agent._opencode_json_runner 的 mock：按序写方案文件并回事件流。

    payloads = [(draft_or_None, text), ...] 按调用次序弹出；None =
    本次不写文件（触发"文件不可读"微反馈）。
    """

    def __init__(self, ws: Path, payloads):
        self.ws = ws
        self.payloads = list(payloads)
        self.calls = []

    def __call__(self, message, workdir, log_stem, timeout_sec=0,
                 session_id=None, model=None, task=None):
        self.calls.append({"message": message, "session_id": session_id})
        stem = Path(f"{log_stem}")
        exp_dir = stem.parents[1]
        exp_dir.mkdir(parents=True, exist_ok=True)
        (exp_dir / "logs").mkdir(exist_ok=True)
        draft, text = (None, "busy") if not self.payloads \
            else self.payloads.pop(0)
        if draft is not None:
            (exp_dir / "acceptance.draft.json").write_text(
                json.dumps(draft, ensure_ascii=False, indent=1) + "\n",
                encoding="utf-8")
        stem.write_text("mock log\n", encoding="utf-8")
        return 0, _ev("ses_acc", text or "written")


class _ProbeRun:
    """probe._run 的 mock：按命令标记路由，写捕获日志与启动日志。

    LOGS 可变字典供修环测试中途改写（模拟 agent 修复后的行为变化）。
    """

    def __init__(self, tree: Path):
        self.tree = tree
        self.calls = []
        self.LOGS = {
            "bare": "BOOT-GREEN\n",
            "inject": "BOOT-GREEN\nDRIVER-SAW-DEVICE\n"
                      "waiting-device-marker\n(devx) ready-marker\n",
            "e2e": f"BOOT-GREEN\nWL-RAN\nWL-SHA256={SHA512Z}\n",
            "neg": "BOOT-GREEN\nWARN-bad-target\n",
        }
        self.inject_flaky = False       # 首次注入调用瞬态失败

    def __call__(self, cmd, cwd, env, timeout_sec, log_path):
        self.calls.append(cmd)
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(f"ran: {cmd}\n", encoding="utf-8")
        if "BUILD-OK" in cmd:
            return 0, "BUILD-OK\n"
        if "UT-ALL-OK" in cmd:
            return 0, "UT-ALL-OK\n"
        if "wiring-done" in cmd:
            return 0, "wiring ok\n"
        if "INJECTSIDE" in cmd:
            if self.inject_flaky:
                self.inject_flaky = False
                return 2, "transient-noise\n"     # 不写 fakeboot.log
            content = self.LOGS["inject"]
        elif "E2EBOOT" in cmd:
            content = self.LOGS["e2e"]
        elif "NEGBOOT" in cmd:
            content = self.LOGS["neg"]
        else:
            content = self.LOGS["bare"]
        (self.tree / "fakeboot.log").write_text(content,
                                                encoding="utf-8")
        return 0, "BOOT-CALLED\n"


class _SeqFix:
    """run_agent_seq 的 mock：先干活（可改 LOGS/树）再跑静态段。"""

    def __init__(self, work=None, verdict="stalled"):
        self.work = work
        self.verdict = verdict
        self.static_results = []

    def __call__(self, prompt, workdir, log_stem, static=None,
                 gen_schema=None, final_static=False, agent_budget_sec=0,
                 task=None, model=None, resume_session=None):
        if self.work:
            self.work()
        if static is not None:
            ok, out = static["fn"]()
            self.static_results.append({"ok": ok, "out": out})
            if not ok:
                return {"status": self.verdict, "session_id": "ses_fix",
                        "fallback": False, "rounds": [{"seg": 1}],
                        "parsed": None, "total_agent_sec": 0.2}
        return {"status": "done", "session_id": "ses_fix",
                "fallback": False, "rounds": [{"seg": 1}, {"seg": 2}],
                "parsed": {"status": "done", "circuit": "migration",
                           "action": "fix-code", "evidence": [],
                           "summary": "修正读路径填充"},
                "total_agent_sec": 0.3}


def _approve(ws: Path):
    with (ws / "answers.md").open("a", encoding="utf-8") as fh:
        fh.write("## @exp-accept.plan\nverdict: approve\n")


class TestExpAcceptUnits(unittest.TestCase):

    def test_resolve_expect(self):
        self.assertEqual(accept_mod.resolve_expect(
            {"literal": "abc"}), "abc")
        self.assertEqual(accept_mod.resolve_expect(
            {"fill_sha256": {"byte": 0, "length": 512}}), SHA512Z)
        self.assertIsNone(accept_mod.resolve_expect(None))
        self.assertIsNone(accept_mod.resolve_expect(
            {"fill_sha256": {"byte": 999, "length": 8}}))
        self.assertIsNone(accept_mod.resolve_expect({"fill_sha256":
                                                     {"byte": 0}}))

    def test_judge_polarity_and_receipt(self):
        results = {"B_x": {"rc": 0, "log": "AA marker-here BB "
                          f"WL-SHA256={SHA512Z}", "log_state": "file",
                          "green": True, "snapshot": "x"}}
        crit = [
            {"id": "c1", "layer": "L3", "boot": "B_x", "kind": "log",
             "polarity": "hit", "expr": "marker-here", "origin":
             "proposed"},
            {"id": "c2", "layer": "L3", "boot": "B_x", "kind": "log",
             "polarity": "miss", "expr": "absent-thing", "origin":
             "proposed"},
            {"id": "c3", "layer": "L4", "boot": "B_x", "kind": "receipt",
             "polarity": "hit", "expr": "WL-SHA256=([0-9a-f]{64})",
             "expect": {"fill_sha256": {"byte": 0, "length": 512}},
             "origin": "proposed"},
            {"id": "c4", "layer": "L4", "boot": "B_x", "kind": "receipt",
             "polarity": "hit", "expr": "WL-SHA256=([0-9a-f]{64})",
             "expect": {"literal": "deadbeef"}, "origin": "proposed"},
            {"id": "c5", "layer": "L1", "boot": "B_missing", "kind":
             "rc", "polarity": "hit", "origin": "frozen"}]
        js = {j["id"]: j for j in accept_mod.judge_criteria(crit,
                                                            results)}
        self.assertTrue(js["c1"]["ok"])
        self.assertTrue(js["c2"]["ok"])
        self.assertTrue(js["c3"]["ok"])
        self.assertFalse(js["c4"]["ok"])
        self.assertIsNone(js["c5"]["ok"])

    def test_validate_draft_errors(self):
        runner = {"inject_device": {"example_args":
                                    {"block": "x", "net": "y"}}}
        base = _valid_draft()
        errs = accept_mod.validate_draft(base, {"inject_device":
                                                {"example_args":
                                                 {"block": "x"}}})
        self.assertEqual(errs, [])
        d = json.loads(json.dumps(base))
        d["criteria"][0]["layer"] = "L1"
        self.assertIn("L3|L4", ";".join(accept_mod.validate_draft(d,
                                                                  runner)))
        d = json.loads(json.dumps(base))
        del d["criteria"][0]["cite"]
        self.assertIn("cite", ";".join(accept_mod.validate_draft(d,
                                                                 runner)))
        d = json.loads(json.dumps(base))
        d["criteria"][3].pop("expect")
        self.assertIn("expect", ";".join(accept_mod.validate_draft(d,
                                                                   runner)))
        d = json.loads(json.dumps(base))
        d["boots"][0].pop("log_file")
        self.assertIn("日志定位", ";".join(accept_mod.validate_draft(d,
                                                                    runner)))
        d = json.loads(json.dumps(base))
        del d["scaffold"]["wiring_paths"]
        self.assertIn("wiring_paths", ";".join(
            accept_mod.validate_draft(d, runner)))
        d = json.loads(json.dumps(base))
        d["criteria"][1]["id"] = d["criteria"][0]["id"]
        self.assertIn("重复", ";".join(accept_mod.validate_draft(d,
                                                                 runner)))

    def test_check_cites_window(self):
        tmp = Path(tempfile.mkdtemp(prefix="acc_cite_"))
        _anchor_tree(tmp)
        good = {"criteria": [
            {"id": "c", "cite": {"tree": {
                "path": "home/drv-x/src/insta.rs", "line": 5,
                "quote": "waiting-device-marker"}}}]}
        self.assertEqual(accept_mod.check_cites(tmp, good), [])
        bad_line = {"criteria": [
            {"id": "c", "cite": {"tree": {
                "path": "home/drv-x/src/insta.rs", "line": 30,
                "quote": "waiting-device-marker"}}}]}
        self.assertIn("cite 未命中",
                      accept_mod.check_cites(tmp, bad_line)[0])
        shutil.rmtree(tmp)

    def test_dry_assemble_missing_args(self):
        tmp = Path(tempfile.mkdtemp(prefix="acc_asm_"))
        draft = {"boots": [
            {"id": "B_x", "cmd": "run <DEVICE_ARGS>",
             "log_file": "l.log"}],
            "criteria": [
                {"id": "c", "boot": "B_x", "expr": "e"}]}
        errs = accept_mod.dry_assemble(draft, {"inject_device":
                                               {"example_args": {}}},
                                        tmp, "home/drv-x")
        self.assertTrue(any("DEVICE_ARGS" in e for e in errs))
        errs = accept_mod.dry_assemble(
            draft, {"inject_device": {"example_args":
                                      {"block": "payload"}}},
            tmp, "home/drv-x")
        self.assertFalse(any("DEVICE_ARGS" in e for e in errs))
        shutil.rmtree(tmp)

    def test_subst_shell_form_preserved(self):
        cmd = accept_mod._subst(
            "run ${PORTER_TARGET_OS_ROOT}/x {PORTER_TARGET_OS_ROOT} "
            "{PORTER_DRIVER_HOME} <DEVICE_ARGS>",
            Path("/t"), "home/d", "PAY")
        self.assertEqual(cmd,
                         "run ${PORTER_TARGET_OS_ROOT}/x /t home/d PAY")

    def test_no_hardcode(self):
        root = Path(__file__).resolve().parent.parent
        files = [root / "porter" / "exp" / "accept.py",
                 root / "skills" / "EXP-accept-draft.md",
                 root / "skills" / "EXP-accept-fix.md",
                 Path(__file__)]
        # token 拼接构造，避免本文件自身命中
        pat = ("aster" + "inas", "dm-" + "zero", "dm" + "zero",
               "dm_" + "zero", "e" + "1000", "spi-" + "nor",
               "spi" + "nor", "q" + "emu")
        for f in files:
            text = f.read_text(encoding="utf-8").lower()
            for token in pat:
                self.assertNotIn(token, text,
                                 f"{f.name} 含硬编码 token")


class TestExpAcceptFlow(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="exp_accept_"))
        self.fx = _mk_fixture(self.tmp)
        self.probe = _ProbeRun(self.fx["tree"])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _ledger(self):
        return json.loads((self.fx["ws"] / "exp-accept" /
                           "ledger.json").read_text(encoding="utf-8"))

    def _draft(self, payloads) -> _DraftRunner:
        runner = _DraftRunner(self.fx["ws"], payloads)
        with mock.patch.object(accept_mod.agent,
                               "_opencode_json_runner", runner):
            rc = accept_mod.run_exp_accept(self.fx["ws"], mode="draft")
        self.rc_draft = rc
        return runner

    def _execute(self, seq=None):
        with mock.patch.object(accept_mod.probe_mod, "_run", self.probe), \
                mock.patch("porter.common.vcs.commit_target",
                           return_value=["cafe1234"]), \
                (mock.patch.object(accept_mod.agent, "run_agent_seq", seq)
                 if seq is not None else _null()):
            return accept_mod.run_exp_accept(self.fx["ws"])

    def test_draft_merge_and_gate(self):
        self._draft([(_valid_draft(), "done")])
        self.assertEqual(self.rc_draft, 3)
        acc = json.loads((self.fx["ws"] / "exp-accept" /
                          "acceptance.json").read_text(encoding="utf-8"))
        ids = [c["id"] for c in acc["criteria"]]
        self.assertIn("L1.build.rc", ids)          # 冻结判据已合并
        self.assertIn("acc.wait.hit", ids)         # 提案判据在场
        self.assertIn("B_build", [b["id"] for b in acc["boots"]])
        self.assertTrue((self.fx["ws"] / "exp-accept" /
                         "review.md").exists())
        gates_doc = json.loads((self.fx["ws"] / "gates.json")
                               .read_text(encoding="utf-8"))
        self.assertIn("exp-accept.plan",
                      [g["id"] for g in gates_doc["gates"]])

    def test_draft_pending_approval_rc3(self):
        self._draft([(_valid_draft(), "done")])
        rc = accept_mod.run_exp_accept(self.fx["ws"])
        self.assertEqual(rc, 3)

    def test_draft_schema_then_evidence_rework(self):
        bad_schema = _valid_draft()
        bad_schema["criteria"][0].pop("cite")
        wrong_line = _valid_draft()
        wrong_line["criteria"][0]["cite"]["tree"]["line"] = 40
        runner = self._draft([(bad_schema, "w"), (wrong_line, "w"),
                              (_valid_draft(), "done")])
        self.assertEqual(self.rc_draft, 3)
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(self._ledger()["draft"]["rounds"], 2)

    def test_draft_exhaustion_gate(self):
        bad = _valid_draft()
        bad["criteria"] = []
        self._draft([(bad, "w")] * 12)
        self.assertEqual(self.rc_draft, 3)
        gates_doc = json.loads((self.fx["ws"] / "gates.json")
                               .read_text(encoding="utf-8"))
        self.assertIn("exp-accept.draft.fail",
                      [g["id"] for g in gates_doc["gates"]])

    def test_full_green_flow(self):
        self._draft([(_valid_draft(), "done")])
        _approve(self.fx["ws"])
        rc = self._execute()
        self.assertEqual(rc, 0)
        led = self._ledger()
        self.assertEqual(led["verdict"], "all-green")
        report = (self.fx["ws"] / "exp-accept" / "report.md").read_text(
            encoding="utf-8")
        self.assertIn("全绿", report)
        self.assertIn("acc.hash", report)
        # 脚手架已落树，接线执行过
        self.assertTrue((self.fx["tree"] / "testroot" / "wl.sh").exists())
        self.assertTrue(any("wiring-done" in c for c in self.probe.calls))
        # 差分双判据均绿
        crit = led["criteria"]
        self.assertTrue(crit["acc.wait.hit"]["ok"])
        self.assertTrue(crit["acc.wait.miss"]["ok"])

    def test_resume_skips_green(self):
        self._draft([(_valid_draft(), "done")])
        _approve(self.fx["ws"])
        self.assertEqual(self._execute(), 0)
        n = len(self.probe.calls)
        rc = self._execute()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.probe.calls), n)

    def test_infra_retry_recovers(self):
        self._draft([(_valid_draft(), "done")])
        _approve(self.fx["ws"])
        self.probe.inject_flaky = True
        rc = self._execute()
        self.assertEqual(rc, 0)
        led = self._ledger()
        self.assertEqual(led["units"]["B_inject"]["attempts"], 2)

    def test_red_diagnose_solved(self):
        self._draft([(_valid_draft(), "done")])
        _approve(self.fx["ws"])
        self.probe.LOGS["e2e"] = f"BOOT-GREEN\nWL-RAN\nWL-SHA256={SHA256Z}\n"

        def work():                       # agent 修码：改行为 + 真提交
            self.probe.LOGS["e2e"] = \
                f"BOOT-GREEN\nWL-RAN\nWL-SHA256={SHA512Z}\n"
            p = self.fx["tree"] / "home" / "drv-x" / "fix.note"
            p.write_text("fix\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(self.fx["tree"]), "add",
                            "-A"], capture_output=True)
            subprocess.run(["git", "-C", str(self.fx["tree"]), "-c",
                            "user.name=t", "-c", "user.email=t@t",
                            "commit", "-q", "-m", "fix"],
                           capture_output=True)

        seq = _SeqFix(work=work)
        with mock.patch.object(accept_mod.probe_mod, "_run", self.probe), \
                mock.patch("porter.common.vcs.commit_target",
                           return_value=["cafe1234"]) as _c, \
                mock.patch.object(accept_mod.agent, "run_agent_seq", seq):
            rc = accept_mod.run_exp_accept(self.fx["ws"])
        self.assertEqual(rc, 0)
        self.assertTrue(any("exp-accept(fix)" in str(ca)
                            for ca in _c.call_args_list))
        self.assertEqual(self._ledger()["verdict"], "all-green")

    def test_red_diagnose_parked(self):
        self._draft([(_valid_draft(), "done")])
        _approve(self.fx["ws"])
        self.probe.LOGS["e2e"] = f"BOOT-GREEN\nWL-RAN\nWL-SHA256={SHA256Z}\n"
        seq = _SeqFix(work=None)          # 修不动 → stalled
        with mock.patch.object(accept_mod.probe_mod, "_run", self.probe), \
                mock.patch("porter.common.vcs.commit_target",
                           return_value=["cafe1234"]), \
                mock.patch.object(accept_mod.agent, "run_agent_seq", seq):
            rc = accept_mod.run_exp_accept(self.fx["ws"])
        self.assertEqual(rc, 1)
        parking = (self.fx["ws"] / "exp-accept" / "parking.md").read_text(
            encoding="utf-8")
        self.assertIn("修环未解", parking)
        self.assertIn("ses_fix", parking)          # 续修 session 落泊车面
        led = self._ledger()
        self.assertEqual(len(led["diagnose"]), 1)  # 修环历史已落盘
        self.assertEqual(led["diagnose"][0]["session_id"], "ses_fix")
        self.assertEqual(led["diagnose"][0]["status"], "stalled")

    def test_diagnose_scaffold_drift_restored(self):
        self._draft([(_valid_draft(), "done")])
        _approve(self.fx["ws"])
        self.probe.LOGS["e2e"] = f"BOOT-GREEN\nWL-RAN\nWL-SHA256={SHA256Z}\n"

        def work():                       # agent 误改脚手架
            (self.fx["tree"] / "testroot" / "wl.sh").write_text(
                "tampered\n", encoding="utf-8")

        seq = _SeqFix(work=work)
        with mock.patch.object(accept_mod.probe_mod, "_run", self.probe), \
                mock.patch("porter.common.vcs.commit_target",
                           return_value=["cafe1234"]), \
                mock.patch.object(accept_mod.agent, "run_agent_seq", seq):
            rc = accept_mod.run_exp_accept(self.fx["ws"])
        self.assertEqual(rc, 1)
        content = (self.fx["tree"] / "testroot" / "wl.sh").read_text(
            encoding="utf-8")
        self.assertIn("WL-RAN", content)
        self.assertNotIn("tampered", content)
        self.assertFalse(seq.static_results[0]["ok"])
        self.assertIn("脚手架漂移", seq.static_results[0]["out"])

    def test_fingerprint_tamper(self):
        self._draft([(_valid_draft(), "done")])
        _approve(self.fx["ws"])
        # 作答后、放行处理前篡改 → 关口重置待答 rc 3
        acc_path = self.fx["ws"] / "exp-accept" / "acceptance.json"
        doc = json.loads(acc_path.read_text(encoding="utf-8"))
        doc["criteria"][-1]["expr"] = "tampered-expr"
        acc_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        rc = self._execute()
        self.assertEqual(rc, 3)
        # 放行完成后再篡改 → execute 指纹核验 rc 2
        _approve(self.fx["ws"])
        with mock.patch.object(accept_mod.probe_mod, "_run", self.probe), \
                mock.patch("porter.common.vcs.commit_target",
                           return_value=["cafe1234"]):
            rc2 = accept_mod.run_exp_accept(self.fx["ws"])
        self.assertEqual(rc2, 0)          # 此轮放行绑定当前内容
        doc = json.loads(acc_path.read_text(encoding="utf-8"))
        doc["criteria"][-1]["expr"] = "tampered-again"
        acc_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        rc3 = self._execute()
        self.assertEqual(rc3, 2)

    def test_preconditions(self):
        rc = accept_mod.run_exp_accept(self.fx["ws"])
        self.assertEqual(rc, 2)           # 无 acceptance → 提示 draft
        (self.fx["ws"] / "exp-mono" / "ledger.json").write_text(
            json.dumps({"modules": {"fx-a": {"status": "fail"}}}),
            encoding="utf-8")
        self.assertEqual(accept_mod.run_exp_accept(self.fx["ws"],
                                                   mode="draft"), 2)
        (self.fx["ws"] / "exp-mono" / "ledger.json").unlink()
        self.assertEqual(accept_mod.run_exp_accept(self.fx["ws"]), 2)


class _null:
    """占位上下文（三段 patch 形态统一用）。"""

    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


if __name__ == "__main__":
    unittest.main()
