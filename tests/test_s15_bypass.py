"""§15 bypass 行为测试（A 项：self_diagnosis 总开关，默认关）。

覆盖：
A. self_diagnosis_enabled：config 缺省 false / PORTER_SELF_DIAGNOSIS=1 强制开
B. diagnose_defect / fix_defect 入口守卫（bypass 下 rc 2 + 提示）
C. p5 快速断路：bypass 下失败不分诊不重跑（triage 空、rc 1 走 attempts）
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.loop import gates as G
from porter.loop import p5 as P5
from porter.loop import p6 as P6


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


class _SdEnv:
    """PORTER_SELF_DIAGNOSIS 环境守护（测后还原）。"""

    def __enter__(self):
        self._old = os.environ.pop("PORTER_SELF_DIAGNOSIS", None)
        return self

    def __exit__(self, *a):
        if self._old is not None:
            os.environ["PORTER_SELF_DIAGNOSIS"] = self._old


class SwitchTest(unittest.TestCase):
    def test_a_switch(self):
        with _SdEnv():
            ok("A1 config 缺省 = bypass（false）",
               G.self_diagnosis_enabled() is False)
            os.environ["PORTER_SELF_DIAGNOSIS"] = "1"
            ok("A2 env 强制开", G.self_diagnosis_enabled() is True)


class GuardTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="porter_byp_t_"))
        self.ws = tmp / "ws"
        self.ws.mkdir()
        (self.ws / "project.json").write_text(
            json.dumps({"target_os": str(tmp)}), encoding="utf-8")

    def test_b_guards(self):
        with _SdEnv():
            ok("B1 diagnose_defect rc 2",
               P6.diagnose_defect(self.ws, "SOME-ID") == 2)
            ok("B2 fix_defect rc 2",
               P6.fix_defect(self.ws, "SOME-ID") == 2)

    def test_c_p5_fast_break(self):
        # 最小 p5 工作区：compile 恒败 + boot 自产日志（避免 missing 干扰）
        tos = self.ws.parent / "tos"
        tos.mkdir(exist_ok=True)
        (self.ws / "project.json").write_text(json.dumps(
            {"target_os": str(tos), "linux_driver": "/drv",
             "category": ["net"]}), encoding="utf-8")
        (self.ws / "runner.json").write_text(json.dumps({
            "build": {"cmd": "false", "timeout_full_sec": 5,
                      "success_pattern": ""},
            "boot": {"cmd": "echo OK > qemu.log", "timeout_sec": 5,
                     "log_file": "qemu.log", "success_pattern": "OK",
                     "panic_pattern": "panic"},
            "inject_device": {"mechanism": "env",
                              "env": {"EXTRA_QEMU_ARGS": "<DEVICE_ARGS>"},
                              "example_args": {"net": "-device e1000"}},
            "unit_test": {"mechanism": "none"}}), encoding="utf-8")
        p3r = self.ws / "P3" / "modA" / "reports"
        p3r.mkdir(parents=True)
        (p3r / "criteria.json").write_text(json.dumps(
            {"criteria": [{"id": "modA.c1", "layer": "L3",
                           "kind": "log_pattern", "expr": "OK",
                           "deferred_by": None}]}), encoding="utf-8")
        (self.ws / "P4" / "modA" / "reports").mkdir(parents=True)
        (self.ws / "P4" / "modA" / "reports" / "migration.json") \
            .write_text("{}", encoding="utf-8")
        (self.ws / "P1" / "modules").mkdir(parents=True)
        (self.ws / "P1" / "modules" / "deps.json").write_text(
            json.dumps({"order": ["modA"], "edges": {}}), encoding="utf-8")
        (self.ws / "loop_state.json").write_text(json.dumps(
            {"order": ["modA"],
             "modules": {"modA": {"phase": "p5",
                                  "attempts": {"p3": 0, "p4": 0,
                                               "p5": 0}}}}),
            encoding="utf-8")
        old_na = os.environ.pop("PORTER_NO_AGENT", None)
        try:
            with _SdEnv():
                rc = P5.run_p5(self.ws, "modA", ["modA"])
        finally:
            if old_na is not None:
                os.environ["PORTER_NO_AGENT"] = old_na
        acc = json.loads((self.ws / "P5" / "modA" / "reports" /
                          "acceptance.json").read_text(encoding="utf-8"))
        ok("C1 bypass 下 rc 1（走 attempts，非分诊消化）", rc == 1)
        ok("C2 triage 节为空（无自动分诊）", acc.get("triage") == [])
        ok("C3 判定照常进行（compile/boot 结果在档）",
           any(r["id"] == "modA.compile" and r["ok"] is False
               for r in acc["results"]))


if __name__ == "__main__":
    unittest.main()
