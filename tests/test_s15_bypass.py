"""错误处理模块开关/守卫/挂载行为测试（§15 重设计后语义）。

覆盖：
A. self_diagnosis_enabled：config 缺省 true（直接生效）/
   PORTER_SELF_DIAGNOSIS=1 强制开
B. 熔断关（self_diagnosis.enabled=false）下 diagnose_defect /
   fix_defect 入口守卫（rc 2 + 提示）
C. 熔断关下 p5 快速断路：失败不求解 rc 1（走 attempts 旧人工路径）
D. 开关开 + PORTER_NO_AGENT → 求解降级只出报告 → p5.unsolved 关口 rc 3
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
            ok("A1 config 缺省 = true（重设计后直接生效）",
               G.self_diagnosis_enabled() is True)
            os.environ["PORTER_SELF_DIAGNOSIS"] = "1"
            ok("A2 env 强制开（冗余但保惯例）",
               G.self_diagnosis_enabled() is True)


class GuardTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="porter_byp_t_"))
        self.ws = tmp / "ws"
        self.ws.mkdir()
        (self.ws / "project.json").write_text(
            json.dumps({"target_os": str(tmp)}), encoding="utf-8")

    def test_b_guards_fuse_off(self):
        with _SdEnv(), mock.patch.object(
                G, "self_diagnosis_enabled", return_value=False):
            ok("B1 diagnose_defect rc 2",
               P6.diagnose_defect(self.ws, "SOME-ID") == 2)
            ok("B2 fix_defect（重定向）rc 2",
               P6.fix_defect(self.ws, "SOME-ID") == 2)

    def test_c_p5_fast_break_fuse_off(self):
        ws = self._mk_p5_ws()
        old_na = os.environ.pop("PORTER_NO_AGENT", None)
        try:
            with _SdEnv(), mock.patch.object(
                    G, "self_diagnosis_enabled", return_value=False):
                rc = P5.run_p5(ws, "modA", ["modA"])
        finally:
            if old_na is not None:
                os.environ["PORTER_NO_AGENT"] = old_na
        acc = json.loads((ws / "P5" / "modA" / "reports" /
                          "acceptance.json").read_text(encoding="utf-8"))
        ok("C1 熔断下 rc 1（走 attempts 旧人工路径）", rc == 1)
        ok("C2 solve 节为空（无求解）", acc.get("solve") == [])
        ok("C3 判定照常进行（compile/boot 结果在档）",
           any(r["id"] == "modA.compile" and r["ok"] is False
               for r in acc["results"]))

    def test_d_p5_no_agent_report_gate(self):
        # 开关开 + PORTER_NO_AGENT=1 → 求解降级为只出报告 → 关口 rc 3
        ws = self._mk_p5_ws()
        old_na = os.environ.pop("PORTER_NO_AGENT", None)
        os.environ["PORTER_NO_AGENT"] = "1"
        try:
            with _SdEnv():
                rc = P5.run_p5(ws, "modA", ["modA"])
        finally:
            os.environ.pop("PORTER_NO_AGENT", None)
            if old_na is not None:
                os.environ["PORTER_NO_AGENT"] = old_na
        acc = json.loads((ws / "P5" / "modA" / "reports" /
                          "acceptance.json").read_text(encoding="utf-8"))
        ok("D1 no-agent → rc 3（关口）", rc == 3)
        ok("D2 solve 节为空（零 agent 轮）", acc.get("solve") == [])
        gates_doc = json.loads((ws / "gates.json").read_text(
            encoding="utf-8"))
        ok("D3 p5.unsolved 关口登记",
           any(g["id"] == "p5.unsolved.modA"
               and g["status"] in ("open", "invalid")
               for g in gates_doc["gates"]))
        ok("D4 升级报告在场",
           any((ws / "escalations").glob("modA-*.json")))

    def _mk_p5_ws(self) -> Path:
        tos = self.ws.parent / "tos2"
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
        p3r.mkdir(parents=True, exist_ok=True)
        (p3r / "criteria.json").write_text(json.dumps(
            {"criteria": [{"id": "modA.c1", "layer": "L3",
                           "kind": "log_pattern", "expr": "OK",
                           "deferred_by": None}]}), encoding="utf-8")
        (self.ws / "P4" / "modA" / "reports").mkdir(parents=True,
                                                    exist_ok=True)
        (self.ws / "P4" / "modA" / "reports" / "migration.json") \
            .write_text("{}", encoding="utf-8")
        (self.ws / "P1" / "modules").mkdir(parents=True, exist_ok=True)
        (self.ws / "P1" / "modules" / "deps.json").write_text(
            json.dumps({"order": ["modA"], "edges": {}}), encoding="utf-8")
        (self.ws / "loop_state.json").write_text(json.dumps(
            {"order": ["modA"],
             "modules": {"modA": {"phase": "p5",
                                  "attempts": {"p3": 0, "p4": 0,
                                               "p5": 0}}}}),
            encoding="utf-8")
        return self.ws


if __name__ == "__main__":
    unittest.main()
