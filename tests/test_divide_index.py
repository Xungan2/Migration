"""p1-divide 索引/展开的功能性测试（无 agent）。

覆盖（plan: divide-refactor §5.1/§5.4）：
A. 真实 e1000 锚点索引：L11 driver_name / L22 pci_tbl / L64 reg /
   L246 exit_module / L251 module_exit / L253 request_irq / fwd 等
B. mini 分配表走 expand 的归属强制：
   - module_exit(:251) 落 os-probe（历史 bug 用例）
   - MODULE_DEVICE_TABLE(:64) 随 pci_tbl
   - 前向声明跟随被声明函数（含跨文件）
   - copybreak=null → module_param(:163)/MODULE_PARM_DESC(:164) 随裁
   - plan schema / dest=src / 片段无重叠无缝隙（已分配区间内）
C. validate_decision：缺符号催补 / 二选一 / whole_file 校验
D. 合成驱动边界：多行定义名在下一行 / 注释扩展 / DEFINE_MUTEX /
   SIMPLE_DEV_PM_OPS / 多行 MODULE_PARM_DESC / 匿名 enum→chunk /
   typedef enum / whole_file 头文件 / 相邻合并

运行：python3 tests/test_divide_index.py
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


class TestDivideIndex(unittest.TestCase):
    maxDiff = None

    def test_full(self):
        from porter.divide import index as IX

        E1000 = Path("/home/xungan/project/linux/drivers/net/ethernet/intel/e1000")

        def at(entries, line):
            return next((e for e in entries if e.line == line), None)

        def plan_lines(plan, mod, src):
            out = []
            for m in plan["modules"]:
                if m["name"] != mod: continue
                for f in m["files"]:
                    if f["src"] != src: continue
                    for fr in f["fragments"]:
                        a, b = fr["lines"].split("-")
                        out.append((int(a), int(b)))
            return sorted(out)

        def covers(spans, line):
            return any(a <= line <= b for a, b in spans)

        def no_overlap(spans):
            return all(a1 <= b1 and b1 < a2 for (a1, b1), (a2, b2) in zip(spans, spans[1:]))

        # ============ A/B. 真实 e1000 ============
        if E1000.is_dir():
            print("=== A. 真实 e1000 索引锚点 ===")
            idx = IX.build_index(E1000)
            mc = idx["e1000_main.c"]
            e = at(mc, 11);  ok("A1 L11 var e1000_driver_name", e and e.kind == "var" and e.symbol == "e1000_driver_name", str(e))
            e = at(mc, 22);  ok("A2 L22 var e1000_pci_tbl", e and e.kind == "var" and e.symbol == "e1000_pci_tbl", str(e))
            e = at(mc, 64);  ok("A3 L64 reg MODULE_DEVICE_TABLE→pci_tbl", e and e.kind == "reg" and "e1000_pci_tbl" in e.refs, str(e))
            e = at(mc, 66);  ok("A4 L66 fwd e1000_up", e and e.kind == "fwd" and e.symbol == "e1000_up", str(e))
            e = at(mc, 246); ok("A5 L246 func e1000_exit_module", e and e.kind == "func" and e.symbol == "e1000_exit_module", str(e))
            e = at(mc, 251); ok("A6 L251 reg module_exit→e1000_exit_module", e and e.kind == "reg" and "e1000_exit_module" in e.refs, str(e))
            e = at(mc, 253); ok("A7 L253 func e1000_request_irq", e and e.kind == "func" and e.symbol == "e1000_request_irq", str(e))
            e = at(mc, 178); ok("A8 L178 var e1000_pm_ops(宏定义型)", e and e.kind == "var" and e.symbol == "e1000_pm_ops" and "e1000_suspend" in e.refs, str(e))
            e = at(mc, 163); ok("A9 L163 reg module_param→copybreak", e and e.kind == "reg" and e.refs == ["copybreak"], str(e))
            hw = idx["e1000_hw.c"]
            e = at(hw, 70);  ok("A10 hw.c L70 多行定义 table 名", e and e.symbol == "e1000_igp_cable_length_table", str(e))
            e = at(hw, 84);  ok("A11 hw.c L84 DEFINE_MUTEX→var e1000_eeprom_lock", e and e.kind == "var" and e.symbol == "e1000_eeprom_lock", str(e))
            tile = all(b.end + 1 == a.start for a, b in zip(mc[1:], mc[:-1])) and mc[-1].end == 5321
            ok("A12 main.c 条目 tile 无缝", tile)
            n_ext = sum(1 for x in mc if x.start < x.line)
            ok("A13 注释扩展生效（main.c 中起点上移条目 > 20）", n_ext > 20, f"n={n_ext}")
            e = at(mc, 162); ok("A9b L162 var copybreak（__read_mostly 不抢名）", e and e.symbol == "copybreak", str(e))
            n_all = {f: len(v) for f, v in idx.items()}
            print(f"  （条目数：{n_all}）")

            print("=== B. mini 分配表 → 归属强制 ===")
            dec_main = {"assignments": {
                "e1000_driver_name": "os-probe", "e1000_driver_string": "os-probe",
                "e1000_copyright": "os-probe", "e1000_pci_tbl": "os-probe",
                "e1000_init_module": "os-probe", "e1000_exit_module": "os-probe",
                "e1000_driver": "os-probe", "e1000_up": "os-rings-open",
                "e1000_request_irq": "os-rx-irq", "e1000_free_irq": "os-rx-irq",
                "e1000_intr": "os-rx-irq",
                "copybreak": None, "debug": None,
            }}
            dec_hw = {"assignments": {"e1000_set_mac_type": "hw-mac-reset",
                                      "e1000_reset_hw": "hw-mac-reset"}}
            plan, audit = IX.expand(idx, {"e1000_main.c": dec_main, "e1000_hw.c": dec_hw},
                                    {}, "模块含 os-probe os-rx-irq os-rings-open hw-mac-reset")
            sp = plan_lines(plan, "os-probe", "e1000_main.c")
            ok("B1 module_exit(:251) 落 os-probe（历史 bug 用例）", covers(sp, 251))
            ok("B2 MODULE_DEVICE_TABLE(:64) 随 pci_tbl 落 os-probe", covers(sp, 64))
            ok("B3 module_init(:238) 落 os-probe", covers(sp, 238))
            ok("B4 os-probe 片段无重叠", no_overlap(sp))
            sp_r = plan_lines(plan, "os-rings-open", "e1000_main.c")
            ok("B5 前向声明 e1000_up(:66) 落 os-rings-open", covers(sp_r, 66))
            sp_i = plan_lines(plan, "os-rx-irq", "e1000_main.c")
            ok("B6 e1000_request_irq(:253) 落 os-rx-irq", covers(sp_i, 253))
            allsp = sorted(sp + sp_r + sp_i)
            ok("B7 copybreak 随裁：:162-164 不被任何片段覆盖",
               not covers(allsp, 162) and not covers(allsp, 163) and not covers(allsp, 164))
            ok("B8 debug 随裁：:197-199 不被覆盖", not covers(allsp, 197) and not covers(allsp, 199))
            ok("B9 审计报告列显式裁剪 copybreak/debug", "copybreak" in audit and "debug" in audit)
            ok("B10 审计含 machine 归属 L251", "L251" in audit and "os-probe" in audit)
            mods = {m["name"] for m in plan["modules"]}
            ok("B11 plan schema 模块/文件/片段字段齐",
               all(set(m) >= {"name", "function", "files"} for m in plan["modules"])
               and all(set(f) >= {"dest", "src", "fragments"} for m in plan["modules"] for f in m["files"])
               and all(set(fr) >= {"lines", "symbol"} for m in plan["modules"] for f in m["files"] for fr in f["fragments"]))
            ok("B12 dest == src", all(f["dest"] == f["src"] for m in plan["modules"] for f in m["files"]))
            ok("B13 模块名均在 strategy 文本 → 无告警", "疑似自创" not in audit)
            plan2, audit2 = IX.expand(idx, {"e1000_main.c": dec_main}, {}, "短文本")
            ok("B14 自创模块名告警", "疑似自创" in audit2)
        else:
            print(f"=== A/B 跳过：{E1000} 不存在 ===")

        # ============ C. validate_decision ============
        print("=== C. validate_decision ===")
        ents = [IX.Entry(line=1, kind="func", symbol="fa"), IX.Entry(line=9, kind="fwd", symbol="fb"),
                IX.Entry(line=11, kind="reg", symbol="module_exit", refs=["fa"]),
                IX.Entry(line=13, kind="chunk", symbol="")]
        ok("C1 None → 报错", IX.validate_decision(ents, None) is not None)
        ok("C2 二选一缺失 → 报错", IX.validate_decision(ents, {"foo": 1}) is not None)
        ok("C3 同时出现 → 报错", "不能同时" in IX.validate_decision(ents, {"whole_file": "m", "assignments": {}}))
        ok("C4 whole_file 合法", IX.validate_decision(ents, {"whole_file": "m"}) is None)
        ok("C5 whole_file 非法值 → 报错", IX.validate_decision(ents, {"whole_file": 3}) is not None)
        msg = IX.validate_decision(ents, {"assignments": {"fb": "m"}})
        ok("C6 缺 fa 催补（fwd/reg/chunk 不要求）", msg and "fa" in msg and "fb" not in msg, msg or "")
        ok("C7 全覆盖通过（null 允许）", IX.validate_decision(ents, {"assignments": {"fa": "m", "fb": None}}) is None)
        ok("C8 值非法 → 报错", IX.validate_decision(ents, {"assignments": {"fa": 5, "fb": "m"}}) is not None)

        # ============ D. 合成驱动边界 ============
        print("=== D. 合成驱动 ===")
        tmp = Path(tempfile.mkdtemp(prefix="p1d_ix_"))
        drv = tmp / "drv"; drv.mkdir()
        (drv / "mini.c").write_text("""#include <linux/x.h>

