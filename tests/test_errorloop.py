"""porter/loop/errorloop.py 单元测试（错误处理求解循环核心）。

无真实 agent（monkeypatch canned verdict）/ 无网络。覆盖：
L1  签名规范化：行号/路径/时间戳/ANSI 碎改动不翻转；内容变则翻转
L2  fix-runner：runner_patch 合入 runner.json
L3  fix-criteria：无证据被拒；有证据修正 + 决策债登记（CP 审计）
L4  park：platform_patches + defect 泊车，终态 parked（免报告）
L5  solved：单轮修好 → 状态 solved + 案例回流候选账
L6  早退：同签名连发 ×2 → 2 轮即停（第 3 轮不烧）
L7  unsolved：3 轮异签名 → 状态 unsolved + 升级报告落盘 + 事件族
L8  PORTER_NO_AGENT → no-agent（零 agent 调用，报告仍出）
L9  熔断关 → bypass
L10 rehang：deferred 改挂，终态 rehung
L11 prompt 形态：轮1 注入知识目录；轮2 不重注（目录提示+上轮总结+
    已检索不匹配清单）
L12 坏轮恢复：R1 输出不可解析 → R2 修好 → solved
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.loop import errorloop as EL


def setUpModule():
    global _SD_OLD
    os.environ.pop("PORTER_NO_AGENT", None)   # 本模块全 mock
    # config 熔断在 S5 前仍为 false——沿用 §15 测试惯例强制开
    _SD_OLD = os.environ.get("PORTER_SELF_DIAGNOSIS")
    os.environ["PORTER_SELF_DIAGNOSIS"] = "1"


def tearDownModule():
    if _SD_OLD is None:
        os.environ.pop("PORTER_SELF_DIAGNOSIS", None)
    else:
        os.environ["PORTER_SELF_DIAGNOSIS"] = _SD_OLD


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def _v(action, **kw):
    base = {"status": "done", "circuit": kw.pop("circuit", "migration"),
            "action": action, "evidence": [], "summary": kw.pop(
                "summary", "测试判定"), "confidence": 0.8}
    base.update(kw)
    return base


def _canned(verdict):
    return (0, "```json\n" + json.dumps(verdict, ensure_ascii=False)
            + "\n```")


class SolveLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porter_el_t_"))
        self.ws = self.tmp / "ws"
        (self.ws / "P3" / "modA" / "reports").mkdir(parents=True)
        (self.ws / "P6" / "reports").mkdir(parents=True)
        (self.ws / "runner.json").write_text(json.dumps(
            {"build": {"cmd": "make kernel"},
             "unit_test": {"cmd": "cargo osdk test"}}), encoding="utf-8")
        (self.ws / "P3" / "modA" / "reports" / "criteria.json").write_text(
            json.dumps({"criteria": [
                {"id": "modA.c3", "layer": "L3", "kind": "log_pattern",
                 "expr": r"\beth0\b up", "deferred_by": None}]}),
            encoding="utf-8")
        self._kb_swap()

    def _kb_swap(self):
        """KB 根重定向（隔离 kb_face/candidates 遥测不污染真仓）。"""
        from porter.bootstrap import kb as _kb
        self._kb = _kb
        self._old = (_kb.KB_ROOT, _kb.BASE_DIR, _kb.TEMP_DIR)
        _kb.KB_ROOT = self.tmp / "knowledge"
        _kb.BASE_DIR = _kb.KB_ROOT / "base"
        _kb.TEMP_DIR = _kb.KB_ROOT / "temp"
        _kb.BASE_DIR.mkdir(parents=True)
        _kb.TEMP_DIR.mkdir(parents=True)
        (self.ws / "project.json").write_text(json.dumps(
            {"target_os": str(self.tmp), "linux_driver": "/drv/e1000",
             "kb_dir": "corpus"}), encoding="utf-8")
        cor = _kb.KB_ROOT / "corpus" / "failures"
        cor.mkdir(parents=True)
        (cor / "compile-fail.md").write_text("# x", encoding="utf-8")
        _kb.save_index(cor, [{"file": "compile-fail.md",
                              "desc": "编译失败处置", "hits": 0}])

    def tearDown(self):
        kb = self._kb
        kb.KB_ROOT, kb.BASE_DIR, kb.TEMP_DIR = self._old
        import shutil
        shutil.rmtree(self.tmp)

    def _failure(self, **kw):
        base = {"source": "p5", "subject": "modA.c3", "module": "modA",
                "kind": "log_pattern", "expr": r"\beth0\b up",
                "detail": "hits=0", "boot_log": "eth0 up …",
                "_workdir": self.tmp}
        base.update(kw)
        return base

    def _verify_script(self, results):
        """results: [(ok, evidence_dict), ...] 逐次弹出。"""
        seq = list(results)

        def verify():
            return seq.pop(0) if seq else (False, {"detail": "hits=0"})
        return verify

    # ---------- L1 签名 ----------

    def test_l1_signature(self):
        a = EL.failure_signature("m.c1", "rc=1 pattern=MISS",
                                 "/root/os/src/x.rs:12:5 error[E0432]\nboom")
        b = EL.failure_signature("m.c1", "rc=2 pattern=MISS",
                                 "src/x.rs:99:1 error[E0432]\nboom")
        ok("L1a 行号/路径前缀/数字漂移不翻转", a == b)
        c = EL.failure_signature("m.c1", "rc=1 pattern=MISS",
                                 "src/x.rs:12:5 error[E0308]\nboom")
        ok("L1b 内容变（错误码）则翻转", a != c)
        d = EL.failure_signature("m.c2", "rc=1 pattern=MISS",
                                 "src/x.rs:12:5 error[E0432]\nboom")
        ok("L1c 对象变则翻转", a != d)
        e = EL.failure_signature(
            "m.c1", "rc=1 pattern=MISS",
            "\x1b[32msrc/x.rs:12:5 error[E0432]\x1b[39m\nboom")
        ok("L1d ANSI 色码不翻转", a == e)
        t = EL.failure_signature("m.c1", "rc=1", "2026-09-03T10:00:00 ok")
        t2 = EL.failure_signature("m.c1", "rc=1", "2026-09-03T11:22:33 ok")
        ok("L1e 时间戳不翻转", t == t2)

    # ---------- L2 fix-runner ----------

    def test_l2_fix_runner(self):
        v = _v("fix-runner", circuit="infra",
               fix={"runner_patch": {
                   "unit_test": {"cmd": "cargo osdk test "
                                "--kcmd-args=console=ttyS0"}}})
        with mock.patch.object(EL.agent, "run_agent",
                               return_value=_canned(v)):
            out = EL.run_solve_loop(
                self.ws, self._failure(), self._verify_script(
                    [(True, None)]))
        ok("L2a solved", out["status"] == "solved")
        runner = json.loads((self.ws / "runner.json").read_text(
            encoding="utf-8"))
        ok("L2b runner 合入", "console=ttyS0"
           in runner["unit_test"]["cmd"]
           and runner["build"]["cmd"] == "make kernel")
        ok("L2c applied 记录", any("runner.unit_test"
           in a for r in out["rounds"] for a in r.get("applied") or []))

    # ---------- L3 fix-criteria ----------

    def test_l3_fix_criteria(self):
        ev = self._failure()
        v_bad = _v("fix-criteria", circuit="criteria",
                   fix={"target": "criteria", "expr": "eth0 up"},
                   evidence=[], summary="没给证据")
        with mock.patch.object(EL.agent, "run_agent",
                               return_value=_canned(v_bad)) as mg:
            out = EL.run_solve_loop(self.ws, ev, self._verify_script(
                [(False, {"detail": "hits=0"}),
                 (False, {"detail": "hits=0"})]))
        doc = json.loads((self.ws / "P3" / "modA" / "reports" /
                          "criteria.json").read_text(encoding="utf-8"))
        ok("L3a 无证据被拒", doc["criteria"][0]["expr"] == r"\beth0\b up"
           and any("被拒" in a for r in out["rounds"]
                   for a in r.get("applied") or []))
        ok("L3b 拒后循环继续（现场未变 → 零进展早退）",
           out["status"] == "early-exit" and mg.call_count == 2)

        v_ok = _v("fix-criteria", circuit="criteria",
                  fix={"target": "criteria", "expr": "eth0 up"},
                  evidence=[{"file": "src/x.rs", "line": 9,
                             "quote": "log 行"}])
        with mock.patch.object(EL.agent, "run_agent",
                               return_value=_canned(v_ok)):
            out2 = EL.run_solve_loop(self.ws, ev, self._verify_script(
                [(True, None)]))
        doc2 = json.loads((self.ws / "P3" / "modA" / "reports" /
                           "criteria.json").read_text(encoding="utf-8"))
        c = doc2["criteria"][0]
        ok("L3c 有证据修正", c["expr"] == "eth0 up"
           and c["auto_fixed"]["was"] == r"\beth0\b up")
        gates_doc = json.loads((self.ws / "gates.json").read_text(
            encoding="utf-8"))
        g = [x for x in gates_doc["gates"]
             if x["id"].startswith("p5.criteria-fix.modA.c3")]
        ok("L3d 决策债登记（CP 审计，非阻塞）",
           len(g) == 1 and g[0]["status"] == "applied"
           and g[0]["answered_by"] == "agent"
           and g[0].get("blocking") is False)

    # ---------- L4 park ----------

    def test_l4_park(self):
        v = _v("park", circuit="platform",
               fix={"gap": "INTX-GAP"}, summary="平台缺口")
        with mock.patch.object(EL.agent, "run_agent",
                               return_value=_canned(v)):
            out = EL.run_solve_loop(
                self.ws, self._failure(subject="INTX-GAP", module=None),
                self._verify_script([]))
        ok("L4a 终态 parked（零复验）", out["status"] == "parked")
        pp = json.loads((self.ws / "platform_patches.json").read_text(
            encoding="utf-8"))
        ok("L4b platform_patches 登记",
           any(p["gap"] == "INTX-GAP" for p in pp["patches"]))
        d = json.loads((self.ws / "defects.json").read_text(
            encoding="utf-8"))
        ok("L4c defect 泊车",
           [x for x in d["defects"] if x["id"] == "INTX-GAP"]
           [0]["status"] == "parked")
        ok("L4d parked 不出报告", out["report"] is None)

    # ---------- L5 solved + 回流 ----------

    def test_l5_solved_sediment(self):
        v = _v("fix-code", circuit="migration",
               summary="configure_rx 未接线，已补 RCTL.EN 置位")
        with mock.patch.object(EL.agent, "run_agent",
                               return_value=_canned(v)) as mg:
            out = EL.run_solve_loop(self.ws, self._failure(),
                                    self._verify_script([(True, None)]))
        ok("L5a 单轮 solved", out["status"] == "solved"
           and mg.call_count == 1)
        cand = list((self._kb.TEMP_DIR / "candidates").glob("*.json"))
        ok("L5b 案例回流候选账", bool(cand)
           and "solve-loop" in cand[0].read_text(encoding="utf-8"))

    # ---------- L6 早退 ----------

    def test_l6_early_exit(self):
        v = _v("fix-code")
        results = [(False, {"detail": "hits=0", "boot_log": "eth0 静"}),
                   (False, {"detail": "hits=0", "boot_log": "eth0 静"})]
        with mock.patch.object(EL.agent, "run_agent",
                               return_value=_canned(v)) as mg:
            out = EL.run_solve_loop(
                self.ws, self._failure(), self._verify_script(results))
        ok("L6a 同签名 ×2 早退", out["status"] == "early-exit"
           and mg.call_count == 2)
        ok("L6b 早退也出报告", out["report"] is not None)
        ok("L6c 零进展注记", any("零进展" in (r.get("note") or "")
           for r in out["rounds"]))

    # ---------- L7 unsolved ----------

    def test_l7_unsolved_report(self):
        v = _v("fix-code")
        results = [(False, {"detail": "hits=0", "boot_log": "形态一"}),
                   (False, {"detail": "hits=0", "boot_log": "形态二"}),
                   (False, {"detail": "hits=0", "boot_log": "形态三"})]
        with mock.patch.object(EL.agent, "run_agent",
                               return_value=_canned(v)) as mg:
            out = EL.run_solve_loop(
                self.ws, self._failure(), self._verify_script(results))
        ok("L7a 3 轮耗尽 unsolved", out["status"] == "unsolved"
           and mg.call_count == 3)
        ok("L7b 报告落盘", out["report_path"] is not None
           and (self.ws / out["report_path"]).exists())
        ok("L7c 报告六字段", all(k in out["report"] for k in
           ("symptom", "env_snapshot", "excluded", "experiments",
            "remaining", "reproduce", "evidence_files")))
        evs = (self.ws / "events.jsonl").read_text(encoding="utf-8")
        ok("L7d 事件族（round×3 + end）",
           evs.count('"errorloop_round"') == 3
           and evs.count('"errorloop_end"') == 1
           and '"escalation"' in evs)

    # ---------- L8 no-agent ----------

    def test_l8_no_agent(self):
        os.environ["PORTER_NO_AGENT"] = "1"
        try:
            with mock.patch.object(EL.agent, "run_agent") as mg:
                out = EL.run_solve_loop(self.ws, self._failure(),
                                        self._verify_script([]))
        finally:
            del os.environ["PORTER_NO_AGENT"]
        ok("L8a 零 agent 调用", mg.call_count == 0)
        ok("L8b no-agent + 报告", out["status"] == "no-agent"
           and out["report_path"] is not None)

    # ---------- L9 bypass ----------

    def test_l9_bypass(self):
        with mock.patch("porter.loop.gates.self_diagnosis_enabled",
                        return_value=False):
            out = EL.run_solve_loop(self.ws, self._failure(),
                                    self._verify_script([]))
        ok("L9 熔断 → bypass", out["status"] == "bypass"
           and out["rounds"] == [])

    # ---------- L10 rehang ----------

    def test_l10_rehang(self):
        (self.ws / "deferred.json").write_text(json.dumps(
            {"entries": [{"id": "hw-link.link-ev", "module": "hw-link",
                          "criterion": {"kind": "log_pattern"},
                          "deferred_by": ["os-probe"], "status": "open",
                          "history": []}]}), encoding="utf-8")
        v = _v("rehang", circuit="attribution",
               fix={"to": ["os-stats"]})
        with mock.patch.object(EL.agent, "run_agent",
                               return_value=_canned(v)):
            out = EL.run_solve_loop(
                self.ws, self._failure(subject="hw-link.link-ev"),
                self._verify_script([]))
        ok("L10a 终态 rehung", out["status"] == "rehung")
        d = json.loads((self.ws / "deferred.json").read_text(
            encoding="utf-8"))
        ok("L10b 改挂落账", d["entries"][0]["deferred_by"] == ["os-stats"])

    # ---------- L11 prompt 形态 ----------

    def test_l11_prompt_shape(self):
        prompts = []

        def fake_agent(prompt, **kw):
            prompts.append(prompt)
            if len(prompts) == 1:
                return _canned(_v("fix-code", kb_consulted=["compile-fail.md"],
                                  summary="第一轮没修好"))
            return _canned(_v("fix-code", summary="第二轮修好"))

        with mock.patch.object(EL.agent, "run_agent",
                               side_effect=fake_agent):
            EL.run_solve_loop(self.ws, self._failure(),
                              self._verify_script(
                                  [(False, {"detail": "hits=0",
                                            "boot_log": "第一轮失败"}),
                                   (True, None)]))
        ok("L11a 轮1 注入知识目录（INDEX）",
           "compile-fail.md —— 编译失败处置" in prompts[0]
           and "### failures（已审）" in prompts[0])
        ok("L11b 轮2 不重注目录", "### failures（已审）" not in prompts[1])
        ok("L11c 轮2 带目录提示（自主再检索）", "目录：" in prompts[1])
        ok("L11d 轮2 带上轮总结", "第一轮没修好" in prompts[1]
           and "勿重查" in prompts[1])
        ok("L11e 轮2 带已检索不匹配清单", "compile-fail.md" in prompts[1])
        ok("L11f 轮2 带复验后失败现场", "第一轮失败" in prompts[1])

    # ---------- L12 坏轮恢复 ----------

    def test_l12_bad_round_recovery(self):
        outs = [(1, "TIMEOUT"), _canned(_v("fix-code"))]
        with mock.patch.object(EL.agent, "run_agent",
                               side_effect=outs) as mg:
            out = EL.run_solve_loop(self.ws, self._failure(),
                                    self._verify_script([(True, None)]))
        ok("L12a 坏轮不终止循环", out["status"] == "solved"
           and mg.call_count == 2)
        ok("L12b 坏轮留痕", out["rounds"][0]["action"] is None
           and "不可解析" in out["rounds"][0]["summary"])


if __name__ == "__main__":
    unittest.main()
