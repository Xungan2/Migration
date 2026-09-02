"""porter/loop/diagnose.py 单元测试（错误处理模块·升级报告面）。

覆盖：
E1 报告六字段完整 + evidence_files 全指不可变快照 + json/md 双落盘
E2 excluded 来自求解轮次（unknown → 显式"未能归责"）
E3 experiments 来自 events cmd_end + diagnosis 注入
E7 签名候选回流 kb 候选账（temp/candidates，CP5 审核面）
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.loop import diagnose as DG
from porter.loop import events as EV


def setUpModule():
    os.environ.pop("PORTER_NO_AGENT", None)


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def _mk_ws(tmp: Path) -> Path:
    ws = tmp / "ws"
    (ws / "P3" / "modA" / "reports").mkdir(parents=True)
    (ws / "project.json").write_text(json.dumps(
        {"target_os": str(tmp / "tos"), "linux_driver": "/drv/e1000"}),
        encoding="utf-8")
    (ws / "runner.json").write_text(json.dumps({
        "build": {"cmd": "make kernel"},
        "boot": {"cmd": "make run_kernel", "log_file": "qemu.log"},
        "unit_test": {"cmd": "cargo osdk test"}}), encoding="utf-8")
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
            triage_verdicts=[{"circuit": "migration", "rule_id": None,
                              "confidence": 0.75, "action": "fix-code",
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
        ok("E1d 无人工门（耗尽由挂载点关口停）", stop is False)
        jp = list((self.ws / "escalations").glob("*.json"))
        mp = (self.ws / "escalations" / "os-stats.gprc.md")
        ok("E1e json+md 双落盘", len(jp) == 1 and mp.exists())

    def test_e2_excluded_from_rounds(self):
        rep, _ = DG.generate_escalation_report(
            self.ws, "p5", "modA.x", "s",
            triage_verdicts=[{"circuit": "unknown", "notes": "判不了"}])
        ok("E2a unknown 显式记录",
           any("未能归责" in e["hypothesis"] for e in rep["excluded"]))

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

    def test_e7_signature_candidates_to_kb(self):
        from porter.bootstrap import kb as KB
        old = (KB.KB_ROOT, KB.BASE_DIR, KB.TEMP_DIR)
        KB.TEMP_DIR = self.tmp / "ttemp"
        try:
            DG.generate_escalation_report(
                self.ws, "p6", "RX-PATH", "rx=0",
                triage_verdicts=[{"circuit": "migration",
                                  "signature_candidates": ["NEW-SIG-XYZ"]}])
            DG.generate_escalation_report(
                self.ws, "p6", "RX-PATH", "rx=0",
                triage_verdicts=[{"circuit": "migration",
                                  "signature_candidates": ["NEW-SIG-XYZ"]}])
            cand = list((KB.TEMP_DIR / "candidates").glob("*.json"))
            ok("E7a 候选账在场", bool(cand))
            doc = json.loads(cand[0].read_text(encoding="utf-8"))
            hits = [i for i in doc["items"]
                    if i.get("source", {}).get("ref") == "NEW-SIG-XYZ"]
            ok("E7b 签名候选记入一条（去重闸幂等）+ 建议类 failures",
               len(hits) == 1
               and hits[0].get("suggested_class") == "failures")
        finally:
            KB.KB_ROOT, KB.BASE_DIR, KB.TEMP_DIR = old


if __name__ == "__main__":
    unittest.main()
