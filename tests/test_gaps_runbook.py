"""gaps / runbook 域测试（收成/消费/晋升 + gap rationale 回写）。

覆盖：
A. gaps：多模块归并（rationale 优先）/fill 成败并入/命名空间隔离/
   INDEX hits 保留/prior_entry 存在性检索/嵌套晋升
B. runbook：三主题落盘/notes 入文/INDEX 行/缺 runner 跳过
C. gates._apply_gap 的 rationale 回写（B6 断点修复）

运行：python3 -m unittest tests.test_gaps_runbook
"""
import sys, json, tempfile, shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import unittest


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def rd(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def wj(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                 encoding="utf-8")


class TestGapsRunbook(unittest.TestCase):
    maxDiff = None

    def test_gaps_domain(self):
        from porter.bootstrap import kb
        from porter.bootstrap import gaps as G
        print("=== A. gaps 域 ===")
        old_temp = kb.TEMP_DIR
        tmp = Path(tempfile.mkdtemp(prefix="kb_gaps_"))
        kb.TEMP_DIR = tmp / "ttemp"
        try:
            ws = tmp / "ws"
            wj(ws / "project.json", {
                "linux_driver": "/x/e1000", "target_os": "/y/asterinas"})
            # m1：agent 裸决策；m2：人工富集（同 api 带 rationale）
            wj(ws / "P3" / "m1" / "reports" / "gap_decisions.json", {
                "decisions": [
                    {"linux_api": "msleep", "strategy": "bypass",
                     "instruction": "忙等 TSC", "evidence": "i8042.rs:296"},
                    {"linux_api": "weird/api->name", "strategy": "fill",
                     "instruction": "补一个", "evidence": "x"},
                    {"linux_api": "clean_api", "strategy": "bypass",
                     "instruction": "丢弃", "evidence": "y"}]})
            wj(ws / "P3" / "m2" / "reports" / "gap_decisions.json", {
                "decisions": [
                    {"linux_api": "msleep", "strategy": "bypass",
                     "instruction": "忙等 TSC（修订）", "evidence": "x:1",
                     "rationale": "仅任务上下文；上游候选已登记",
                     "answered": True}]})
            # fill 成败：msleep fell-back（platform_patches）；
            # 另一 api filled（fill.json）
            wj(ws / "platform_patches.json", {"patches": [
                {"gap": "msleep", "status": "fell-back",
                 "reason": "OSTD 无定时睡眠原语，加法式补齐超范围",
                 "files": []}]})
            wj(ws / "P4" / "m1" / "reports" / "fill.json", {"results": {
                "weird/api->name": {"status": "filled", "patch": {
                    "reason": "", "files": ["a.rs"]}}}})

            rc = G.draft_gaps(ws)
            gdir = kb.domain_temp("gaps")
            ns = gdir / "e1000@asterinas"
            ok("A1 rc=0 文件落盘（api 名清洗）", rc == 0
               and (ns / "msleep.md").exists()
               and (ns / "weird_api-_name.md").exists())
            idx = rd(gdir / "INDEX.json")
            ok("A2 INDEX 嵌套行", {e["file"] for e in idx} == {
                "e1000@asterinas/msleep.md",
                "e1000@asterinas/weird_api-_name.md",
                "e1000@asterinas/clean_api.md"})
            ms = next(e for e in idx if e["file"].endswith("msleep.md"))
            ok("A3 desc 含策略与失败标记", "msleep：bypass" in ms["desc"]
               and "fill 曾失败" in ms["desc"])
            body = (ns / "msleep.md").read_text(encoding="utf-8")
            ok("A4 富集字段入文（rationale 优先源覆盖裸决策）",
               "理由（人工裁定留档）：仅任务上下文" in body
               and "忙等 TSC（修订）" in body)
            ok("A5 fill 失败原因入文",
               "fell-back——OSTD 无定时睡眠原语" in body)
            ok("A6a fill 成功入文", "fill 结果：filled" in
                (ns / "weird_api-_name.md").read_text(encoding="utf-8"))
            ok("A6b 未走 fill 标注", "—（未走 fill）" in
                (ns / "clean_api.md").read_text(encoding="utf-8"))

            # hits 保留 + 其他命名空间不动
            kb.save_index(gdir, [
                {"file": "e1000@asterinas/msleep.md", "desc": "旧",
                 "hits": 4},
                {"file": "other@z/keep.md", "desc": "他人", "hits": 1}])
            G.draft_gaps(ws)
            idx = rd(gdir / "INDEX.json")
            ms = next(e for e in idx if e["file"].endswith("msleep.md"))
            ok("A7 同 ns hits 保留", ms["hits"] == 4)
            ok("A8 他 ns 行不动",
               any(e["file"] == "other@z/keep.md" for e in idx))

            # prior_entry：temp 与已审两侧
            kb_dir = tmp / "corpus"
            ok("A9 prior temp 命中",
               G.prior_entry(kb_dir, "msleep") == ns / "msleep.md")
            ok("A10 prior 未命中", G.prior_entry(kb_dir, "nope") is None)
            n, _moved = kb.promote_entries(
                "gaps", ["e1000@asterinas/msleep.md"], kb_dir)
            ok("A11 嵌套晋升", n == 1 and (kb_dir / "gaps" /
               "e1000@asterinas" / "msleep.md").exists())
            ok("A12 prior 已审命中",
               G.prior_entry(kb_dir, "msleep") ==
               kb_dir / "gaps" / "e1000@asterinas" / "msleep.md")
            ok("A13 sanitize", G.sanitize_api("a/b->c") == "a_b-_c")
        finally:
            kb.TEMP_DIR = old_temp
            shutil.rmtree(tmp)

    def test_runbook_domain(self):
        from porter.bootstrap import kb
        from porter.bootstrap import runbook as R
        print("=== B. runbook 域 ===")
        old_temp = kb.TEMP_DIR
        tmp = Path(tempfile.mkdtemp(prefix="kb_rb_"))
        kb.TEMP_DIR = tmp / "ttemp"
        try:
            ws = tmp / "ws"
            wj(ws / "project.json", {
                "linux_driver": "/x/e1000", "target_os": "/y/asterinas"})
            wj(ws / "runner.json", {
                "build": {"cmd": "make kernel", "timeout_full_sec": 3000,
                          "timeout_inc_sec": 600,
                          "success_pattern": "completed successfully"},
                "boot": {"cmd": "make run_kernel", "timeout_sec": 600,
                         "log_file": "qemu.log",
                         "success_pattern": "Successfully booted",
                         "panic_pattern": "panic",
                         "inject_device": {
                             "mechanism": "env",
                             "env": "{'EXTRA_QEMU_ARGS': '<DEVICE_ARGS>'}",
                             "example_args": {"net": "-device e1000"}}},
                "unit_test": {"mechanism": "cargo-osdk-test",
                              "cmd": "cargo osdk test",
                              "success_pattern": "passed; 0 failed;",
                              "notes": "必须显式 --kcmd-args console=ttyS0"}})
            rc = R.draft_runbook(ws)
            rdir = kb.domain_temp("runbook")
            ok("B1 rc=0 三主题落盘", rc == 0
               and (rdir / "asterinas" / "build.md").exists()
               and (rdir / "asterinas" / "boot.md").exists()
               and (rdir / "asterinas" / "unit_test.md").exists())
            idx = rd(rdir / "INDEX.json")
            ok("B2 INDEX 三行", {e["file"] for e in idx} == {
                "asterinas/build.md", "asterinas/boot.md",
                "asterinas/unit_test.md"})
            ub = (rdir / "asterinas" / "unit_test.md").read_text(
                encoding="utf-8")
            ok("B3 notes 坑史入文", "console=ttyS0" in ub
               and "坑史" in ub)
            bb = (rdir / "asterinas" / "boot.md").read_text(encoding="utf-8")
            ok("B4 设备注入机制入文", "example_args" in bb
               and "-device e1000" in bb)
            desc = next(e for e in idx
                        if e["file"] == "asterinas/unit_test.md")["desc"]
            ok("B5 desc 含坑史标注", "含坑史 notes" in desc)
            # hits 保留
            kb.save_index(rdir, [{"file": "asterinas/build.md",
                                  "desc": "旧", "hits": 7}])
            R.draft_runbook(ws)
            idx = rd(rdir / "INDEX.json")
            ok("B6 hits 保留", next(e for e in idx if e["file"] ==
                                    "asterinas/build.md")["hits"] == 7)
            # 缺 runner → rc 1
            (ws / "runner.json").unlink()
            ok("B7 缺 runner → rc 1", R.draft_runbook(ws) == 1)
        finally:
            kb.TEMP_DIR = old_temp
            shutil.rmtree(tmp)

    def test_apply_gap_rationale(self):
        from porter.loop import gates as G
        print("=== C. gap 关口 rationale 回写（B6 断点修复）===")
        tmp = Path(tempfile.mkdtemp(prefix="kb_gap_"))
        try:
            ws = tmp / "ws"
            wj(ws / "P3" / "m1" / "reports" / "gap_decisions.json", {
                "decisions": [
                    {"linux_api": "netdev_stats", "strategy": "human",
                     "instruction": "待人确认"}]})
            gate = {"id": "p3.gap.m1.netdev_stats", "module": "m1",
                    "subject": "netdev_stats"}
            G._apply_gap(ws, gate, {
                "strategy": "bypass", "instruction": "丢弃统计",
                "rationale": "MVP 无消费方"})
            dec = rd(ws / "P3" / "m1" / "reports" / "gap_decisions.json")
            d = dec["decisions"][0]
            ok("C1 rationale 回写进 gap_decisions",
               d["rationale"] == "MVP 无消费方"
               and d["strategy"] == "bypass" and d["answered"] is True)
            # mapping notes 同步仍在（尽力而为）
            ok("C2 无 mapping 不崩", not (ws / "P2").exists())
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
