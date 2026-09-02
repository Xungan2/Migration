"""三挂载端到端最小单测（§15 Phase 6：同一 triage/diagnose 内核三处复用）。

M1 挂载① p5.run_p5：注入 docker 锁失败（boot 日志）→ 快照 → SIG-01
   infra 分诊 → 有界重跑（现场自愈注入）→ PASS 收尾（不计 attempts 的
   重试在 run_p5 内部消化，loop 不感知）
M2 挂载② p6.execute：注入 boot/判据双红项 → 快照 + 红项分诊入 health
   报告（triage 节）
M3 挂载③ p6.diagnose_defect：注入 INTX-DELIVERY 型缺陷 → SIG-06 泊车 +
   platform_patches 登记 + defects history 落账 + 考古包
全部 PORTER_NO_AGENT=1（规则路径即命中，不真调 opencode）。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def setUpModule():
    os.environ.setdefault("PORTER_NO_AGENT", "1")
    # §15 三挂载测试：强制开自诊（仓级 config 默认 bypass）
    os.environ.setdefault("PORTER_SELF_DIAGNOSIS", "1")


def tearDownModule():
    os.environ.pop("PORTER_NO_AGENT", None)
    os.environ.pop("PORTER_SELF_DIAGNOSIS", None)


from porter.loop import events as EV
from porter.loop import p5 as P5
from porter.loop import p6 as P6


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


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
    def test_m1_p5_infra_rerun_selfheal(self):
        tmp = Path(tempfile.mkdtemp(prefix="porter_m1_"))
        w = _Ws(tmp, "ws")
        w.criteria([{"id": "modA.c1", "layer": "L3",
                     "kind": "log_pattern", "expr": "OK",
                     "deferred_by": None}])
        rc = P5.run_p5(w.ws, "modA", ["modA"])
        acc = json.loads((w.ws / "P5" / "modA" / "reports" /
                          "acceptance.json").read_text(encoding="utf-8"))
        ok("M1a infra 自愈后 PASS", rc == 0 and acc["pass"] is True)
        ok("M1b triage 节在档", len(acc.get("triage") or []) == 2
           and acc["triage"][0]["circuit"] == "infra"
           and acc["triage"][0]["rule_id"] == "SIG-01")
        evs = EV.read_events(w.ws)
        kinds = [e["kind"] for e in evs]
        ok("M1c 快照先于重跑", "snapshot" in kinds
           and kinds.index("snapshot") < len(kinds) - 1
           and kinds.count("cmd_start") >= 4)      # ≥2 轮 build+boot
        ok("M1d triage 落账", "triage" in kinds)
        ok("M1e 快照实物", (w.ws / "failure-snapshot-1" /
                            "manifest.json").exists())
        rep_md = (w.ws / "P5" / "modA" / "reports" / "report.md") \
            .read_text(encoding="utf-8")
        ok("M1f report.md 分诊节", "分诊（§15 挂载①）" in rep_md
           and "SIG-01" in rep_md)

    def test_m1b_p5_migration_no_rerun(self):
        tmp = Path(tempfile.mkdtemp(prefix="porter_m1b_"))
        w = _Ws(tmp, "ws")
        # 非 infra 且无自动修正 → 不重跑（一轮即收）：compile 恒 FAIL 的
        # 判据判 migration（boot 自产日志——新日志面语义下预置文件会被
        # probe 的清旧日志删除，属 missing 而非本测场景）
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
        rc = P5.run_p5(w.ws, "modA", ["modA"])
        acc = json.loads((w.ws / "P5" / "modA" / "reports" /
                          "acceptance.json").read_text(encoding="utf-8"))
        ok("M1g migration 判定不重跑",
           rc == 1 and acc["triage"][0]["circuit"] == "migration")
        evs = EV.read_events(w.ws)
        ok("M1h 仅一轮 build", sum(1 for e in evs
                                    if e["kind"] == "cmd_start"
                                    and str(e.get("cmd")).startswith(
                                        "false")) == 1)


class MountP6Test(unittest.TestCase):
    def test_m2_p6_execute_red_triage(self):
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
        rc = P6.execute(w.ws)
        ok("M2a 红项判定 FAIL", rc == 1)
        health = json.loads((w.ws / "P6" / "reports" / "health.json")
                            .read_text(encoding="utf-8"))
        tri = health.get("triage") or []
        ok("M2b 红项分诊入 health", len(tri) >= 2
           and all(v["circuit"] == "infra" and v["rule_id"] == "SIG-01"
                   for v in tri))
        ok("M2c 快照在场", (w.ws / "failure-snapshot-1" /
                             "manifest.json").exists())
        md = (w.ws / "P6" / "reports" / "health.md").read_text(
            encoding="utf-8")
        ok("M2d health.md 分诊节", "红项分诊（§15 挂载②）" in md)

    def test_m3_defect_diagnose(self):
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
        rc = P6.diagnose_defect(w.ws, "INTX-DELIVERY")
        ok("M3a rc=0（泊车非停车）", rc == 0)
        d = json.loads((w.ws / "defects.json").read_text(
            encoding="utf-8"))["defects"][0]
        ok("M3b defects history 落账",
           [h["event"] for h in d["history"]][:1] == ["triaged"]
           and "parked" in [h["event"] for h in d["history"]]
           and d["status"] == "parked")
        pp = json.loads((w.ws / "platform_patches.json").read_text(
            encoding="utf-8"))
        ok("M3c platform_patches 登记",
           any(p["gap"] == "INTX-DELIVERY" for p in pp["patches"]))
        packs = list((w.ws / "escalations").glob("context-pack-*.json"))
        ok("M3d 考古包产出", len(packs) == 1)

    def test_m3b_defect_diagnose_missing(self):
        tmp = Path(tempfile.mkdtemp(prefix="porter_m3b_"))
        w = _Ws(tmp, "ws")
        ok("M3e 缺陷不存在 rc=2",
           P6.diagnose_defect(w.ws, "NOPE") == 2)


if __name__ == "__main__":
    unittest.main()
