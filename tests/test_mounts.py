"""三挂载端到端最小单测（错误处理模块：求解循环内核三处复用）。

M1 挂载① p5.run_p5：注入 docker 锁失败（boot 日志）→ 快照 → 求解
   agent 判 infra/rerun → 复验自愈（现场自愈注入）→ PASS 收尾
   （求解循环内部消化，loop 不感知 attempts）
M1b 挂载① p5.run_p5：修不动的编译失败 → 求解循环零进展早退 →
   p5.unsolved 关口（rc 3）+ 升级报告 + attempts 不烧
M2 挂载② p6.execute：红项分诊（§15 旧内核——S4 接求解循环）
M3 挂载③ p6.diagnose_defect（同上）
agent 全 mock（canned verdict）/ 规则路径即命中，不真调 opencode。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def setUpModule():
    os.environ.setdefault("PORTER_NO_AGENT", "1")   # M2/M3 旧内核规则路径
    os.environ.setdefault("PORTER_SELF_DIAGNOSIS", "1")


def tearDownModule():
    os.environ.pop("PORTER_NO_AGENT", None)
    os.environ.pop("PORTER_SELF_DIAGNOSIS", None)


from porter.loop import errorloop as EL
from porter.loop import events as EV
from porter.loop import p5 as P5
from porter.loop import p6 as P6


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


class _NoAgentOff:
    """M1/M1b 用：临时摘掉 PORTER_NO_AGENT（solve 循环需要 agent 位）。"""

    def __enter__(self):
        self._old = os.environ.pop("PORTER_NO_AGENT", None)
        return self

    def __exit__(self, *a):
        if self._old is not None:
            os.environ["PORTER_NO_AGENT"] = self._old


def _canned(verdict):
    return (0, "```json\n" + json.dumps(verdict, ensure_ascii=False)
            + "\n```")


class _Ws:
    """最小工作区：runner cmd=自愈式 bash（第一次写失败日志+落 marker，
    之后写成功日志）——注入 infra 失败且重跑自愈。"""

    def __init__(self, tmp: Path, name: str):
        self.ws = tmp / name
        self.tos = tmp / f"tos_{name}"
        (self.tos).mkdir(parents=True)
        (self.ws / "P3" / "modA" / "reports").mkdir(parents=True)
        (self.ws / "P4" / "modA" / "reports").mkdir(parents=True)
        (self.ws / "project.json").write_text(json.dumps(
            {"target_os": str(self.tos), "linux_driver": "/drv",
             "category": ["net"]}), encoding="utf-8")
        (self.ws / "P4" / "modA" / "reports" / "migration.json") \
            .write_text("{}", encoding="utf-8")
        self._runner()

    def _runner(self):
        boot_cmd = ("if [ -f healed ]; then echo OK > qemu.log; "
                    "else echo 'docker: resource temporarily "
                    "unavailable' > qemu.log; touch healed; fi")
        (self.ws / "runner.json").write_text(json.dumps({
            "build": {"cmd": "true", "timeout_full_sec": 5,
                      "success_pattern": ""},
            "boot": {"cmd": boot_cmd, "timeout_sec": 5,
                     "log_file": "qemu.log",
                     "success_pattern": "OK", "panic_pattern": "panic"},
            "inject_device": {"mechanism": "env",
                              "env": {"EXTRA_QEMU_ARGS": "<DEVICE_ARGS>"},
                              "example_args": {"net": "-device e1000"}},
            "unit_test": {"mechanism": "none"}}), encoding="utf-8")

    def criteria(self, items):
        (self.ws / "P3" / "modA" / "reports" / "criteria.json").write_text(
            json.dumps({"criteria": items}), encoding="utf-8")


class MountP5Test(unittest.TestCase):
    def test_m1_p5_solve_rerun_selfheal(self):
        tmp = Path(tempfile.mkdtemp(prefix="porter_m1_"))
        w = _Ws(tmp, "ws")
        w.criteria([{"id": "modA.c1", "layer": "L3",
                     "kind": "log_pattern", "expr": "OK",
                     "deferred_by": None}])
        verdict = {"status": "done", "circuit": "infra", "action": "rerun",
                   "evidence": [{"file": "<boot-log>", "line": 0,
                                 "quote": "resource temporarily "
                                          "unavailable"}],
                   "summary": "docker 锁——幂等重跑", "confidence": 0.9}
        with _NoAgentOff(), mock.patch.object(
                EL.agent, "run_agent",
                return_value=_canned(verdict)) as mg:
            rc = P5.run_p5(w.ws, "modA", ["modA"])
        acc = json.loads((w.ws / "P5" / "modA" / "reports" /
                          "acceptance.json").read_text(encoding="utf-8"))
        ok("M1a 求解 rerun 后自愈 PASS", rc == 0 and acc["pass"] is True)
        solve = acc.get("solve") or []
        ok("M1b solve 节在档（1 轮 rerun 复验过）",
           len(solve) == 1 and mg.call_count == 1
           and solve[0]["action"] == "rerun"
           and solve[0].get("verified") is True
           and solve[0]["circuit"] == "infra")
        evs = EV.read_events(w.ws)
        kinds = [e["kind"] for e in evs]
        ok("M1c 快照先于求解", "snapshot" in kinds
           and "errorloop_round" in kinds
           and kinds.index("snapshot") < kinds.index("errorloop_round")
           and kinds.count("cmd_start") >= 4)      # ≥2 轮 build+boot
        ok("M1d 求解事件族落账", "errorloop_round" in kinds
           and "errorloop_end" in kinds)
        ok("M1e 快照实物", (w.ws / "failure-snapshot-1" /
                            "manifest.json").exists())
        rep_md = (w.ws / "P5" / "modA" / "reports" / "report.md") \
            .read_text(encoding="utf-8")
        ok("M1f report.md 求解节", "求解循环（错误处理挂载①）" in rep_md
           and "rerun" in rep_md)

    def test_m1b_p5_unsolved_gate(self):
        tmp = Path(tempfile.mkdtemp(prefix="porter_m1b_"))
        w = _Ws(tmp, "ws")
        # 修不动的编译失败（cmd=false 恒败）：solve 循环两轮同签名 →
        # 零进展早退 → p5.unsolved 关口 + 升级报告，attempts 不烧
        (w.ws / "runner.json").write_text(json.dumps({
            "build": {"cmd": "false", "timeout_full_sec": 5,
                      "success_pattern": ""},
            "boot": {"cmd": "echo OK > qemu.log", "timeout_sec": 5,
                     "log_file": "qemu.log", "success_pattern": "OK",
                     "panic_pattern": "panic"},
            "inject_device": {"mechanism": "env",
                              "env": {"EXTRA_QEMU_ARGS": "<DEVICE_ARGS>"},
                              "example_args": {"net": "-device e1000"}},
            "unit_test": {"mechanism": "none"}}), encoding="utf-8")
        (w.tos / "qemu.log").write_text("OK", encoding="utf-8")
        w.criteria([{"id": "modA.c1", "layer": "L3",
                     "kind": "log_pattern", "expr": "OK",
                     "deferred_by": None}])
        (w.ws / "loop_state.json").write_text(json.dumps(
            {"order": ["modA"], "modules": {"modA": {
                "phase": "p5", "attempts": {"p3": 0, "p4": 0, "p5": 0}}}}),
            encoding="utf-8")
        verdict = {"status": "done", "circuit": "migration",
                   "action": "fix-code",
                   "evidence": [{"file": "src/x.rs", "line": 9,
                                 "quote": "bug"}],
                   "summary": "编译错——迁移代码问题", "confidence": 0.9}
        with _NoAgentOff(), mock.patch.object(
                EL.agent, "run_agent",
                return_value=_canned(verdict)) as mg:
            rc = P5.run_p5(w.ws, "modA", ["modA"])
        ok("M1b-a 未解决 → 关口 rc 3", rc == 3)
        gates_doc = json.loads((w.ws / "gates.json").read_text(
            encoding="utf-8"))
        ok("M1b-b p5.unsolved 关口登记",
           any(g["id"] == "p5.unsolved.modA"
               and g["status"] in ("open", "invalid")
               for g in gates_doc["gates"]))
        acc = json.loads((w.ws / "P5" / "modA" / "reports" /
                          "acceptance.json").read_text(encoding="utf-8"))
        ok("M1b-c 同签名早退（2 轮）", len(acc.get("solve") or []) == 2
           and mg.call_count == 2)
        ok("M1b-d 升级报告在场",
           any((w.ws / "escalations").glob("modA-*.json")))
        st = json.loads((w.ws / "loop_state.json").read_text(
            encoding="utf-8"))
        ok("M1b-e attempts 不烧（p5=0）",
           st["modules"]["modA"]["attempts"]["p5"] == 0)


class MountP6Test(unittest.TestCase):
    def test_m2_p6_execute_red_solve_noagent(self):
        tmp = Path(tempfile.mkdtemp(prefix="porter_m2_"))
        w = _Ws(tmp, "ws")
        (w.tos / "qemu.log").write_text(
            "docker: database is locked", encoding="utf-8")
        (w.ws / "runner.json").write_text(json.dumps({
            "build": {"cmd": "true", "timeout_full_sec": 5,
                      "success_pattern": ""},
            "boot": {"cmd": "echo 'docker: database is locked' "
                            "> qemu.log", "timeout_sec": 5,
                     "log_file": "qemu.log",
                     "success_pattern": "Successfully booted",
                     "panic_pattern": "panic"},
            "inject_device": {"mechanism": "env",
                              "env": {"EXTRA_QEMU_ARGS": "<DEVICE_ARGS>"},
                              "example_args": {"net-user": "-netdev user,"
                                                "id=e1"}},
            "unit_test": {"mechanism": "none"}}), encoding="utf-8")
        (w.ws / "loop_state.json").write_text(json.dumps(
            {"order": ["modA"], "modules": {"modA": {
                "phase": "done", "attempts": {"p3": 0, "p4": 0,
                                               "p5": 0}}}}),
            encoding="utf-8")
        w.criteria([{"id": "modA.c1", "layer": "L3",
                     "kind": "log_pattern", "expr": "SUCCESS-MARKER",
                     "deferred_by": None}])
        (w.ws / "P5" / "modA" / "reports").mkdir(parents=True,
                                                 exist_ok=True)
        (w.ws / "P5" / "modA" / "reports" / "acceptance.json").write_text(
            json.dumps({"pass": True}), encoding="utf-8")
        # 模块级 PORTER_NO_AGENT=1（setUpModule）→ 求解降级只出报告
        rc = P6.execute(w.ws)
        ok("M2a 红项未解决 → 关口 rc 3", rc == 3)
        health = json.loads((w.ws / "P6" / "reports" / "health.json")
                            .read_text(encoding="utf-8"))
        ok("M2b solve 节为空（零 agent 轮）", health.get("solve") == [])
        gates_doc = json.loads((w.ws / "gates.json").read_text(
            encoding="utf-8"))
        ok("M2c p6.unsolved 关口登记",
           any(g["id"] == "p6.unsolved"
               and g["status"] in ("open", "invalid")
               for g in gates_doc["gates"]))
        ok("M2d 升级报告在场",
           any((w.ws / "escalations").glob("P6-red-*.json")))
        ok("M2e 快照在场", (w.ws / "failure-snapshot-1" /
                             "manifest.json").exists())

    def test_m3_defect_diagnose_solve_park(self):
        tmp = Path(tempfile.mkdtemp(prefix="porter_m3_"))
        w = _Ws(tmp, "ws")
        (w.ws / "defects.json").write_text(json.dumps(
            {"defects": [{"id": "INTX-DELIVERY",
                          "title": "INTx 中断 CPU 交付不可达",
                          "status": "open",
                          "discovered": {"time": "t",
                                         "evidence": "icr=0x14 但 "
                                                     "irq_count=0"},
                          "root_cause": "", "fix": "",
                          "regression_evidence": "", "attempts": 0,
                          "history": []}]}), encoding="utf-8")
        verdict = {"status": "done", "circuit": "platform",
                   "action": "park", "fix": {"gap": "INTX-DELIVERY"},
                   "evidence": [{"file": "<l4-log>", "line": 0,
                                 "quote": "icr=0x14 但 irq_count=0"}],
                   "summary": "平台缺口——泊车 + 上游登记",
                   "confidence": 0.85}
        with _NoAgentOff(), mock.patch.object(
                EL.agent, "run_agent",
                return_value=_canned(verdict)):
            rc = P6.diagnose_defect(w.ws, "INTX-DELIVERY")
        ok("M3a 求解泊车 rc=0", rc == 0)
        d = json.loads((w.ws / "defects.json").read_text(
            encoding="utf-8"))["defects"][0]
        ok("M3b defects history 落账 + 泊车",
           any(h.get("event") == "solve-round" for h in d["history"])
           and "parked" in [h.get("event") for h in d["history"]]
           and d["status"] == "parked")
        pp = json.loads((w.ws / "platform_patches.json").read_text(
            encoding="utf-8"))
        ok("M3c platform_patches 登记",
           any(p["gap"] == "INTX-DELIVERY" for p in pp["patches"]))

    def test_m3b_defect_diagnose_missing(self):
        tmp = Path(tempfile.mkdtemp(prefix="porter_m3b_"))
        w = _Ws(tmp, "ws")
        ok("M3d 缺陷不存在 rc=2",
           P6.diagnose_defect(w.ws, "NOPE") == 2)


if __name__ == "__main__":
    unittest.main()
