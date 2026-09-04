"""p1-import 子命令的功能性测试（无 agent）。

覆盖（A7：tool-p1-adaptation-plan §A7 + 两处顺修）：
  1. happy path：合成两模块驱动导入 → plan 规范位 + modules/ 重建 +
     module.json + deps.json（edges/order）
  2. 幂等：重复导入产物逐字节一致
  3. deps 对账：一致 rc=0 / order 篡改 rc=1（盘上为重算值）
  4. 环守卫：互引双文件构造环 → rc=1 且 deps.json 不落盘
  5. schema/前置守卫：空 modules / 缺 fragments / plan 文件不存在 → rc=2；
     src 不存在（形状合规）→ rc=1
  6. H6 顺修：run_divide 复用分支在 modules/ 缺失时按现存 plan 重建
  7. resolve 空图守卫：modules/ 缺失或为空 → rc=2（不写空图 deps.json）

运行：python3 tests/test_p1_import.py
"""
import sys, json, tempfile, shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import unittest


def ok(name, cond, extra=""):
    """断言助手：失败即抛（unittest discover / 直跑两用法通用）。"""
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def _mk_drv(tmp: Path) -> Path:
    """合成驱动：core.c（被依赖）+ leaf.c（引用 core_fn）+ 环构造对 ca/cb。

    符号名均 ≥3 字符（scan_file 引用过滤 <3 字符名）；原型语句
    `int core_fn(void);` 只入 protos 不入 defs/refs——依赖边来自 leaf
    函数体对 core_fn 的真实调用。
    """
    drv = tmp / "drv"
    drv.mkdir(parents=True)
    (drv / "core.c").write_text(
        "int core_fn(void)\n{\n\treturn 0;\n}\n", encoding="utf-8")
    (drv / "leaf.c").write_text(
        "int core_fn(void);\n\nint leaf_fn(void)\n{\n\treturn core_fn();\n}\n",
        encoding="utf-8")
    (drv / "ca.c").write_text(
        "int cb_fn(void);\n\nint ca_fn(void)\n{\n\treturn cb_fn();\n}\n",
        encoding="utf-8")
    (drv / "cb.c").write_text(
        "int ca_fn(void);\n\nint cb_fn(void)\n{\n\treturn ca_fn();\n}\n",
        encoding="utf-8")
    return drv


def _mod(name: str, drv: Path, fname: str) -> dict:
    n = len((drv / fname).read_text().splitlines())
    return {"name": name, "function": name, "files": [
        {"dest": fname, "src": fname,
         "fragments": [{"lines": f"1-{n}", "symbol": "(整文件)"}]}]}


