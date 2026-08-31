"""porter/loop/p7.py 单元测试（无 git / 无网络；unittest 形态）。

覆盖（P7 终态报告工具地基）：
M. platform_patches 台账：登记（proposed+doc 指针）/ 状态流转（非法值
   拒绝）/ 不存在拒绝 / 持久化
N. baseline_diff：tracked+untracked 合并 / 分组（driver-crate /
   workspace-wiring / other）/ target·log·pcap 过滤（_git_lines 打桩）
O. 聚合：final_report.json/.md 落盘（流水线/映射/L4/账本/patches 面）/
   crate 统计 / degraded 容忍
运行：python3 tests/test_p7.py 或 unittest discover
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.loop import p7 as P7


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def _mk_p7_ws() -> tuple[Path, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="porter_p7_t_"))
    ws = tmp / "ws"
    tos = tmp / "os" / "kernel" / "core" / "comps" / "e1000"
    tos.mkdir(parents=True)
    (tos / "lib.rs").write_text("fn a() {}\nfn b() {}\n", encoding="utf-8")
    (tos / "hw.rs").write_text("#[ktest]\nfn t() {}\n", encoding="utf-8")
    (ws / "P2").mkdir(parents=True)
    (ws / "P3" / "m1" / "reports").mkdir(parents=True)
    (ws / "P6" / "reports").mkdir(parents=True)
    (ws / "P5" / "m1" / "reports").mkdir(parents=True)
    (ws / "project.json").write_text(json.dumps({
        "target_os": str(tmp / "os"), "linux_driver": str(tmp / "drv/e1000"),
        "target_os_baseline": {"baseline_commit": "36ae7fe"},
        "category": ["net"]}), encoding="utf-8")
    (ws / "loop_state.json").write_text(json.dumps({
        "order": ["m1"],
        "modules": {"m1": {"phase": "done", "attempts": {}}}}),
        encoding="utf-8")
    (ws / "P5" / "m1" / "reports" / "acceptance.json").write_text(
        json.dumps({"module": "m1", "pass": True}), encoding="utf-8")
    (ws / "P2" / "mapping.json").write_text(json.dumps({
        "entries": [
            {"linux_api": "a", "verdict": "direct", "origin": "P2a",
             "risk": "low"},
            {"linux_api": "b", "verdict": "adapt", "origin": "P3m1",
             "risk": "med"}],
        "redesigns": [{"x": 1}], "wiring": [{"y": 2}]}), encoding="utf-8")
    (ws / "P6" / "reports" / "l4_criteria.json").write_text(json.dumps({
        "status": "finalized",
        "criteria": [
            {"id": "c1", "disposition": "clear"},
            {"id": "c2", "disposition": "park"}]}), encoding="utf-8")
    (ws / "P6" / "reports" / "health.json").write_text(json.dumps({
        "mode": "execute",
        "verdict": {"all_green_except_parked": True, "parked": ["c2"]}}),
        encoding="utf-8")
    (ws / "deferred.json").write_text(json.dumps({"entries": [
        {"id": "d1", "status": "cleared"},
        {"id": "d2", "status": "open"}]}), encoding="utf-8")
    (ws / "defects.json").write_text(json.dumps({"defects": [
        {"id": "X", "status": "fixed"},
        {"id": "Y", "status": "parked"}]}), encoding="utf-8")
    (ws / "platform_patches.json").write_text(json.dumps({"patches": [
        {"gap": "msleep", "module": "hw", "status": "planned"}]}),
        encoding="utf-8")
    return ws, tmp / "os"


# ---------- M. patches 台账 ----------

class TestPatches(unittest.TestCase):
    def test_patches(self):
        print("M. platform_patches 台账")
        ws, _tos = _mk_p7_ws()
        e = P7.register_patch(ws, "ioapic-level", "OSTD level 触发变体",
                              "加法式扩展")
        ok("登记 proposed + doc 指针",
           e["status"] == "proposed"
           and e["doc"] == "P7/reports/patches/ioapic-level.md")
        try:
            P7.register_patch(ws, "ioapic-level", "dup", "")
            raise SystemExit("应抛 ValueError")
        except ValueError:
            ok("重复登记拒绝", True)
        e2 = P7.set_patch_status(ws, "msleep", "closed",
                                 note="bypass 已终态：TSC 忙等为定案")
        ok("状态流转 + 处置理由入档",
           e2["status"] == "closed" and "终态" in e2["closed_note"])
        try:
            P7.set_patch_status(ws, "msleep", "dreaming")
            raise SystemExit("应抛 ValueError")
        except ValueError as ex:
            ok("非法状态拒绝", "非法状态" in str(ex))
        try:
            P7.set_patch_status(ws, "nope", "closed")
            raise SystemExit("应抛 ValueError")
        except ValueError:
            ok("不存在拒绝", True)
        saved = P7.load_patches(ws)
        ok("持久化", {p["gap"] for p in saved}
           == {"msleep", "ioapic-level"})
        shutil.rmtree(ws.parent)


# ---------- N. baseline diff ----------

class TestBaselineDiff(unittest.TestCase):
    def test_diff(self):
        print("N. baseline diff 分组与合并")
        ws, tos = _mk_p7_ws()
        saved = P7._git_lines
        fake_out = [
            "M\tCargo.toml",
            "M\tkernel/core/src/net/iface/init.rs",
            "M\tkernel/core/comps/e1000/src/lib.rs",
            "M\tkernel/some/legacy.rs",
        ]

        def fake_git(_tos, args):
            if args[:2] == ["diff", "--name-status"]:
                return fake_out
            if args[:2] == ["ls-files", "--others"]:
                return ["kernel/core/comps/e1000/src/l4_selftest.rs",
                        "target/junk.o", "asterinas.log", "dump.pcap",
                        "docs/new.md"]
            return []
        P7._git_lines = fake_git
        try:
            d = P7.baseline_diff(tos, "36ae7fe", "e1000")
        finally:
            P7._git_lines = saved
        g = {f["path"]: f for f in d["files"]}
        ok("crate 内 tracked 归 driver-crate",
           g["kernel/core/comps/e1000/src/lib.rs"]["group"] == "driver-crate")
        ok("crate 内 untracked 并入（status=A）",
           g["kernel/core/comps/e1000/src/l4_selftest.rs"]["status"] == "A"
           and g["kernel/core/comps/e1000/src/l4_selftest.rs"]["group"]
           == "driver-crate")
        ok("接线面归 workspace-wiring",
           g["Cargo.toml"]["group"] == "workspace-wiring"
           and g["kernel/core/src/net/iface/init.rs"]["group"]
           == "workspace-wiring")
        ok("其余归 other", g["kernel/some/legacy.rs"]["group"] == "other")
        ok("target/log/pcap 过滤",
           not any(p in g for p in ("target/junk.o", "asterinas.log",
                                    "dump.pcap")))
        ok("分组计数", d["groups"] == {"driver-crate": 2,
                                       "workspace-wiring": 2, "other": 2})
        shutil.rmtree(ws.parent)


# ---------- O. 聚合 ----------

class TestAggregate(unittest.TestCase):
    def test_run_p7(self):
        print("O. 聚合 final_report")
        ws, tos = _mk_p7_ws()
        P7.register_patch(ws, "ioapic-level", "OSTD level 触发变体", "")
        saved = P7._git_lines
        P7._git_lines = lambda _t, _a: []
        try:
            rc = P7.run_p7(ws)
        finally:
            P7._git_lines = saved
        ok("rc=0", rc == 0)
        r = json.loads((ws / "P7" / "reports" / "final_report.json")
                       .read_text(encoding="utf-8"))
        ok("流水线面", r["pipeline"]["phase_done"] == 1
           and r["pipeline"]["acceptance_pass"] == 1)
        ok("crate 统计", r["crate"] == {"files": 2, "lines": 4,
                                        "ktests": 1})
        ok("映射面", r["mapping"]["total"] == 2
           and r["mapping"]["by_verdict"] == {"direct": 1, "adapt": 1})
        ok("L4 面", r["l4"]["clear"] == 1 and r["l4"]["park"] == 1)
        ok("P6 判定透传", r["p6_verdict"]["all_green_except_parked"] is True)
        ok("账本面", r["deferred"]["open_ids"] == ["d2"]
           and r["defects"]["parked_ids"] == ["Y"]
           and r["patches"]["by_status"]["proposed"] == ["ioapic-level"])
        ok("degraded 容忍", r["baseline"]["degraded"] is True)
        md = (ws / "P7" / "reports" / "final_report.md").read_text(
            encoding="utf-8")
        ok("md 骨架 + 人工撰写区", "## 结论与去向（人工撰写区）" in md
           and "36ae7fe" in md)
        shutil.rmtree(ws.parent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
