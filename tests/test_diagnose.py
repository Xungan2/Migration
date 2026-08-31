"""porter/loop/diagnose.py 单元测试（§15 子系统 D：升级报告 + 有界诊断）。

无真实 agent（monkeypatch）/ 无网络。覆盖：
E1 报告六字段完整 + evidence_files 全指不可变快照 + json/md 双落盘
E2 excluded 来自 triage verdict（unknown → 显式记"未能判定"）
E3 experiments 来自 events cmd_end + diagnosis 注入
E4 诊断编排：2 轮、第二轮带上一轮结论、超时轮也留痕、耗尽自动出报告
E5 诊断收敛（remaining 空）→ 提前停
E6 审核门：diagnosis_escalation=human → 停车 + questions + answers 放行
E7 签名候选自动附 failures.md 候选区（幂等）
E8 build_context_pack：events/快照/报告/defect 打包
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.loop import diagnose as DG
from porter.loop import events as EV


def setUpModule():
    os.environ.pop("PORTER_NO_AGENT", None)   # 本模块全 mock，走 agent 路径


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def _mk_ws(tmp: Path) -> Path:
    ws = tmp / "ws"
    (ws / "P3" / "modA" / "reports").mkdir(parents=True)
    (ws / "runner.json").write_text(json.dumps({
        "build": {"cmd": "make kernel"},
        "boot": {"cmd": "make run_kernel", "log_file": "qemu.log"},
        "unit_test": {"cmd": "cargo osdk test"}}), encoding="utf-8")
    (ws / "defects.json").write_text(json.dumps(
        {"defects": [{"id": "RX-PATH", "title": "RX 未通", "status": "open",
                      "discovered": {"time": "t", "evidence": "rx=0"},
                      "root_cause": "", "fix": "",
                      "regression_evidence": "", "attempts": 0,
                      "history": []}]}), encoding="utf-8")
    return ws


def _mk_snapshot(ws: Path, subject: str) -> None:
    snap = ws / "failure-snapshot-1"
    snap.mkdir()
    (snap / "qemu.log").write_text("boot…rx_bytes=0 tx_bytes=64",
                                   encoding="utf-8")
    (snap / "manifest.json").write_text(json.dumps(
        {"n": 1, "time": "2026-09-02T00:00:00", "source": "p6",
         "subject": subject, "reason": "L3 hits=0",
         "files": {"qemu_log": {"copied": "qemu.log", "size": 10}},
         "kernel": {"found": True, "sha256": "a" * 64},
         "qemu_cmdline": "make run_kernel (EXTRA_QEMU_ARGS=-device e1000)"}),
        encoding="utf-8")


class DiagnoseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porter_diag_t_"))
        self.ws = _mk_ws(self.tmp)

    def test_e1_report_fields(self):
        _mk_snapshot(self.ws, "os-stats.gprc")
        EV.append_event("cmd_end", subject="os-stats.gprc", cmd="make kernel",
                        rc=0, summary="300s", ws=self.ws, mount="p6")
        rep, stop = DG.generate_escalation_report(
            self.ws, "p6", "os-stats.gprc", "counter gprc hits=0",
            triage_verdicts=[{"circuit": "migration", "rule_id": "SIG-05",
                              "confidence": 0.75, "action": "rework",
                              "evidence": [{"file": "b", "line": 1,
                                            "quote": "q"}],
                              "notes": "n"}])
        ok("E1a 六字段齐",
           all(k in rep for k in ("symptom", "env_snapshot", "excluded",
                                  "experiments", "remaining", "reproduce",
                                  "evidence_files")))
        ok("E1b evidence 全指快照",
           rep["evidence_files"]
           and all(f.startswith("failure-snapshot-")
                   for f in rep["evidence_files"]))
        ok("E1c env_snapshot 含 kernel 指纹",
           rep["env_snapshot"]["snapshots"][0]["kernel"]["sha256"] == "a"*64)
        ok("E1d 默认 agent 门不停", stop is False)
        jp = list((self.ws / "escalations").glob("*.json"))
        mp = (self.ws / "escalations" / "os-stats.gprc.md")
        ok("E1e json+md 双落盘", len(jp) == 1 and mp.exists())

    def test_e2_excluded_from_triage(self):
        rep, _ = DG.generate_escalation_report(
            self.ws, "p5", "modA.x", "s",
            triage_verdicts=[{"circuit": "unknown", "notes": "判不了"}])
        ok("E2a unknown 显式记录",
           any("未能判定" in e["hypothesis"] for e in rep["excluded"]))

    def test_e3_experiments(self):
        EV.append_event("cmd_end", subject="modA.x", cmd="rerun probe",
                        rc=1, summary="5s", ws=self.ws, mount="p5")
        rep, _ = DG.generate_escalation_report(
            self.ws, "p5", "modA.x", "s", diagnosis={
                "experiments": [{"name": "trace", "result": "e1000 tx ok",
                                 "conclusion": "TX 活跃"}],
                "excluded": [], "remaining": [], "reproduce": "cmd X"})
        names = [e["name"] for e in rep["experiments"]]
        ok("E3a events 实验", "rerun probe" in names)
        ok("E3b diagnosis 实验", "trace" in names)
        ok("E3c reproduce 取诊断值", rep["reproduce"] == "cmd X")

    def test_e4_two_rounds_and_timeout_trace(self):
        EV.bind(self.ws, "d1")
        outs = [(0, '```json\n{"excluded":[{"hypothesis":"H1",'
                    '"evidence":"e","ref":"s1"}],'
                    '"experiments":[],"remaining":[{"hypothesis":"H2",'
                    '"evidence":""}]}```'),
                (-1, "TIMEOUT")]
        with mock.patch.object(DG.agent, "run_agent",
                               side_effect=outs) as mg:
            merged, rep = DG.run_diagnosis(
                self.ws, {"source": "d1", "subject": "RX-PATH",
                          "detail": "rx=0", "_workdir": self.tmp})
        ok("E4a 恰两轮", mg.call_count == 2)
        ok("E4b R1 结论并入", merged["excluded"][0]["hypothesis"] == "H1")
        ok("E4c R2 超时留痕", merged["rounds"][1]["rc"] == -1
           and merged["rounds"][1]["log"].endswith("R2.log"))
        evs = EV.read_events(self.ws)
        ok("E4d diagnose_round 事件 ×2",
           sum(1 for e in evs if e["kind"] == "diagnose_round") == 2)
        ok("E4e 自动升级报告", any(e["kind"] == "escalation" for e in evs)
           and rep["remaining"])
        # R2 prompt 应携带 R1 结论
        second_prompt = mg.call_args_list[1][0][0]
        ok("E4f R2 带上轮结论", "H1" in second_prompt
           and "勿重查" in second_prompt)
        EV.unbind()

    def test_e5_converged_early(self):
        with mock.patch.object(DG.agent, "run_agent",
                               return_value=(0,
                                             '```json\n{"excluded":[],'
                                             '"experiments":[],'
                                             '"remaining":[]}```')) as mg:
            merged, _ = DG.run_diagnosis(
                self.ws, {"source": "d1", "subject": "X", "detail": "d",
                          "_workdir": self.tmp})
        ok("E5 收敛即停", mg.call_count == 1)

    def test_e6_human_gate(self):
        cfg = {"review_gates": {"diagnosis_escalation": "human"}}
        _mk_snapshot(self.ws, "modA.x")
        rep, stop = DG.generate_escalation_report(
            self.ws, "p5", "modA.x", "s", cfg=cfg)
        ok("E6a human 停车", stop is True)
        q = (self.ws / "human_questions.md").read_text(encoding="utf-8")
        ok("E6b questions 写入", "diagnosis_escalation: approve" in q)
        ok("E6c 未放行", DG.released(self.ws) is False)
        (self.ws / "answers.md").write_text(
            "diagnosis_escalation: approve\n", encoding="utf-8")
        ok("E6d answers 放行", DG.released(self.ws) is True)

    def test_e7_signature_candidates_attach(self):
        from porter.common.agent import TOOL_ROOT
        failures = TOOL_ROOT / "knowledge" / "failures.md"
        backup = failures.read_text(encoding="utf-8")
        try:
            DG._attach_signature_candidates(self.ws, ["NEW-SIG-XYZ"])
            DG._attach_signature_candidates(self.ws, ["NEW-SIG-XYZ"])
            text = failures.read_text(encoding="utf-8")
            ok("E7a 候选附上", "NEW-SIG-XYZ" in text)
            ok("E7b 幂等", text.count("NEW-SIG-XYZ") == 1)
        finally:
            failures.write_text(backup, encoding="utf-8")

    def test_e8_context_pack(self):
        _mk_snapshot(self.ws, "RX-PATH")
        EV.append_event("cmd_end", subject="RX-PATH", cmd="x", rc=0,
                        ws=self.ws, mount="d1")
        DG.generate_escalation_report(self.ws, "d1", "RX-PATH", "rx=0")
        pack_p = DG.build_context_pack(self.ws, "d1", "RX-PATH")
        pack = json.loads(pack_p.read_text(encoding="utf-8"))
        ok("E8a 打包字段", all(k in pack for k in
            ("events_file", "snapshots", "escalation_reports", "defect")))
        ok("E8b 快照关联", pack["snapshots"] == ["failure-snapshot-1"])
        ok("E8c defect 关联", pack["defect"]["id"] == "RX-PATH")


if __name__ == "__main__":
    unittest.main()
