"""porter/loop 单元测试（无 agent / 无网络 / 无 docker；unittest 形态）。

覆盖（plan: vertical-slice-pipeline §8.3 + 方案 A 相位重构）：
A. LoopState：deps 初始化 / 断点指针 / 五相流转 / attempts 三桶与人工
   重置 / 存量无 p5 键兼容 / 坏结构重建
B. answers 解析与消费（T3 惯例：## 键 节；retry 键含 -p5）
C. criteria：schema 校验（枚举/layer-kind/正则/去重）、基线、
   check_unit_test / check_log_pattern
D. surface：模块使用面四分类（跨模块/裁剪残留/拼接碎片/纯字段访问/
   已映射/真缺失）+ 使用位置
E. probes：条目校验 / 日志判定 / probes.rs 确定性再生成 / downgraded 剔除
   / 生命周期降级不株连 / 编译错误定位 / ut_verify 烟测
F. p4/p5 机制：_slices 行数切分（P4）/ deferred 登记与清偿（P5）/
   相位职责剥离边界（P4 无验收、P5 有）
G. 循环编排：p4→p5→done 转移 / attempts 三桶判界（p5 烧穿 exit 3）/
   泊车绕过（deps 满足放行、不满足拒绝）/ retry -p5 / p5 子命令前置检查
运行：python3 tests/test_loop_state.py 或 unittest discover
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.loop import criteria as CR
from porter.loop import p4 as P4
from porter.loop import p5 as P5
from porter.loop import probes as PB
from porter.loop import run as RUN
from porter.loop.state import LoopState, consume_answers, parse_answers
from porter.loop.surface import extract_surface


def ok(name, cond, extra=""):
    """断言助手：失败即抛（unittest discover / 直跑两用法通用）。"""
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def _mk_ws(order=("modA", "modB"), edges=None) -> tuple[Path, Path]:
    """临时工作区（deps.json + project.json）+ 驱动参考树。"""
    tmp = Path(tempfile.mkdtemp(prefix="porter_loop_t_"))
    ws = tmp / "ws"
    drv = tmp / "drv"
    for m in order:
        (ws / "P1" / "modules" / m).mkdir(parents=True)
    (drv).mkdir(parents=True)
    deps = {"order": list(order), "edges": edges or {}, "cycles": []}
    (ws / "P1" / "modules" / "deps.json").write_text(
        json.dumps(deps), encoding="utf-8")
    (ws / "project.json").write_text("{}", encoding="utf-8")
    return ws, drv


# ---------- A. LoopState ----------

class TestState(unittest.TestCase):
    def test_state(self):
        print("A. LoopState")
        ws, drv = _mk_ws()
        st = LoopState(ws)
        ok("deps 初始化", st.load_or_init() and st.order == ["modA", "modB"]
           and st.phase_of("modA") == "pending")
        ok("初始化 attempts 三桶", st.attempts("modA", "p5") == 0)
        ok("断点指针=首个非 done", st.pointer() == "modA")
        st.set_phase("modA", "p3")
        st.set_phase("modA", "p4")
        st.set_phase("modA", "p5")
        ok("指针在 p5 相位仍指向本模块", st.pointer() == "modA")
        st.set_phase("modA", "done")
        ok("指针推进", st.pointer() == "modB")
        st2 = LoopState(ws)
        ok("落盘重入", st2.load_or_init() and st2.phase_of("modA") == "done")
        ok("done 集", st2.done_set() == {"modA"})
        n = st2.bump("modB", "p5")
        ok("attempts bump p5 桶", n == 1 and st2.attempts("modB", "p5") == 1)
        st2.reset_attempts("modB", "p5")
        ok("人工重置 p5 桶", st2.attempts("modB", "p5") == 0)
        # 存量兼容：旧状态机 attempts 无 p5 键——读入自动补零且不回写盘
        legacy = {"order": ["modA", "modB"],
                  "modules": {"modA": {"phase": "done",
                                       "attempts": {"p3": 1, "p4": 2}},
                              "modB": {"phase": "p4",
                                       "attempts": {"p3": 0, "p4": 1}}}}
        (ws / "loop_state.json").write_text(json.dumps(legacy),
                                            encoding="utf-8")
        st3 = LoopState(ws)
        ok("存量无 p5 键读入成功", st3.load_or_init())
        ok("存量 p5 桶补零", st3.attempts("modA", "p5") == 0
           and st3.attempts("modB", "p5") == 0)
        on_disk = json.loads((ws / "loop_state.json").read_text(
            encoding="utf-8"))
        ok("合法存量读入不回写盘",
           "p5" not in on_disk["modules"]["modA"]["attempts"])
        # 坏结构重建
        (ws / "loop_state.json").write_text('{"order": ["x"], "modules": {}}',
                                            encoding="utf-8")
        st4 = LoopState(ws)
        ok("坏结构自 deps 重建", st4.load_or_init()
           and st4.order == ["modA", "modB"])
        shutil.rmtree(ws.parent)


# ---------- B. answers ----------

class TestAnswers(unittest.TestCase):
    def test_answers(self):
        print("B. answers")
        ws, _drv = _mk_ws()
        (ws / "answers.md").write_text(
            "# answers\n\n## retry modA-p5\n\nfix applied\n\n## prefetch\n\n"
            "删除调用即可\n", encoding="utf-8")
        a = parse_answers(ws)
        ok("解析两节", set(a) == {"retry modA-p5", "prefetch"}
           and "删除调用" in a["prefetch"])
        taken = consume_answers(ws, ["prefetch"])
        ok("按键取走", "删除调用" in taken["prefetch"])
        a2 = parse_answers(ws)
        ok("未消费节保留", "retry modA-p5" in a2 and "prefetch" not in a2)
        shutil.rmtree(ws.parent)


# ---------- C. criteria ----------

class TestCriteria(unittest.TestCase):
    def test_criteria(self):
        print("C. criteria")
        good = [
            {"id": "m.u1", "layer": "L0", "kind": "unit_test",
             "expr": "t1,t2", "deferred_by": None},
            {"id": "m.l1", "layer": "L3", "kind": "log_pattern",
             "expr": "MAC address .*", "deferred_by": ["modB"]},
            {"id": "m.c1", "layer": "L3", "kind": "counter",
             "expr": "rx=[1-9]", "deferred_by": None},
            {"id": "m.e1", "layer": "L4", "kind": "e2e", "expr": "",
             "deferred_by": None},
        ]
        ok_list, errs = CR.validate_criteria(good, "m")
        ok("合格 4 条", len(ok_list) == 4 and not errs, str(errs))
        bad = [
            {"id": "m.x", "layer": "L2", "kind": "unit_test", "expr": "t",
             "deferred_by": None},                       # layer-kind 不一致
            {"id": "m.y", "layer": "L3", "kind": "log_pattern", "expr": "[",
             "deferred_by": None},                       # 坏正则
            {"id": "m.z", "layer": "L0", "kind": "unit_test", "expr": "",
             "deferred_by": None},                       # unit_test 无测名
            {"id": "m.w", "layer": "L9", "kind": "compile", "expr": "",
             "deferred_by": None},                       # layer 非法
        ]
        _ok2, errs2 = CR.validate_criteria(bad, "m")
        ok("坏条目全拦（4）", len(errs2) == 4 and not _ok2, str(errs2))
        dup = [
            {"id": "m.d", "layer": "L0", "kind": "unit_test", "expr": "t",
             "deferred_by": None},
            {"id": "m.d", "layer": "L0", "kind": "unit_test", "expr": "t",
             "deferred_by": None},                       # id 重复
        ]
        ok3, errs3 = CR.validate_criteria(dup, "m")
        ok("重复 id 拦截（首个合法时）", len(errs3) == 1 and len(ok3) == 1,
           str(errs3))
        base = CR.baseline_criteria("m")
        ok("基线 compile+boot", [c["kind"] for c in base] == ["compile", "boot"]
           and base[0]["id"] == "m.compile")
        out = "test tests::t1 ... ok\ntest tests::t2 ... ok\n" \
              "test result: ok. 2 passed; 0 failed\n"
        good_ck, _d = CR.check_unit_test(out, ["t1", "t2"])
        bad_ck, _d2 = CR.check_unit_test(out.replace("ok. 2", "FAILED. 1"),
                                         ["t1"])
        miss_ck, d3 = CR.check_unit_test(out, ["t3"])
        ok("unit_test 判定", good_ck and not bad_ck and not miss_ck
           and "t3" in d3, d3)
        log = "e1000: MAC address 52:54:00:12:34:56 read\n" \
              "e1000 stats: rx=3 tx=1\n"
        ok("log_pattern 命中", CR.check_log_pattern(log, "MAC address .*")[0])
        ok("counter 命中", CR.check_log_pattern(log, r"rx=[1-9]")[0])
        ok("counter 零不命中", not CR.check_log_pattern(
            "e1000 stats: rx=0 tx=1\n", r"rx=[1-9]")[0])


# ---------- D. surface ----------

class TestSurface(unittest.TestCase):
    def test_surface(self):
        print("D. surface")
        ws, drv = _mk_ws()
        # 原始驱动树：TRIMMED_MACRO 在原树有定义、切分中被裁
        (drv / "orig.h").write_text(
            "#define TRIMMED_MACRO 1\nstruct hw { int mac_type; };\n",
            encoding="utf-8")
        # modB 定义 CROSS（跨模块依赖）
        (ws / "P1" / "modules" / "modB" / "b.c").write_text(
            "int CROSS = 1;\n", encoding="utf-8")
        (ws / "P1" / "modules" / "modB" / "module.json").write_text(
            '{"name": "modB"}', encoding="utf-8")
        # modA：使用各色符号（PASTE_ 只引用不定义——拼接碎片形态）
        (ws / "P1" / "modules" / "modA" / "a.c").write_text(
            "#include <linux/pci.h>\n"
            "extern void KERNEL_API(int);\n"
            "extern int MAPPED_API;\n"
            "int g = PASTE_ + 1;\n"
            "void f(struct hw *h) {\n"
            "    KERNEL_API(TRIMMED_MACRO + CROSS + MAPPED_API);\n"
            "    h->mac_type = 1;\n"
            "}\n", encoding="utf-8")
        (ws / "P1" / "modules" / "modA" / "module.json").write_text(
            '{"name": "modA"}', encoding="utf-8")
        mapping = {"entries": [
            {"linux_api": "MAPPED_API", "kind": "function", "verdict": "direct",
             "target": "T", "evidence": "a.rs:1", "notes": "", "risk": "low",
             "confidence": "high", "domain": "linux/pci.h"}],
            "redesigns": [], "wiring": []}
        (ws / "P2").mkdir(parents=True, exist_ok=True)
        (ws / "P2" / "mapping.json").write_text(json.dumps(mapping),
                                                encoding="utf-8")
        surface, rc = extract_surface(ws, drv, "modA")
        ok("提取成功", rc == 0)
        st = surface["stats"]
        ok("跨模块识别", surface["cross_module"] == ["CROSS"],
           str(surface["cross_module"]))
        ok("裁剪残留识别", "TRIMMED_MACRO" in surface["noise"]["internal_cut"])
        ok("拼接碎片识别", "PASTE_" in surface["noise"]["paste_fragments"])
        ok("纯字段访问识别", "mac_type" in surface["noise"]["field_only"],
           str(surface["noise"]))
        ok("已映射识别", surface["mapped_by_verdict"].get("direct")
           == ["MAPPED_API"])
        missing = [s for v in surface["missing_by_domain"].values()
                   for s in v]
        ok("真缺失识别", missing == ["KERNEL_API"], str(missing))
        ok("使用位置带行号", any("a.c:" in loc
           for locs in surface["usage_locations"].values()
           for loc in locs))
        # 幂等：二次调用复用
        surf2, _rc = extract_surface(ws, drv, "modA")
        ok("幂等复用", surf2["generated"] == surface["generated"])
        shutil.rmtree(ws.parent)


# ---------- E. probes ----------

class TestProbes(unittest.TestCase):
    def test_probes(self):
        print("E. probes")
        good = [{"name": "p_one", "rust": "fn p_one() { }", "claim": "readl"}]
        bad = [{"name": "BadName", "rust": "fn x() {}", "claim": ""},
               {"name": "p_unbal", "rust": "fn p_unbal() {", "claim": "c"}]
        okl, errs = PB.validate_probes(good + bad)
        ok("探针校验", len(okl) == 1 and len(errs) == 2, str(errs))
        log = "PROBE_p_one PASS\nPROBE_p_two FAIL\n"
        v = PB.judge(log, ["p_one", "p_two", "p_three"])
        ok("判定 pass/fail/missing", v == {"p_one": "ok", "p_two": "fail",
                                            "p_three": "missing"})
        tmp = Path(tempfile.mkdtemp(prefix="porter_probe_t_"))
        crate = tmp / "kernel/core/comps/drv/src"
        crate.mkdir(parents=True)
        reg1 = {"probes": [{"name": "p_one", "rust": "fn p_one() {\n}",
                            "claim": "readl", "status": "active"},
                           {"name": "p_dead", "rust": "fn p_dead() {\n}",
                            "claim": "writel", "status": "downgraded"}]}
        sections = [("P3(modA)", reg1["probes"])]
        p = PB.sync_probes(tmp, tmp, "drv", sections)
        text1 = p.read_text(encoding="utf-8")
        ok("active 进 run_all", "p_one();" in text1)
        ok("downgraded 剔除", "p_dead" not in text1)
        p2 = PB.sync_probes(tmp, tmp, "drv", sections)
        ok("再生成确定性", p2.read_text(encoding="utf-8") == text1)
        # P2 预生成注册表：known_claims / collect_sections / marker
        ws = tmp / "ws"
        (ws / "P2" / "reports").mkdir(parents=True)
        (ws / "P3" / "modA" / "reports").mkdir(parents=True)
        PB.save_registry(ws / "P2" / "reports" / "probes.json",
                         {"probes": [{"name": "p_pre", "rust": "fn p_pre() {}",
                                      "claim": "prefetch", "status": "active"}]})
        PB.save_registry(ws / "P3" / "modA" / "reports" / "probes.json",
                         {"probes": [{"name": "p_a", "rust": "fn p_a() {}",
                                      "claim": "readl", "status": "active"}]})
        claims = PB.known_claims(ws, ["modA"], "(pregen)")
        ok("known_claims 含 P2 注册表", claims == {"readl"}, str(claims))
        claims2 = PB.known_claims(ws, ["modA"], "modB")
        ok("known_claims 跨表并集",
           claims2 == {"readl", "prefetch"}, str(claims2))
        sections = PB.collect_sections(ws, ["modA"], "modB",
                                       ws / "P3" / "modB" / "reports" /
                                       "probes.json", kind="P3")
        ok("collect_sections 含 P2 节",
           [m for m, _ in sections] == ["P2(pregen)", "P3(modA)"],
           str([m for m, _ in sections]))
        sections_self = PB.collect_sections(ws, ["modA"], "(pregen)",
                                            ws / "P2" / "reports" / "probes.json",
                                            kind="P2")
        ok("预生成自身调用不重复 P2 节",
           [m for m, _ in sections_self] == ["P3(modA)", "P2(pregen)"],
           str([m for m, _ in sections_self]))
        ok("marker_of 路径推断",
           PB.marker_of(ws / "P2" / "reports" / "probes.json") == "P2(pregen)"
           and PB.marker_of(ws / "P3" / "modA" / "reports" / "probes.json")
           == "P3(modA)")
        shutil.rmtree(tmp)


# ---------- E3. 探针生命周期：降级不株连 PASS ----------

class TestProbeLifecycle(unittest.TestCase):
    def test_probe_lifecycle(self):
        print("E3. probes lifecycle 降级不株连")
        tmp = Path(tempfile.mkdtemp(prefix="porter_life_t_"))
        ws = tmp / "ws"
        tgt = tmp / "asterinas"
        boot_ws = ws / "P3" / "modA"
        (boot_ws / "reports").mkdir(parents=True)
        log = tmp / "boot.log"
        log.write_text("PROBE_p_a PASS\nPROBE_p_b FAIL\n", encoding="utf-8")
        runner = json.dumps({"boot": {"log_file": str(log)}})
        (boot_ws.parent / "runner.json").write_text(runner, encoding="utf-8")
        (ws / "runner.json").write_text(runner, encoding="utf-8")
        entries = [
            {"linux_api": "api_a", "kind": "function", "verdict": "direct",
             "target": "T", "evidence": "a.rs:1", "notes": "", "risk": "med",
             "confidence": "med", "domain": "x"},
            {"linux_api": "api_b", "kind": "function", "verdict": "direct",
             "target": "T", "evidence": "a.rs:2", "notes": "", "risk": "med",
             "confidence": "med", "domain": "x"}]
        mapping = {"entries": entries, "redesigns": [], "wiring": []}
        (ws / "P2").mkdir()
        (ws / "P2" / "mapping.json").write_text(json.dumps(mapping),
                                                encoding="utf-8")
        gen_json = ('```json\n{"probes": ['
                    '{"name": "p_a", "rust": "fn p_a() { }", "claim": "api_a"},'
                    '{"name": "p_b", "rust": "fn p_b() { }", "claim": "api_b"}'
                    ']}\n```')

        def fake_run_agent(prompt, workdir, log_stem, timeout_sec=0):
            if "待探针的映射条目" in prompt:
                return 0, gen_json
            return 0, "junk"    # 改判轮无可解析输出 → _rejudge_failed False

        saved = (PB.agent.run_agent, PB.agent.load_skill,
                 PB.probe_mod.probe_build, PB.probe_mod.probe_boot_with_device)
        PB.agent.run_agent = fake_run_agent
        PB.agent.load_skill = lambda _n: ""
        PB.probe_mod.probe_build = lambda *a, **k: {"ok": True}
        PB.probe_mod.probe_boot_with_device = lambda *a, **k: {"ok": True}
        try:
            reg_path = boot_ws / "reports" / "probes.json"
            rc = PB.run_probe_lifecycle(
                ws, tgt, {"linux_driver": "/x/drv", "category": []},
                ["modA"], reg_path, label="T", todo_entries=entries,
                logs_dir=tmp / "logs", boot_ws=boot_ws)
        finally:
            (PB.agent.run_agent, PB.agent.load_skill, PB.probe_mod.probe_build,
             PB.probe_mod.probe_boot_with_device) = saved
        reg = PB.load_registry(reg_path)
        st = {p["name"]: p["status"] for p in reg["probes"]}
        ok("PASS 探针保留 active", st.get("p_a") == "active", str(st))
        ok("仅 FAIL 探针降级", st.get("p_b") == "downgraded", str(st))
        m2 = json.loads((ws / "P2" / "mapping.json").read_text(encoding="utf-8"))
        v = {e["linux_api"]: e["verdict"] for e in m2["entries"]}
        ok("PASS claim 不改判", v.get("api_a") == "direct", str(v))
        ok("FAIL claim 降级 gap", v.get("api_b") == "gap", str(v))
        rs = (tgt / "kernel" / "core" / "comps" / "drv" / "src" /
              "probes.rs").read_text(encoding="utf-8")
        ok("probes.rs 剔除降级项", "p_a();" in rs and "fn p_b" not in rs)
        ok("生命周期 rc=0", rc == 0)
        shutil.rmtree(tmp)


# ---------- E4. 编译错误行号 → 出错探针定位 ----------

class TestFixTargeting(unittest.TestCase):
    def test_fix_targeting(self):
        print("E4. 编译错误定位探针")
        tmp = Path(tempfile.mkdtemp(prefix="porter_fix_t_"))
        crate = tmp / "kernel/core/comps/drv/src"
        crate.mkdir(parents=True)
        (crate / "probes.rs").write_text(
            "// header\nfn helper_a() { }\nfn p_one() { let _x = 1; }\n"
            "fn p_two() { helper_a(); }\npub(crate) fn run_all() {\n"
            "    p_one();\n    p_two();\n}\n", encoding="utf-8")
        active = [{"name": "p_one"}, {"name": "p_two"}]
        got = PB._probes_owning_lines(tmp, tmp, "drv", active, [3, 4, 8])
        ok("行号归属正确", got == {"p_one", "p_two"}, str(got))
        ok("路径缺失安全",
           PB._probes_owning_lines(tmp, tmp / "nope", "drv", active,
                                   [3]) == set())
        shutil.rmtree(tmp)


# ---------- E2. ut_verify 烟测 ----------

class TestUtVerify(unittest.TestCase):
    def test_ut_verify(self):
        print("E2. ut_verify")
        from porter.loop import ut_verify as UV
        ansi = "test result: \x1b[32mok\x1b[39m. 4 passed; 0 failed; 0 filtered\n"
        ok("verify 命中（ANSI 剥离）",
           UV.verify_output(ansi, "passed; 0 failed;")[0])
        ok("verify 失败特征命中即拒",
           not UV.verify_output(ansi + "\nfailures:\n", "passed; 0 failed;",
                                "failures:")[0])
        ok("verify ANSI 剥离后逐字可命中",
           UV.verify_output(ansi, "test result: ok")[0])
        ok("verify 缺成功特征",
           not UV.verify_output("no tests here", "passed; 0 failed;")[0])
        fb = UV.feedback_block("输出未见成功特征 'x'", "line1\nline2")
        ok("反馈块含说明与尾部", "输出未见成功特征" in fb and "line2" in fb
           and "```" in fb)


# ---------- F. p4/p5 机制 ----------

class TestP4P5Mechanics(unittest.TestCase):
    def test_p4_mechanics(self):
        print("F. p4/p5 机制")
        tmp = Path(tempfile.mkdtemp(prefix="porter_p4_t_"))
        f = tmp / "big.c"
        f.write_text("\n" * 2000, encoding="utf-8")
        g = tmp / "small.c"
        g.write_text("\n" * 100, encoding="utf-8")
        slices = P4._slices([f, g], max_lines=900)
        ok("切片尺寸", [(s[1], s[2]) for s in slices]
           == [(1, 900), (901, 1800), (1801, 2000), (1, 100)], str(slices))
        # 相位职责剥离边界
        ok("P4 已无验收段", not hasattr(P4, "_acceptance")
           and not hasattr(P4, "_try_clear_deferred"))
        ok("P4 保留轮末快速冒烟", hasattr(P4, "_quick_smoke"))
        ok("验收/deferred 助手迁至 P5", hasattr(P5, "run_p5")
           and hasattr(P5, "_try_clear_deferred")
           and hasattr(P5, "_register_deferred"))
        # deferred 登记/清偿（P5）
        ws = tmp / "ws"
        ws.mkdir()
        crit = {"criteria": [
            {"id": "m.d1", "layer": "L3", "kind": "log_pattern",
             "expr": "MAC address", "deferred_by": ["modB"]},
            {"id": "m.u1", "layer": "L0", "kind": "unit_test", "expr": "t1",
             "deferred_by": ["modB"]}]}
        P5._register_deferred(ws, "m", crit)
        d = json.loads((ws / "deferred.json").read_text(encoding="utf-8"))
        ok("登记 2 条", len(d["entries"]) == 2)
        P5._register_deferred(ws, "m", crit)
        d = json.loads((ws / "deferred.json").read_text(encoding="utf-8"))
        ok("登记幂等", len(d["entries"]) == 2)
        log = "e1000: MAC address aa:bb\n"
        ut = "test tests::t1 ... ok\ntest result: ok. 1 passed; 0 failed\n"
        rc, uncleared = P5._try_clear_deferred(ws, {"modB"}, log, ut)
        d = json.loads((ws / "deferred.json").read_text(encoding="utf-8"))
        ok("清偿成功", rc == 0 and all(e["status"] == "cleared"
                                      for e in d["entries"]))
        rc2, unc2 = P5._try_clear_deferred(ws, {"modB"}, "no log", "no test")
        ok("失败可复核", rc2 == 0)     # 已 cleared 不再复核
        # 未达消费者不清偿
        crit2 = {"criteria": [
            {"id": "m.d2", "layer": "L3", "kind": "log_pattern",
             "expr": "X", "deferred_by": ["modC"]}]}
        P5._register_deferred(ws, "m", crit2)
        rc3, _u = P5._try_clear_deferred(ws, {"modB"}, "X", "")
        d = json.loads((ws / "deferred.json").read_text(encoding="utf-8"))
        e2 = next(e for e in d["entries"] if e["id"] == "m.d2")
        ok("消费者未齐不清偿", e2["status"] == "open")
        # 全局哨兵（__P6__ 与旧 P5/__P5__）在 P5 内不可清偿
        for sentinel in ("__P6__", "__P5__", "P5"):
            crit3 = {"criteria": [
                {"id": f"m.g_{sentinel.strip('_')}", "layer": "L3",
                 "kind": "log_pattern", "expr": "X",
                 "deferred_by": [sentinel]}]}
            P5._register_deferred(ws, "m", crit3)
        rc4, _u4 = P5._try_clear_deferred(ws, {"__P6__", "__P5__", "P5",
                                               "modB"}, "X", "")
        d = json.loads((ws / "deferred.json").read_text(encoding="utf-8"))
        gl = {e["id"]: e["status"] for e in d["entries"]
              if e["id"].startswith("m.g_")}
        ok("全局哨兵条目不在 P5 清偿",
           rc4 == 0 and set(gl.values()) == {"open"}, str(gl))
        # mech-none 登记哨兵 = __P6__
        P5._register_mech_none(ws, "m", {"id": "m.u9", "layer": "L0",
                                         "kind": "unit_test", "expr": "t9",
                                         "deferred_by": None})
        d = json.loads((ws / "deferred.json").read_text(encoding="utf-8"))
        e9 = next(e for e in d["entries"] if e["id"] == "m.u9")
        ok("mech-none 哨兵 __P6__", e9["deferred_by"] == ["__P6__"],
           str(e9["deferred_by"]))
        shutil.rmtree(tmp)


# ---------- G. 循环编排（run_loop / cmd_p5） ----------

class _LoopHarness:
    """打桩 p3/p4/p5 阶段函数 + 调用序记录。"""

    def __init__(self, rc_plan=None):
        self.calls: list[tuple[str, str]] = []
        self.rc_plan = rc_plan or {}     # (step, 第几次) -> rc；缺省 0
        self._counts = {"p3": 0, "p4": 0, "p5": 0}
        self._saved = None

    def _fake(self, step):
        def run(ws, module, order):
            self._counts[step] += 1
            self.calls.append((step, module))
            return self.rc_plan.get((step, self._counts[step]), 0)
        return run

    def __enter__(self):
        self._saved = (RUN.p3.run_p3, RUN.p4.run_p4, RUN.p5.run_p5)
        RUN.p3.run_p3 = self._fake("p3")
        RUN.p4.run_p4 = self._fake("p4")
        RUN.p5.run_p5 = self._fake("p5")
        return self

    def __exit__(self, *exc):
        (RUN.p3.run_p3, RUN.p4.run_p4, RUN.p5.run_p5) = self._saved


class TestLoopOrchestration(unittest.TestCase):
    @mock.patch("porter.loop.gates.first_module_review_enabled",
                return_value=False)
    def test_p3_p4_p5_done_transitions(self, _fm):
        print("G1. p3→p4→p5→done 转移（FM 审关闭——本测专注状态机）")
        ws, _drv = _mk_ws()
        with _LoopHarness() as h:
            rc = RUN.run_loop(ws)
        ok("全绿退出 0", rc == 0)
        ok("每模块 p3→p4→p5 依序调用",
           h.calls == [("p3", "modA"), ("p4", "modA"), ("p5", "modA"),
                       ("p3", "modB"), ("p4", "modB"), ("p5", "modB")],
           str(h.calls))
        st = LoopState(ws)
        st.load_or_init()
        ok("全部 done", st.done_set() == {"modA", "modB"})
        saved = json.loads((ws / "loop_state.json").read_text(encoding="utf-8"))
        ok("落盘 attempts 三桶", all(
            set(m["attempts"]) == {"p3", "p4", "p5"}
            for m in saved["modules"].values()))
        shutil.rmtree(ws.parent)

    @mock.patch("porter.loop.gates.first_module_review_enabled",
                return_value=False)
    def test_p4_fail_bump_then_recover(self, _fm):
        print("G2. P4 失败一次 bump 后自愈（FM 审关闭）")
        ws, _drv = _mk_ws(order=("modA",))
        with _LoopHarness(rc_plan={("p4", 1): 1}) as h:
            rc = RUN.run_loop(ws)
        ok("最终退出 0", rc == 0)
        ok("p4 调用两次", [c for c in h.calls if c[0] == "p4"]
           == [("p4", "modA"), ("p4", "modA")], str(h.calls))
        st = LoopState(ws)
        st.load_or_init()
        ok("p4 桶 attempts=1", st.attempts("modA", "p4") == 1)
        shutil.rmtree(ws.parent)

    @mock.patch("porter.loop.gates.first_module_review_enabled",
                return_value=False)
    def test_p5_burnout_exit3(self, _fm):
        print("G3. p5 桶烧穿 → exit 3 + panic 关口（FM 审关闭）")
        ws, _drv = _mk_ws(order=("modA",))
        with _LoopHarness(rc_plan={("p5", 1): 1, ("p5", 2): 1,
                                   ("p5", 3): 1}):
            rc = RUN.run_loop(ws)
        ok("烧穿退出 3", rc == 3)
        st = LoopState(ws)
        st.load_or_init()
        ok("泊在 p5 相位", st.phase_of("modA") == "p5"
           and st.attempts("modA", "p5") == 3)
        ledger = json.loads((ws / "gates.json").read_text(encoding="utf-8"))
        gate_ids = [g["id"] for g in ledger["gates"]]
        ok("panic 关口已登记", "loop.attempts.modA-p5" in gate_ids)
        ok("关口 open", any(g["id"] == "loop.attempts.modA-p5"
                            and g["status"] == "open"
                            for g in ledger["gates"]))
        hq = (ws / "human_questions.md").read_text(encoding="utf-8")
        ok("渲染含关口表单", "@loop.attempts.modA-p5" in hq
           and "note" in hq)
        # retry 答案清零后恢复
        (ws / "answers.md").write_text(
            "## retry modA-p5\n\nfixed\n", encoding="utf-8")
        with _LoopHarness() as h:
            rc = RUN.run_loop(ws)
        st2 = LoopState(ws)
        st2.load_or_init()
        ok("retry -p5 清零后直通", rc == 0
           and st2.phase_of("modA") == "done"
           and st2.attempts("modA", "p5") == 0, str(h.calls[-2:]))
        shutil.rmtree(ws.parent)

    def test_bypass_parked_module(self):
        print("G4. 泊车绕过")
        ws, _drv = _mk_ws(order=("modA", "modB"), edges={"modB": []})
        st = LoopState(ws)
        st.load_or_init()
        st.set_phase("modA", "p3")
        for _ in range(3):
            st.bump("modA", "p3")
        with _LoopHarness() as h:
            rc = RUN.run_loop(ws, module="modB")
        ok("绕行完成退出 0", rc == 0)
        st2 = LoopState(ws)
        st2.load_or_init()
        ok("modB done、modA 泊车不动",
           st2.phase_of("modB") == "done" and st2.phase_of("modA") == "p3")
        ok("只推进 modB", {m for _s, m in h.calls} == {"modB"},
           str(h.calls))
        shutil.rmtree(ws.parent)

    def test_bypass_refused_when_deps_unmet(self):
        print("G5. deps 不满足 → 绕行拒绝 rc 2")
        ws, _drv = _mk_ws(order=("modA", "modC"), edges={"modC": ["modA"]})
        st = LoopState(ws)
        st.load_or_init()
        st.set_phase("modA", "p3")
        for _ in range(3):
            st.bump("modA", "p3")
        with _LoopHarness() as h:
            rc = RUN.run_loop(ws, module="modC")
        ok("拒绝退出 2", rc == 2)
        ok("未推进任何模块", h.calls == [], str(h.calls))
        shutil.rmtree(ws.parent)

    def test_cmd_p5_precondition(self):
        print("G6. p5 子命令前置检查（未跑 P4 拒绝）")
        from porter import main as MAIN
        ws, _drv = _mk_ws(order=("modA",))
        st = LoopState(ws)
        st.load_or_init()
        for phase, expect in (("pending", 2), ("p3", 2), ("p4", 2)):
            st.set_phase("modA", phase)
            rc = MAIN.cmd_p5(SimpleNamespace(output_dir=str(ws),
                                             module="modA"))
            ok(f"phase={phase} → rc {expect}", rc == expect)
        # 全部 done + 指针 None → no-op 成功
        for m in ("modA",):
            st.set_phase(m, "done")
        rc = MAIN.cmd_p5(SimpleNamespace(output_dir=str(ws), module=None))
        ok("指针 None → 全部已完成 rc 0", rc == 0)
        shutil.rmtree(ws.parent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