/* banner */
static const
u16 table_x[] = {1, 2};

/**
 * doc for fa
 */
static int fa(void)
{
	return 0;
}
EXPORT_SYMBOL(fa);
void fb(void);
static SIMPLE_DEV_PM_OPS(pm_ops, sus, res);
MODULE_PARM_DESC(cb,
		"copy break");
static int cb = 1;
module_param(cb, int, 0600);
typedef enum {
	A = 0,
} anon_t;
struct sbox {
	int f;
};
union ubox {
	int u;
};
""", encoding="utf-8")
        (drv / "mini.h").write_text("#ifndef H\n#define H\n#define MAGIC 1\n#endif\n", encoding="utf-8")
        ix2 = IX.build_index(drv)
        mc2 = ix2["mini.c"]
        e = at(mc2, 5); ok("D1 多行定义：名在下一行 table_x", e and e.symbol == "table_x" and e.kind == "var", str(e))
        e = at(mc2, 10); ok("D2 func fa", e and e.kind == "func" and e.symbol == "fa", str(e))
        ok("D3 注释扩展：fa.start=7（/** 行）", e and e.start == 7, f"start={e and e.start}")
        e = at(mc2, 14); ok("D4 EXPORT_SYMBOL→reg refs=[fa]", e and e.kind == "reg" and e.refs == ["fa"], str(e))
        e = at(mc2, 15); ok("D5 fwd fb", e and e.kind == "fwd" and e.symbol == "fb", str(e))
        e = at(mc2, 16); ok("D6 SIMPLE_DEV_PM_OPS→var pm_ops refs=[sus,res]", e and e.kind == "var" and e.symbol == "pm_ops" and e.refs == ["sus", "res"], str(e))
        e = at(mc2, 17); ok("D7 多行 MODULE_PARM_DESC→refs=[cb]", e and e.kind == "reg" and e.refs == ["cb"], str(e))
        e = at(mc2, 20); ok("D8 module_param→refs=[cb]", e and e.kind == "reg" and e.refs == ["cb"], str(e))
        e = at(mc2, 21); ok("D9 typedef enum 匿名→chunk", e and e.kind == "chunk", str(e))
        e = at(mc2, 24); ok("D10 struct sbox", e and e.kind == "struct" and e.symbol == "sbox", str(e))
        e = at(mc2, 27); ok("D11 union ubox", e and e.kind == "union" and e.symbol == "ubox", str(e))
        tile2 = all(b.end + 1 == a.start for a, b in zip(mc2[1:], mc2[:-1]))
        ok("D12 合成文件 tile 无缝且末行=总行数", tile2 and mc2[-1].end == 29)

        dec = {"mini.c": {"assignments": {"table_x": "hw-defs", "fa": "hw-core",
                                          "pm_ops": None, "cb": "os-param"}},
               "mini.h": {"whole_file": "hw-defs"}}
        plan3, audit3 = IX.expand(ix2, dec, {}, "hw-defs hw-core os-param")
        sp = plan_lines(plan3, "hw-defs", "mini.c")
        ok("D13 table_x 落 hw-defs", covers(sp, 5))
        sp = plan_lines(plan3, "hw-core", "mini.c")
        ok("D14 EXPORT_SYMBOL(:14) 随 fa 落 hw-core", covers(sp, 14))
        ok("D15 pm_ops=null → :16 裁", not covers(plan_lines(plan3, "hw-core", "mini.c") + plan_lines(plan3, "hw-defs", "mini.c") + plan_lines(plan3, "os-param", "mini.c"), 16))
        sp = plan_lines(plan3, "os-param", "mini.c")
        ok("D16 MODULE_PARM_DESC(:17-18)+module_param(:20) 随 cb 落 os-param",
           covers(sp, 17) and covers(sp, 18) and covers(sp, 20))
        sp3 = plan_lines(plan3, "hw-defs", "mini.c")
        ok("D17 匿名 typedef enum(:21-23) chunk 跟随前一可分配 cb",
           covers(plan_lines(plan3, "os-param", "mini.c"), 21) and covers(plan_lines(plan3, "os-param", "mini.c"), 23))
        hdr = [f for m in plan3["modules"] if m["name"] == "hw-defs" for f in m["files"] if f["src"] == "mini.h"]
        ok("D18 whole_file 头文件 → 单片段 1-4", hdr and hdr[0]["fragments"] == [{"lines": "1-4", "symbol": "(整文件)"}], str(hdr))
        ok("D19 相邻 fa(:7-13)+EXPORT(:14)+无主 fwd fb(:15) 合并成一片段",
           plan_lines(plan3, "hw-core", "mini.c") == [(7, 15)]
           and sp == [(17, 23)], str(plan_lines(plan3, "hw-core", "mini.c")))
        ok("D20 function 缺省=模块名", all(m["function"] == m["name"] for m in plan3["modules"]))

        # 与 fragments.extract_modules 端到端（真实抽取）
        from porter.divide import fragments as FR
        ws = tmp / "ws"; (ws / "P1").mkdir(parents=True)
        summary = FR.extract_modules(ws, drv, plan3)
        ok("D21 抽取三模块", sorted(summary) == ["hw-core", "hw-defs", "os-param"], str(sorted(summary)))
        ok("D22 跨模块同名 dest=mini.c 并存", (ws / "P1/modules/hw-defs/mini.c").exists()
           and (ws / "P1/modules/os-param/mini.c").exists() and (ws / "P1/modules/hw-defs/mini.h").exists())
        same_mod = {"modules": [
            {"name": "m1", "function": "", "files": [
                {"dest": "mini.c", "src": "mini.c", "fragments": [{"lines": "1-5", "symbol": "a"}]},
                {"dest": "mini.c", "src": "mini.c", "fragments": [{"lines": "7-8", "symbol": "b"}]}]}]}
        try:
            FR.extract_modules(ws, drv, same_mod)
            ok("D23 同模块 dest 重复仍被拒绝", False)
        except FR.DivideError:
            ok("D23 同模块 dest 重复仍被拒绝", True)

        shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
