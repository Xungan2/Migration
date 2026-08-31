"""porter/loop/triage.py 单元测试（§15 子系统 C：五回路各 ≥1 例 + 证据强制）。

无真实 agent（monkeypatch canned verdict）/ 无网络 / 无 docker。覆盖：
T1 规则命中：SIG-01 docker 锁 → infra
T2 规则命中：SIG-02 ktest 静默 → infra + ut-console-args 定向修复落 runner
T3 规则命中：SIG-02b 半成品 ISO → infra + full-make-kernel 建议
T4 规则命中：SIG-03 ANSI 边界正则 → criteria 自动修正 criteria.json
T5 规则命中：COMPILE-FAIL / SIG-05 RX 复合 → migration
T6 规则命中：DEFER-UNCLEARED → attribution 改挂 deferred.json
T7 规则命中：SIG-06 平台缺口 → platform 泊车 + platform_patches 登记
T8 规则命中：SIG-04 计划过期（d1）→ criteria/close_stale（agent 证据链）
T9 agent 兜底：规则未命中 → canned verdict 落 events；两连败 → unknown
T10 门控：b_class_autofix=human → 不修正，写 human_questions.md
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.loop import triage as TR


def setUpModule():
    os.environ.pop("PORTER_NO_AGENT", None)   # 本模块 T9 全 mock


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


class TriageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porter_tri_t_"))
        self.ws = self.tmp / "ws"
        (self.ws / "P3" / "modA" / "reports").mkdir(parents=True)
        (self.ws / "P6" / "reports").mkdir(parents=True)
        (self.ws / "P3" / "modA" / "reports" / "criteria.json").write_text(
            json.dumps({"criteria": [
                {"id": "modA.c3", "layer": "L3", "kind": "log_pattern",
                 "expr": r"\beth0\b up", "deferred_by": None}]}),
            encoding="utf-8")

    def _run(self, evidence, use_agent=False):
        return TR.run_triage(self.ws, evidence, use_agent=use_agent)

    def test_t1_docker_lock(self):
        v = self._run({"source": "p6", "subject": "P6.build",
                       "kind": "compile",
                       "detail": "rc=1 pattern=MISS",
                       "build_out": "docker: resource temporarily "
                       "unavailable"})
        ok("T1a infra 命中", v["circuit"] == "infra")
        ok("T1b SIG-01", v["rule_id"] == "SIG-01"
           and v["action"] == "rerun")

    def test_t2_ktest_silent(self):
        runner = {"unit_test": {"cmd": "cargo osdk test; cat log",
                                "success_pattern": "passed; 0 failed;"}}
        ev = {"source": "p5", "subject": "modA.c0", "kind": "unit_test",
              "module": "modA", "detail": "输出无 'passed; 0 failed;'",
              "ut_out": "", "runner": runner,
              "events_tail": [{"kind": "cmd_end", "rc": 0,
                               "cmd": "cargo osdk test …",
                               "summary": "300s log=P5_modA_acc_ut.log"}]}
        v = self._run(ev)
        ok("T2a SIG-02 命中", v["circuit"] == "infra"
           and v["rule_id"] == "SIG-02")
        ok("T2b 定向修复建议", v.get("suggested_fix") == "ut-console-args")
        r = TR.apply_verdict(self.ws, ev, v)
        runner2 = json.loads((self.ws / "runner.json").read_text(
            encoding="utf-8"))
        ok("T2c runner ut 命令补 console 参数",
           "--kcmd-args" in runner2["unit_test"]["cmd"]
           and "console=ttyS0" in runner2["unit_test"]["cmd"])
        ok("T2d auto-fixed 标记",
           "auto-fixed" in runner2["unit_test"]["notes"])
        ok("T2e applied 记录", any("SIG-02" in a for a in r["applied"]))
        # 幂等：再来一次不重复追加
        TR.apply_verdict(self.ws, ev, v)
        runner3 = json.loads((self.ws / "runner.json").read_text(
            encoding="utf-8"))
        ok("T2f 幂等", runner3["unit_test"]["cmd"].count("--kcmd-args") == 2)

    def test_t3_halfbuilt_iso(self):
        v = self._run({"source": "p5", "subject": "modA.boot",
                       "kind": "boot", "layer": "L2",
                       "detail": "rc=0 success_pattern=MISS panic=no",
                       "boot_log": "BdsDxe: loading Boot0002 "
                       '"UEFI QEMU DVD-ROM"',
                       "events_tail": [{"kind": "cmd_end", "rc": -1,
                                        "cmd": "make run_kernel",
                                        "summary": "TIMEOUT after 600s"}]})
        ok("T3a SIG-02b 命中", v["circuit"] == "infra"
           and v["rule_id"] == "SIG-02b")
        ok("T3b full-make-kernel 建议",
           v.get("suggested_fix") == "full-make-kernel")

    def test_t4_ansi_regex_autofix(self):
        ev = {"source": "p5", "subject": "modA.c3", "kind": "log_pattern",
              "module": "modA", "expr": r"\beth0\b up", "detail": "hits=0",
              "boot_log": "eth0 up",
              "boot_log_raw": "\x1b[32meth0 up\x1b[39m"}
        v = self._run(ev)
        ok("T4a SIG-03 命中", v["circuit"] == "criteria"
           and v["action"] == "autofix")
        ok("T4b 修正值去 \\b", v["fix_value"] == "eth0 up")
        TR.apply_verdict(self.ws, ev, v)
        doc = json.loads((self.ws / "P3" / "modA" / "reports" /
                          "criteria.json").read_text(encoding="utf-8"))
        c = doc["criteria"][0]
        ok("T4c expr 已修正", c["expr"] == "eth0 up")
        ok("T4d auto-fixed 档案", c["auto_fixed"]["was"] == r"\beth0\b up"
           and c["auto_fixed"]["evidence"])

    def test_t5_migration(self):
        v1 = self._run({"source": "p5", "subject": "modA.compile",
                        "kind": "compile", "detail": "rc=2",
                        "build_out": "error[E0432]: unresolved import"})
        ok("T5a COMPILE-FAIL → migration", v1["circuit"] == "migration"
           and v1["action"] == "rework")
        v2 = self._run({"source": "p6", "subject": "os-stats.gprc",
                        "kind": "counter", "detail": "hits=0",
                        "boot_log": "e1000 stats: rx_bytes=0 tx_bytes=64"})
        ok("T5b SIG-05 复合型 → migration", v2["circuit"] == "migration"
           and v2["rule_id"] == "SIG-05")

    def test_t6_attribution(self):
        (self.ws / "deferred.json").write_text(json.dumps(
            {"entries": [{"id": "hw-link.link-ev", "module": "hw-link",
                          "criterion": {"kind": "log_pattern"},
                          "deferred_by": ["os-probe"], "status": "open",
                          "history": []}]}), encoding="utf-8")
        ev = {"source": "p5", "subject": "hw-link.link-ev",
              "deferred_uncleared": ["hw-link.link-ev"]}
        v = self._run(ev)
        ok("T6a attribution 命中", v["circuit"] == "attribution")
        v["rehang_to"] = ["os-stats"]      # agent 给出的真实消费者
        TR.apply_verdict(self.ws, ev, v)
        d = json.loads((self.ws / "deferred.json").read_text(
            encoding="utf-8"))
        ok("T6b 改挂落账", d["entries"][0]["deferred_by"] == ["os-stats"]
           and d["entries"][0]["history"])

    def test_t7_platform_park(self):
        ev = {"source": "d1", "subject": "INTX-DELIVERY",
              "detail": "ics kick: icr=0x14 但 irq_count=0",
              "defect": {"id": "INTX-DELIVERY",
                         "title": "INTx 中断交付不可达",
                         "history": [{"detail": "平台缺口泊车：OSTD "
                                        "ioapic.rs 电平触发缺失（禁改）"}]}}
        v = self._run(ev)
        ok("T7a SIG-06 → platform", v["circuit"] == "platform"
           and v["action"] == "park")
        TR.apply_verdict(self.ws, ev, v)
        pp = json.loads((self.ws / "platform_patches.json").read_text(
            encoding="utf-8"))
        ok("T7b platform_patches 登记",
           any(p["gap"] == "INTX-DELIVERY" and p["status"] == "proposed"
               for p in pp["patches"]))
        d = json.loads((self.ws / "defects.json").read_text(
            encoding="utf-8"))
        ok("T7c defect add+park 幂等",
           [x for x in d["defects"] if x["id"] == "INTX-DELIVERY"
            ][0]["status"] == "parked")

    def test_t8_stale_doc(self):
        (self.ws / "defects.json").write_text(json.dumps(
            {"defects": [{"id": "RESET-HW-STALE", "title":
                          "§14 遗留#2 bring-up 未调 reset_hw",
                          "status": "open",
                          "discovered": {"time": "t", "evidence":
                                         "计划文档 §14 称未调用"},
                          "root_cause": "", "fix": "",
                          "regression_evidence": "", "attempts": 0,
                          "history": []}]}), encoding="utf-8")
        defect = json.loads((self.ws / "defects.json").read_text(
            encoding="utf-8"))["defects"][0]
        ev = {"source": "d1", "subject": "RESET-HW-STALE",
              "defect": defect}
        v = self._run(ev)
        ok("T8a SIG-04 候选 → criteria", v["circuit"] == "criteria"
           and v["fix_target"] == "close_stale")
        # agent 补代码实测证据后闭账
        v["evidence"] = [{"file": "kernel/core/comps/e1000/src/"
                                  "os_probe.rs", "line": 765,
                          "quote": "hw.reset_hw()"}]
        TR.apply_verdict(self.ws, ev, v)
        d = json.loads((self.ws / "defects.json").read_text(
            encoding="utf-8"))
        e = [x for x in d["defects"] if x["id"] == "RESET-HW-STALE"][0]
        ok("T8b 闭账 stale + file:line 证据",
           e["status"] == "fixed" and "os_probe.rs:765" in e["root_cause"])

    def test_t9_agent_fallback_and_unknown(self):
        ev = {"source": "p5", "subject": "modA.weird", "kind": "log_pattern",
              "expr": "whatever", "detail": "hits=0", "boot_log": "…"}
        canned = {"circuit": "migration", "confidence": 0.7,
                  "evidence": [{"file": "src/x.rs", "line": 9,
                                "quote": "bug"}], "action": "rework",
                  "notes": "agent 判定"}
        with mock.patch.object(TR.agent, "run_agent",
                               return_value=(0, "```json\n" +
                                             json.dumps(canned) + "\n```")):
            v = self._run(ev, use_agent=True)
        ok("T9a canned agent verdict", v["circuit"] == "migration"
           and v["evidence"][0]["file"] == "src/x.rs")
        evs = json.loads("[" + ",".join(
            (self.ws / "events.jsonl").read_text(
                encoding="utf-8").splitlines()) + "]")
        ok("T9b triage 事件落账", any(e["kind"] == "triage"
                                      for e in evs))
        with mock.patch.object(TR.agent, "run_agent",
                               return_value=(1, "TIMEOUT")) as mg:
            v2 = self._run({**ev, "subject": "modA.weird2"},
                           use_agent=True)
        ok("T9c 两连败 → unknown/escalate", v2["circuit"] == "unknown"
           and v2["action"] == "escalate")
        ok("T9d agent 有界 2 次", mg.call_count == 2)

    def test_t10_human_gate(self):
        ev = {"source": "p5", "subject": "modA.c3", "kind": "log_pattern",
              "module": "modA", "expr": "x", "detail": "hits=0"}
        v = {"circuit": "criteria", "action": "autofix",
             "fix_target": "criteria", "fix_value": "fixed-expr",
             "rule_id": "SIG-03", "confidence": 0.7,
             "evidence": [{"file": "f.c", "line": 1, "quote": "q"}],
             "signature_candidates": [],
             "time": "2026-09-02T00:00:00", "source": "p5",
             "subject": "modA.c3"}
        r = TR.apply_verdict(self.ws, ev, v, gate_ok=False)
        ok("T10a human 门不修正", r["applied"] == [] and r["human_stop"])
        q = (self.ws / "human_questions.md").read_text(encoding="utf-8")
        ok("T10b 审核问题落盘", "b_class_autofix: approve" in q
           and "modA.c3" in q)


if __name__ == "__main__":
    unittest.main()
