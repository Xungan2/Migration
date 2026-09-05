"""scope 消费侧（批次 2b+3）的功能性测试（无 agent）。

覆盖：
  1. divide 白名单过滤：scope 在场 → 只分配闭包内文件（stub _assign_one_file
     断言被分配文件）；空交集 → rc 2；无 scope → 全分配（兼容）
  2. CP1 双指纹：scope 在场 → 联合指纹（context_files 含 scope.json）；
     批准后改 scope.json → rc 3 重审；无 scope 工作区行为不变
  3. 知识指纹：_draft_to_temp 的 linux_files 用 scope 并集
  4. 噪音基线：_orig_driver_defs/_orig_defs 的 scope 过滤

运行：python3 tests/test_scope_consumers.py
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.loop import gates as G


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def _mk_drv(tmp: Path, names: tuple[str, ...] = ("a.c", "b.c")) -> Path:
    drv = tmp / "drv"
    drv.mkdir(parents=True)
    for n in names:
        fn = n.replace(".c", "_fn").replace(".h", "_fn")
        (drv / n).write_text(
            f"int {fn}(void)\n{{\n\treturn 0;\n}}\n", encoding="utf-8")
    return drv


def _ws_with_strategy(tmp: Path, scope_files: list[str] | None) -> Path:
    """最小工作区：P1/strategy.md（+可选 P1/scope.json）+ knowledge/。

    knowledge/ 必须存在——否则 kb temp_root 回退到全局共享草稿区，
    测试会污染工具仓 knowledge/temp。
    """
    ws = tmp / "ws"
    (ws / "P1").mkdir(parents=True)
    (ws / "knowledge").mkdir()
    (ws / "P1" / "strategy.md").write_text("# 策略\n" + "分析正文。 " * 200,
                                           encoding="utf-8")
    if scope_files is not None:
        (ws / "P1" / "scope.json").write_text(json.dumps(
            {"modules": [{"name": "m", "function": "m",
                          "files": scope_files}]}, ensure_ascii=False),
            encoding="utf-8")
    return ws


class TestDivideFilter(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="scope_div_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.drv = _mk_drv(self.tmp)

    def _run(self, ws: Path) -> tuple[int, list[str]]:
        from porter.divide import run as DR
        called: list[str] = []

        def fake_assign(skill, strategy, fname, entries, p1):
            called.append(fname)
            return ({"whole_file": "m"}, {})   # 最简合法 decision，走完全程

        with mock.patch.object(DR, "_assign_one_file", fake_assign):
            rc = DR.run_divide(ws, self.drv)
        return rc, called

    def test_a_scope_filters_assignment(self):
        ws = _ws_with_strategy(self.tmp / "case-a", ["a.c"])
        rc, called = self._run(ws)
        ok("A1 只分配闭包内文件", called == ["a.c"], str(called))
        ok("A2 全部分配完成后 rc 0", rc == 0)

    def test_b_empty_intersection_rc2(self):
        ws = _ws_with_strategy(self.tmp / "case-b", ["ghost.c"])
        rc, called = self._run(ws)
        ok("B1 白名单与索引无交集 → rc 2", rc == 2 and called == [])

    def test_c_no_scope_compat(self):
        ws = _ws_with_strategy(self.tmp / "case-c", None)
        _rc, called = self._run(ws)
        ok("C1 无 scope → 全文件分配", sorted(called) == ["a.c", "b.c"],
           str(called))


class TestCP1DualFingerprint(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="scope_cp1_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _approve(self, ws: Path):
        ledger = G.GateLedger(ws).load()
        gate = ledger.find("cp1.strategy")
        gate["status"] = "applied"
        gate["answer"] = {"verdict": "approve"}
        gate["answered_by"] = "tester"
        ledger.save()

    def test_d_dual_fingerprint(self):
        ws = _ws_with_strategy(self.tmp / "case-d", ["a.c"])
        rc = G.strategy_checkpoint(ws)
        ok("D1 首审停车 rc 3", rc == 3)
        ledger = json.loads((ws / "gates.json").read_text(encoding="utf-8"))
        gate = [g for g in ledger["gates"] if g["id"] == "cp1.strategy"][0]
        ok("D2 context_files 含 scope.json",
           "P1/scope.json" in gate["context_files"])
        ok("D3 问题文本含范围审阅提示", "范围闭包" in gate["question"])
        self._approve(ws)
        ok("D4 批准后放行 rc 0", G.strategy_checkpoint(ws) == 0)
        (ws / "P1" / "scope.json").write_text(
            '{"modules": [{"name": "m", "function": "m", "files": ["b.c"]}]}',
            encoding="utf-8")
        ok("D5 编辑 scope.json → 指纹失效重审 rc 3",
           G.strategy_checkpoint(ws) == 3)

    def test_e_no_scope_unchanged(self):
        ws = _ws_with_strategy(self.tmp / "case-e", None)
        ok("E1 无 scope 首审停车 rc 3", G.strategy_checkpoint(ws) == 3)
        self._approve(ws)
        ok("E2 批准后放行 rc 0", G.strategy_checkpoint(ws) == 0)
        (ws / "P1" / "strategy.md").write_text("# 改动\n" + "x " * 300,
                                               encoding="utf-8")
        ok("E3 编辑 strategy.md → 重审 rc 3（原行为保持）",
           G.strategy_checkpoint(ws) == 3)


class TestKnowledgeFingerprint(unittest.TestCase):

    def test_f_linux_files_uses_scope(self):
        from porter.divide import strategy as ST
        tmp = Path(tempfile.mkdtemp(prefix="scope_kb_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        drv = _mk_drv(tmp, ("a.c", "b.c", "c.c"))
        ws = _ws_with_strategy(tmp / "ws", ["a.c", "b.c"])
        proj = {"linux_driver": str(drv)}
        st_path = ws / "P1" / "strategy.md"
        res = ST._draft_to_temp(ws, proj, drv, st_path)
        ok("F1 草稿已写入", res["status"].startswith("已写入"), str(res))
        idx = json.loads(
            (ws / "knowledge" / "temp" / "splits" / "strategies"
             / "INDEX.json").read_text(encoding="utf-8"))
        ok("F2 linux_files = scope 并集（非全目录）",
           idx and idx[-1]["linux_files"] == ["a.c", "b.c"],
           json.dumps(idx, ensure_ascii=False))

    def test_g_linux_files_no_scope_compat(self):
        from porter.divide import strategy as ST
        tmp = Path(tempfile.mkdtemp(prefix="scope_kb2_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        drv = _mk_drv(tmp, ("a.c", "b.c"))
        ws = _ws_with_strategy(tmp / "ws", None)
        proj = {"linux_driver": str(drv)}
        res = ST._draft_to_temp(ws, proj, drv, ws / "P1" / "strategy.md")
        ok("G1 无 scope → 目录全文件指纹",
           res.get("linux_files") == ["a.c", "b.c"], str(res))


class TestNoiseBaseline(unittest.TestCase):

    def test_h_orig_defs_scope_filter(self):
        from porter.bootstrap import extract_spine as ES
        from porter.loop import surface as SF
        tmp = Path(tempfile.mkdtemp(prefix="scope_noise_"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        drv = _mk_drv(tmp, ("a.c", "b.c"))
        full = ES._orig_driver_defs(drv)
        ok("H1 无 scope 全扫", {"a_fn", "b_fn"} <= full)
        sub = ES._orig_driver_defs(drv, scope={"a.c"})
        ok("H2 extract_spine scope 过滤",
           "a_fn" in sub and "b_fn" not in sub)
        sub2 = SF._orig_defs(drv, scope={"a.c"})
        ok("H3 surface scope 过滤", "a_fn" in sub2 and "b_fn" not in sub2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
