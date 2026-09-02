"""S5 欠账自动化测试：p6 --draft-l4（L4 草案生成器）/ --defect-fix。

无网络 / 无 docker。agent 与探测全部 mock：
A. draft_l4 机器路径（PORTER_NO_AGENT）：素材收集 + 预分类 + schema 过
B. draft_l4 幂等（已有 draft 跳过）与无素材 rc 2
C. draft_l4 agent 路径（mock run_agent 返回合法草案）
D. fix_defect 前置缺失（未诊断 rc 2 / PORTER_NO_AGENT rc 2）
E. fix_defect happy（mock agent+build+boot → 四字段闭账 + 决策债 CP4）
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.loop import p6 as P6
from porter.common import agent as AGENT


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def _mk_ws() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="porter_s5_t_"))
    ws = tmp / "ws"
    ws.mkdir()
    (ws / "P6" / "logs").mkdir(parents=True)
    return ws


def _mk_l4_material(ws: Path) -> None:
    # deferred 全局哨兵条目 ×2（log_pattern + unit_test）
    (ws / "deferred.json").write_text(json.dumps({
        "entries": [
            {"id": "link-up", "module": "m1",
             "criterion": {"id": "link-up", "kind": "log_pattern",
                           "expr": "NIC Link is Up"},
             "deferred_by": ["__P6__"], "status": "open", "history": []},
            {"id": "ut-x", "module": "m1",
             "criterion": {"id": "ut-x", "kind": "unit_test", "expr": "t1"},
             "deferred_by": ["__P6__"], "status": "open", "history": []},
            {"id": "later-m2", "module": "m2",
             "criterion": {"id": "later-m2", "kind": "log_pattern",
                           "expr": "x"},
             "deferred_by": ["m2"], "status": "open", "history": []},
        ]}), encoding="utf-8")
    # P3 e2e 判据 ×1（应被收进素材；P3 非 e2e 不收）
    p3r = ws / "P3" / "m1" / "reports"
    p3r.mkdir(parents=True)
    (p3r / "criteria.json").write_text(json.dumps({"criteria": [
        {"id": "tcp-rx", "layer": "L4", "kind": "e2e", "expr": "",
         "deferred_by": None},
        {"id": "not-e2e", "layer": "L3", "kind": "log_pattern",
         "expr": "y", "deferred_by": None}]}), encoding="utf-8")


class DraftL4Test(unittest.TestCase):
    def setUp(self):
        self.ws = _mk_ws()
        self._old = os.environ.get("PORTER_NO_AGENT")
        os.environ["PORTER_NO_AGENT"] = "1"

    def tearDown(self):
        if self._old is None:
            os.environ.pop("PORTER_NO_AGENT", None)
        else:
            os.environ["PORTER_NO_AGENT"] = self._old

    def test_a_machine_draft(self):
        _mk_l4_material(self.ws)
        rc = P6.draft_l4(self.ws)
        ok("A1 rc 0", rc == 0)
        doc = json.loads(P6.l4_criteria_path(self.ws).read_text(
            encoding="utf-8"))
        ok("A2 status draft", doc["status"] == "draft")
        ids = {c["id"] for c in doc["criteria"]}
        ok("A3 素材收全（哨兵 2 + e2e 1；消费者条目/非 e2e 不收）",
           ids == {"link-up", "ut-x", "tcp-rx"})
        by = {c["id"]: c for c in doc["criteria"]}
        ok("A4 kind→form 预分类",
           by["link-up"]["form"] == "boot观测"
           and by["ut-x"]["form"] == "内核自测"
           and by["tcp-rx"]["form"] == "流量驱动")
        _ok_items, errs = P6.validate_l4(doc["criteria"])
        ok("A5 机器草案过 schema", not errs, str(errs[:3]))

    def test_b_idempotent_and_empty(self):
        ok("B1 无素材 rc 2", P6.draft_l4(self.ws) == 2)
        _mk_l4_material(self.ws)
        P6.draft_l4(self.ws)
        rc = P6.draft_l4(self.ws)
        ok("B2 已有 draft 跳过 rc 0", rc == 0)

    def test_c_agent_draft(self):
        os.environ.pop("PORTER_NO_AGENT", None)
        _mk_l4_material(self.ws)
        good = {"criteria": [
            {"id": "link-up", "title": "链路 up", "form": "boot观测",
             "expr": "NIC Link is Up", "rationale": "r", 
             "disposition": "clear"},
            {"id": "ut-x", "title": "自测", "form": "内核自测",
             "expr": "L4 ut-x PASS", "rationale": "r",
             "disposition": "clear"},
            {"id": "tcp-rx", "title": "tcp", "form": "流量驱动",
             "expr": "rx ok", "rationale": "r", "disposition": "park"}]}
        with mock.patch.object(AGENT, "run_agent",
                               return_value=(0, "json")) as m_run, \
             mock.patch.object(AGENT, "extract_json",
                               return_value=good), \
             mock.patch.object(AGENT, "load_skill", return_value="SKILL"):
            rc = P6.draft_l4(self.ws)
        ok("C1 agent 路径 rc 0", rc == 0)
        ok("C2 agent 被调用", m_run.called)
        doc = json.loads(P6.l4_criteria_path(self.ws).read_text(
            encoding="utf-8"))
        ok("C3 采用 agent 草案（title 来自 agent）",
           doc["criteria"][0]["title"] == "链路 up")
        ok("C4 park 保留", any(c["disposition"] == "park"
                               for c in doc["criteria"]))


def _mk_defect_ws() -> Path:
    ws = _mk_ws()
    (ws / "defects.json").write_text(json.dumps({"defects": [
        {"id": "RX-PATH", "title": "收包路径坏",
         "status": "open", "attempts": 0,
         "discovered": {"time": "t", "evidence": "p6 红项 rx hits=0"},
         "history": []}]}), encoding="utf-8")
    esc = ws / "escalations"
    esc.mkdir()
    (esc / "RX-PATH.md").write_text(
        "# 升级报告\n- remaining: 描述符环形缓冲映射错误\n", encoding="utf-8")
    (ws / "project.json").write_text(json.dumps(
        {"target_os": str(ws / "tos")}), encoding="utf-8")
    (ws / "runner.json").write_text(json.dumps(
        {"build": {"cmd": "make", "timeout_full_sec": 60,
                   "timeout_inc_sec": 30},
         "boot": {"cmd": "boot", "timeout_sec": 30, "success_pattern": "ok",
                  "panic_pattern": "panic", "log_file": "b.log"},
         "inject_device": {"mechanism": "cmd", "cmd_suffix": "-d X",
                           "example_args": "-d X"}}), encoding="utf-8")
    return ws


class FixDefectTest(unittest.TestCase):
    def setUp(self):
        self.ws = _mk_defect_ws()
        self._old = os.environ.get("PORTER_NO_AGENT")
        self._old_sd = os.environ.get("PORTER_SELF_DIAGNOSIS")
        # --defect-fix 属 §15 链（依赖 diagnose 的升级报告）——测试强制开
        os.environ["PORTER_SELF_DIAGNOSIS"] = "1"

    def tearDown(self):
        if self._old is None:
            os.environ.pop("PORTER_NO_AGENT", None)
        else:
            os.environ["PORTER_NO_AGENT"] = self._old
        if self._old_sd is None:
            os.environ.pop("PORTER_SELF_DIAGNOSIS", None)
        else:
            os.environ["PORTER_SELF_DIAGNOSIS"] = self._old_sd

    def test_d_prereq(self):
        os.environ["PORTER_NO_AGENT"] = "1"
        ok("D1 无 agent rc 2（diagnose）",
           P6.diagnose_defect(self.ws, "RX-PATH") == 2)
        ok("D1b --defect-fix 重定向同语义",
           P6.fix_defect(self.ws, "RX-PATH") == 2)
        os.environ.pop("PORTER_NO_AGENT", None)
        bad = _mk_ws()
        (bad / "defects.json").write_text(json.dumps({"defects": [
            {"id": "NODIAG", "title": "x", "status": "open",
             "history": []}]}), encoding="utf-8")
        ok("D2 缺陷不存在 rc 2",
           P6.diagnose_defect(bad, "GHOST") == 2)

    def test_e_happy(self):
        # d1 求解循环 happy：fix-code verdict + 双信号复验通过 → 闭账 + CP4 债
        from porter.loop import errorloop as EL
        verdict = {"status": "done", "circuit": "migration",
                   "action": "fix-code",
                   "evidence": [{"file": "kernel/core/comps/e1000/src/"
                                         "rx.rs", "line": 12,
                                 "quote": "描述符环形缓冲映射错误"}],
                   "summary": "描述符环形缓冲映射错误（改 dma 映射长度计算）",
                   "confidence": 0.9}
        from porter.env import probe as ENV_PROBE
        with mock.patch.object(EL.agent, "run_agent",
                               return_value=(0, "```json\n"
                                             + json.dumps(verdict)
                                             + "\n```")), \
             mock.patch.object(ENV_PROBE, "probe_build",
                               return_value={"item": "build", "ok": True,
                                             "detail": "rc=0"}), \
             mock.patch.object(P6, "_boot_and_log",
                               return_value=(True, "raw", "boot log ok",
                                             "file")):
            rc = P6.diagnose_defect(self.ws, "RX-PATH")
        ok("E1 happy rc 0", rc == 0)
        d = json.loads((self.ws / "defects.json").read_text(
            encoding="utf-8"))["defects"][0]
        ok("E2 四字段闭账", d["status"] == "fixed"
           and "描述符" in d["root_cause"]
           and "build+boot PASS" in d["regression_evidence"])
        ok("E3 history 落账 fixed-auto",
           any(h.get("event") == "fixed-auto" for h in d["history"]))
        led = json.loads((self.ws / "gates.json").read_text(
            encoding="utf-8"))
        gate = [g for g in led["gates"] if g["id"] == "p6.defect.fix.RX-PATH"]
        ok("E4 决策债登记（CP4 批审对象）",
           len(gate) == 1 and gate[0]["status"] == "applied"
           and gate[0]["answered_by"] == "agent"
           and gate[0]["checkpoint"] == "CP4")


if __name__ == "__main__":
    unittest.main()