def _snap(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


class TestP1Import(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="p1_import_"))
        self.drv = _mk_drv(self.tmp)
        self.plan = {"modules": [_mod("mod-core", self.drv, "core.c"),
                                 _mod("mod-leaf", self.drv, "leaf.c")]}
        self.plan_path = self.tmp / "plan.json"
        self.plan_path.write_text(json.dumps(self.plan), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_happy_and_idempotent(self):
        from porter.divide import import_p as IMP
        ws = self.tmp / "ws"
        (ws / "P1").mkdir(parents=True)
        rc = IMP.run_import(ws, self.drv, self.plan_path)
        ok("1a happy rc=0", rc == 0, f"rc={rc}")
        ok("1b modules 重建（module.json + 片段文件）",
           (ws / "P1/modules/mod-core/module.json").exists()
           and (ws / "P1/modules/mod-core/core.c").exists()
           and (ws / "P1/modules/mod-leaf/leaf.c").exists())
        mj = json.loads((ws / "P1/modules/mod-core/module.json")
                        .read_text(encoding="utf-8"))
        ok("1c module.json source_map", mj["name"] == "mod-core"
           and mj["source_map"]["core.c"]["fragments"] == ["1-4"], str(mj))
        deps = json.loads((ws / "P1/modules/deps.json").read_text(encoding="utf-8"))
        ok("1d edges", deps["edges"] == {"mod-leaf": ["mod-core"]}, str(deps["edges"]))
        ok("1e order", deps["order"] == ["mod-core", "mod-leaf"], str(deps["order"]))
        ok("1f plan 规范位", (ws / "P1/reports/P1D_plan.json").exists())
        snap1 = _snap(ws / "P1")
        rc2 = IMP.run_import(ws, self.drv, self.plan_path)
        ok("1g 幂等 rc=0", rc2 == 0, f"rc={rc2}")
        ok("1h 幂等产物逐字节一致", _snap(ws / "P1") == snap1)

    def test_deps_reconcile(self):
        from porter.divide import import_p as IMP
        good = {"modules": ["mod-leaf"],
                "edges": {"mod-leaf": ["mod-core"]},
                "order": ["mod-core", "mod-leaf"]}
        gp = self.tmp / "deps_good.json"
        gp.write_text(json.dumps(good), encoding="utf-8")
        ws = self.tmp / "ws-good"
        (ws / "P1").mkdir(parents=True)
        rc = IMP.run_import(ws, self.drv, self.plan_path, deps_path=gp)
        ok("2a deps 一致 rc=0", rc == 0, f"rc={rc}")

        bad = {"modules": ["mod-leaf"],
               "edges": {"mod-leaf": ["mod-core"]},
               "order": ["mod-leaf", "mod-core"]}
        bp = self.tmp / "deps_bad.json"
        bp.write_text(json.dumps(bad), encoding="utf-8")
        ws2 = self.tmp / "ws-bad"
        (ws2 / "P1").mkdir(parents=True)
        rc2 = IMP.run_import(ws2, self.drv, self.plan_path, deps_path=bp)
        ok("2b deps 不一致 rc=1", rc2 == 1, f"rc={rc2}")
        deps = json.loads((ws2 / "P1/modules/deps.json").read_text(encoding="utf-8"))
        ok("2c 盘上为重算值（以重算为准）",
           deps["order"] == ["mod-core", "mod-leaf"], str(deps["order"]))

    def test_cycle_guard(self):
        from porter.divide import import_p as IMP
        plan = {"modules": [_mod("mod-a", self.drv, "ca.c"),
                            _mod("mod-b", self.drv, "cb.c")]}
        pp = self.tmp / "plan_cycle.json"
        pp.write_text(json.dumps(plan), encoding="utf-8")
        ws = self.tmp / "ws-cyc"
        (ws / "P1").mkdir(parents=True)
        rc = IMP.run_import(ws, self.drv, pp)
        ok("3a 环 → rc=1", rc == 1, f"rc={rc}")
        ok("3b deps.json 不落盘",
           not (ws / "P1/modules/deps.json").exists())
        ok("3c modules/ 保留供排查",
           (ws / "P1/modules/mod-a/module.json").exists())

    def test_schema_and_precondition_guard(self):
        from porter.divide import import_p as IMP
        ws = self.tmp / "ws-sch"
        (ws / "P1").mkdir(parents=True)
        ok("4a plan 文件不存在 → rc=2",
           IMP.run_import(ws, self.drv, self.tmp / "ghost.json") == 2)
        p1 = self.tmp / "bad1.json"
        p1.write_text(json.dumps({"modules": []}), encoding="utf-8")
        ok("4b 空 modules → rc=2", IMP.run_import(ws, self.drv, p1) == 2)
        p2 = self.tmp / "bad2.json"
        p2.write_text(json.dumps({"modules": [
            {"name": "m", "files": [{"dest": "core.c", "src": "core.c"}]}]}),
            encoding="utf-8")
        ok("4c 缺 fragments → rc=2", IMP.run_import(ws, self.drv, p2) == 2)
        p3 = self.tmp / "bad3.json"
        p3.write_text(json.dumps({"modules": [
            {"name": "m", "files": [{"dest": "x.c", "src": "ghost.c",
                                     "fragments": [{"lines": "1-1"}]}]}]}),
            encoding="utf-8")
        ok("4d src 不存在（形状合规）→ rc=1",
           IMP.run_import(ws, self.drv, p3) == 1)
        bad_json = self.tmp / "bad4.json"
        bad_json.write_text("{not json", encoding="utf-8")
        ok("4e 非法 JSON → rc=2", IMP.run_import(ws, self.drv, bad_json) == 2)

    def test_h6_reuse_rebuild(self):
        from porter.divide import run as P1A
        ws = self.tmp / "ws-h6"
        (ws / "P1" / "reports").mkdir(parents=True)
        (ws / "P1/reports/P1D_plan.json").write_text(
            json.dumps(self.plan), encoding="utf-8")
        rc = P1A.run_divide(ws, self.drv)
        ok("5a plan 复用 + modules 缺失 → rc=0 且重建", rc == 0
           and (ws / "P1/modules/mod-core/module.json").exists()
           and (ws / "P1/modules/mod-leaf/module.json").exists(), f"rc={rc}")
        rc2 = P1A.run_divide(ws, self.drv)
        ok("5b modules 已在 → 复用 rc=0", rc2 == 0, f"rc={rc2}")

    def test_resolve_empty_guard(self):
        from porter.divide import resolve as RES
        ws = self.tmp / "ws-rg1"
        (ws / "P1" / "reports").mkdir(parents=True)
        (ws / "P1/reports/P1D_plan.json").write_text(
            json.dumps(self.plan), encoding="utf-8")
        (ws / "P1/modules").mkdir()          # 空 modules/
        ok("6a modules/ 为空 → rc=2（不写空图 deps.json）",
           RES.run_resolve(ws, self.drv) == 2
           and not (ws / "P1/modules/deps.json").exists())
        ws2 = self.tmp / "ws-rg2"
        (ws2 / "P1" / "reports").mkdir(parents=True)
        (ws2 / "P1/reports/P1D_plan.json").write_text(
            json.dumps(self.plan), encoding="utf-8")
        ok("6b modules/ 不存在 → rc=2", RES.run_resolve(ws2, self.drv) == 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
