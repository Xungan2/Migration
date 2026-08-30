"""porter/loop 单元测试（无 agent / 无网络 / 无 docker）。

覆盖（plan: vertical-slice-pipeline §8.3）：
A. LoopState：deps 初始化 / 断点指针 / phase 流转 / attempts 与人工重置 /
   坏结构重建
B. answers 解析与消费（T3 惯例：## 键 节）
C. criteria：schema 校验（枚举/layer-kind/正则/去重）、基线、
   check_unit_test / check_log_pattern
D. surface：模块使用面四分类（跨模块/裁剪残留/拼接碎片/纯字段访问/
   已映射/真缺失）+ 使用位置
E. probes：条目校验 / 日志判定 / probes.rs 确定性再生成 / downgraded 剔除
F. p4 机制：_slices 行数切分 / deferred 登记与清偿判定
运行：python3 tests/test_loop_state.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.loop import criteria as CR
from porter.loop import probes as PB
from porter.loop import p4 as P4
from porter.loop.state import LoopState, consume_answers, parse_answers
from porter.loop.surface import extract_surface

PASS, FAIL = 0, 0


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {extra}")


def _mk_ws() -> tuple[Path, Path]:
    """临时工作区 + 驱动参考树。"""
    tmp = Path(tempfile.mkdtemp(prefix="porter_loop_t_"))
    ws = tmp / "ws"
    drv = tmp / "drv"
    (ws / "P1" / "modules" / "modA").mkdir(parents=True)
    (ws / "P1" / "modules" / "modB").mkdir(parents=True)
    (drv).mkdir(parents=True)
    deps = {"order": ["modA", "modB"], "edges": {}, "cycles": []}
    (ws / "P1" / "modules" / "deps.json").write_text(
        json.dumps(deps), encoding="utf-8")
    return ws, drv


# ---------- A. LoopState ----------

def test_state():
    print("A. LoopState")
    ws, drv = _mk_ws()
    st = LoopState(ws)
    ok("deps 初始化", st.load_or_init() and st.order == ["modA", "modB"]
       and st.phase_of("modA") == "pending")
    ok("断点指针=首个非 done", st.pointer() == "modA")
    st.set_phase("modA", "p3")
    st.set_phase("modA", "p4")
    st.set_phase("modA", "done")
    ok("指针推进", st.pointer() == "modB")
    st2 = LoopState(ws)
    ok("落盘重入", st2.load_or_init() and st2.phase_of("modA") == "done")
    ok("done 集", st2.done_set() == {"modA"})
    n = st2.bump("modB", "p3")
    ok("attempts bump", n == 1 and st2.attempts("modB", "p3") == 1)
    st2.reset_attempts("modB", "p3")
    ok("人工重置 attempts", st2.attempts("modB", "p3") == 0)
    # 坏结构重建
    (ws / "loop_state.json").write_text('{"order": ["x"], "modules": {}}',
                                        encoding="utf-8")
    st3 = LoopState(ws)
    ok("坏结构自 deps 重建", st3.load_or_init()
       and st3.order == ["modA", "modB"])
    shutil.rmtree(ws.parent)


# ---------- B. answers ----------

def test_answers():
    print("B. answers")
    ws, drv = _mk_ws()
    (ws / "answers.md").write_text(
        "# answers\n\n## retry modA-p4\n\nfix applied\n\n## prefetch\n\n"
        "删除调用即可\n", encoding="utf-8")
    a = parse_answers(ws)
    ok("解析两节", set(a) == {"retry modA-p4", "prefetch"}
       and "删除调用" in a["prefetch"])
    taken = consume_answers(ws, ["prefetch"])
    ok("按键取走", "删除调用" in taken["prefetch"])
    a2 = parse_answers(ws)
    ok("未消费节保留", "retry modA-p4" in a2 and "prefetch" not in a2)
    shutil.rmtree(ws.parent)


# ---------- C. criteria ----------

def test_criteria():
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

def test_surface():
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

def test_probes():
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
    p = PB.sync_probes_rs(tmp, "drv", sections)
    text1 = p.read_text(encoding="utf-8")
    ok("active 进 run_all", "p_one();" in text1)
    ok("downgraded 剔除", "p_dead" not in text1)
    p2 = PB.sync_probes_rs(tmp, "drv", sections)
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

def test_probe_lifecycle():
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

def test_fix_targeting():
    print("E4. 编译错误定位探针")
    tmp = Path(tempfile.mkdtemp(prefix="porter_fix_t_"))
    crate = tmp / "kernel/core/comps/drv/src"
    crate.mkdir(parents=True)
    (crate / "probes.rs").write_text(
        "// header\nfn helper_a() { }\nfn p_one() { let _x = 1; }\n"
        "fn p_two() { helper_a(); }\npub(crate) fn run_all() {\n"
        "    p_one();\n    p_two();\n}\n", encoding="utf-8")
    active = [{"name": "p_one"}, {"name": "p_two"}]
    got = PB._probes_owning_lines(tmp, "drv", active, [3, 4, 8])
    ok("行号归属正确", got == {"p_one", "p_two"}, str(got))
    ok("路径缺失安全",
       PB._probes_owning_lines(tmp / "nope", "drv", active, [3]) == set())
    shutil.rmtree(tmp)


# ---------- E2. ut_verify 烟测 ----------

def test_ut_verify():
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


# ---------- F. p4 机制 ----------

def test_p4_mechanics():
    print("F. p4 机制")
    tmp = Path(tempfile.mkdtemp(prefix="porter_p4_t_"))
    f = tmp / "big.c"
    f.write_text("\n" * 2000, encoding="utf-8")
    g = tmp / "small.c"
    g.write_text("\n" * 100, encoding="utf-8")
    slices = P4._slices([f, g], max_lines=900)
    ok("切片尺寸", [(s[1], s[2]) for s in slices]
       == [(1, 900), (901, 1800), (1801, 2000), (1, 100)], str(slices))
    # deferred 登记/清偿
    ws = tmp / "ws"
    ws.mkdir()
    crit = {"criteria": [
        {"id": "m.d1", "layer": "L3", "kind": "log_pattern",
         "expr": "MAC address", "deferred_by": ["modB"]},
        {"id": "m.u1", "layer": "L0", "kind": "unit_test", "expr": "t1",
         "deferred_by": ["modB"]}]}
    P4._register_deferred(ws, "m", crit)
    d = json.loads((ws / "deferred.json").read_text(encoding="utf-8"))
    ok("登记 2 条", len(d["entries"]) == 2)
    P4._register_deferred(ws, "m", crit)
    d = json.loads((ws / "deferred.json").read_text(encoding="utf-8"))
    ok("登记幂等", len(d["entries"]) == 2)
    log = "e1000: MAC address aa:bb\n"
    ut = "test tests::t1 ... ok\ntest result: ok. 1 passed; 0 failed\n"
    rc, uncleared = P4._try_clear_deferred(ws, {"modB"}, log, ut)
    d = json.loads((ws / "deferred.json").read_text(encoding="utf-8"))
    ok("清偿成功", rc == 0 and all(e["status"] == "cleared"
                                  for e in d["entries"]))
    rc2, unc2 = P4._try_clear_deferred(ws, {"modB"}, "no log", "no test")
    ok("失败可复核", rc2 == 0)     # 已 cleared 不再复核
    # 未达消费者不清偿
    crit2 = {"criteria": [
        {"id": "m.d2", "layer": "L3", "kind": "log_pattern",
         "expr": "X", "deferred_by": ["modC"]}]}
    P4._register_deferred(ws, "m", crit2)
    rc3, _u = P4._try_clear_deferred(ws, {"modB"}, "X", "")
    d = json.loads((ws / "deferred.json").read_text(encoding="utf-8"))
    e2 = next(e for e in d["entries"] if e["id"] == "m.d2")
    ok("消费者未齐不清偿", e2["status"] == "open")
    shutil.rmtree(tmp)


if __name__ == "__main__":
    test_state()
    test_answers()
    test_criteria()
    test_surface()
    test_probes()
    test_probe_lifecycle()
    test_fix_targeting()
    test_ut_verify()
    test_p4_mechanics()
    print(f"\n{'='*40}\n{'ALL PASS' if FAIL == 0 else 'FAILURES'}: "
          f"{PASS} ok, {FAIL} fail")
    sys.exit(0 if FAIL == 0 else 1)
