"""driver_name 身份层的测试（无 agent）。

覆盖（身份层：scope 提案 → CP1 人审 → project.json 同步 → 下游消费）：
  1. validate_and_normalize：driver_name 有/缺/非 kebab 三分支；规范化回写含之
  2. load_driver_name / sync_driver_name：读、同步、幂等、改 scope 后重同步
  3. driver_name_of：显式优先 / 缺省回退目录名
  4. CP1 同步：批准路径把 scope 的 driver_name 写入 project.json
  5. P4 crate 落点：manifest.driver_home 优先 / 缺 manifest 回退
     comps/<driver_name>

运行：python3 tests/test_driver_name.py
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.common import scope as SC
from porter.loop import gates as G


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def _mk_drv(tmp: Path) -> Path:
    drv = tmp / "md"
    drv.mkdir(parents=True)
    (drv / "dm.c").write_text("int dm_fn(void)\n{\n\treturn 0;\n}\n",
                              encoding="utf-8")
    return drv


def _ws(tmp: Path, scope_dict: dict | None, proj_extra: dict | None = None) -> Path:
    ws = tmp / "ws"
    (ws / "P1").mkdir(parents=True)
    (ws / "knowledge").mkdir()
    (ws / "P1" / "strategy.md").write_text("# 策略\n" + "正文。 " * 200,
                                           encoding="utf-8")
    proj = {"linux_driver": str(_mk_drv(tmp)), "target_os": str(tmp / "tos")}
    proj.update(proj_extra or {})
    (ws / "project.json").write_text(json.dumps(proj), encoding="utf-8")
    if scope_dict is not None:
        (ws / "P1" / "scope.json").write_text(
            json.dumps(scope_dict, ensure_ascii=False), encoding="utf-8")
    return ws


def _sc(dn=None, files=("dm.c",)):
    d = {"modules": [{"name": "m", "function": "m", "files": list(files)}]}
    if dn is not None:
        d["driver_name"] = dn
    return d


class TestValidate(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dn_val_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.drv = _mk_drv(self.tmp)
        self.ws = self.tmp / "ws"
        (self.ws / "P1").mkdir(parents=True)

    def test_a_branches(self):
        ok("A1 合法（含 driver_name）",
           SC.validate_and_normalize(_sc("dm-zero"), self.drv, self.ws) == [])
        out = json.loads((self.ws / "P1" / "scope.json")
                         .read_text(encoding="utf-8"))
        ok("A2 规范化回写含 driver_name", out.get("driver_name") == "dm-zero")
        d = SC.validate_and_normalize(_sc(None), self.drv, self.ws)
        ok("A3 缺失 → 缺陷", any("driver_name" in x for x in d), str(d))
        d = SC.validate_and_normalize(_sc("Dm_Zero"), self.drv, self.ws)
        ok("A4 非 kebab → 缺陷", any("kebab" in x for x in d), str(d))
        d = SC.validate_and_normalize(_sc("drivers/md"), self.drv, self.ws)
        ok("A5 路径形式拒绝", any("kebab" in x for x in d), str(d))


class TestSync(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dn_sync_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_b_sync_idempotent(self):
        ws = _ws(self.tmp, _sc("dm-zero"))
        ok("B1 load_driver_name 读取", SC.load_driver_name(ws) == "dm-zero")
        ok("B2 sync 写入 project.json", SC.sync_driver_name(ws) is True)
        proj = json.loads((ws / "project.json").read_text(encoding="utf-8"))
        ok("B3 字段就位", proj.get("driver_name") == "dm-zero")
        ok("B4 幂等（二次 sync 无写入）", SC.sync_driver_name(ws) is False)
        (ws / "P1" / "scope.json").write_text(
            json.dumps(_sc("dm-zero-full")), encoding="utf-8")
        ok("B5 改 scope 后重同步", SC.sync_driver_name(ws) is True)
        proj = json.loads((ws / "project.json").read_text(encoding="utf-8"))
        ok("B6 跟随新值", proj.get("driver_name") == "dm-zero-full")

    def test_c_driver_name_of(self):
        ws = _ws(self.tmp, _sc("dm-zero"))
        SC.sync_driver_name(ws)
        proj = json.loads((ws / "project.json").read_text(encoding="utf-8"))
        ok("C1 显式优先", SC.driver_name_of(proj) == "dm-zero")
        ws2 = _ws(self.tmp / "case2", None)
        proj2 = json.loads((ws2 / "project.json").read_text(encoding="utf-8"))
        ok("C2 缺省回退目录名", SC.driver_name_of(proj2) == "md")


class TestCP1AndCrate(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dn_cp1_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _approve(self, ws: Path):
        ledger = G.GateLedger(ws).load()
        gate = ledger.find("cp1.strategy")
        gate["status"] = "applied"
        gate["answer"] = {"verdict": "approve"}
        ledger.save()

    def test_d_cp1_syncs_driver_name(self):
        ws = _ws(self.tmp, _sc("dm-zero"))
        ok("D1 CP1 首审停车", G.strategy_checkpoint(ws) == 3)
        proj = json.loads((ws / "project.json").read_text(encoding="utf-8"))
        ok("D2 批准前 project 未同步", "driver_name" not in proj)
        self._approve(ws)
        ok("D3 批准放行", G.strategy_checkpoint(ws) == 0)
        proj = json.loads((ws / "project.json").read_text(encoding="utf-8"))
        ok("D4 批准后 driver_name 同步", proj.get("driver_name") == "dm-zero")

    def test_e_crate_rel(self):
        from porter.loop import p4 as P4
        ws = _ws(self.tmp, None, proj_extra={"driver_name": "dm-zero"})
        ok("E1 无 manifest → 回退 comps/<driver_name>",
           P4._crate_rel(ws, json.loads(
               (ws / "project.json").read_text(encoding="utf-8")))
           == "kernel/core/comps/dm-zero")
        (ws / "P2" / "reports").mkdir(parents=True)
        (ws / "P2" / "reports" / "scaffold_manifest.json").write_text(
            json.dumps({"driver_home": "kernel/core/comps/md"}),
            encoding="utf-8")
        ok("E2 manifest.driver_home 优先（发现式真值）",
           P4._crate_rel(ws, json.loads(
               (ws / "project.json").read_text(encoding="utf-8")))
           == "kernel/core/comps/md")


if __name__ == "__main__":
    unittest.main(verbosity=2)
