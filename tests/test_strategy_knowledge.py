"""p1-strategy 的知识/temp 功能性测试（无 agent）。

目录模型（kb.py 三区）：base（工具随附）/ 本次知识库目录（已沉淀）/
temp（草稿）。本文件用假 KB 目录覆盖 kb.BASE_DIR/kb.TEMP_DIR。

覆盖：
A. 草稿入 temp（_draft_to_temp）：写入/字段/排除/同名改名/完全一致跳过
B. 晋升（promote_sample）：正常/真重复拒绝（含 base 一致）/构成不同
   改名并入/歧义/缺文件
C. 注入（_build_samples_injection）：多态
D. 报告（_write_knowledge_report）：改名反映
E. 集成（run_strategy 复用路径，无 agent）

运行：python3 -m unittest tests.test_strategy_knowledge
所有测试用临时假数据，测后恢复分区为空库。
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


class TestStrategyKnowledge(unittest.TestCase):
    maxDiff = None

    def test_full(self):
        from porter.bootstrap import kb
        from porter.divide import strategy as S

        tmp = Path(tempfile.mkdtemp(prefix="p1s_t_"))
        fake_base, fake_temp = tmp / "base", tmp / "ttemp"
        corpus = tmp / "corpus"
        old_base, old_temp = kb.BASE_DIR, kb.TEMP_DIR
        kb.BASE_DIR, kb.TEMP_DIR = fake_base, fake_temp
        try:
            BD = kb.domain_base("splits")    # base 分区
            KD = kb.domain_kb("splits", corpus)  # 已沉淀（知识库目录侧）
            TD = kb.domain_temp("splits")    # 草稿分区

            def reset():
                for d in (BD, KD, TD):
                    d.mkdir(parents=True, exist_ok=True)
                    for f in d.glob("*"):
                        if f.name not in ("README.md",):
                            if f.is_dir(): shutil.rmtree(f)
                            else: f.unlink()
                    (d / "INDEX.json").write_text("[]", encoding="utf-8")

            def mkdrv(t, name, files):
                d = t / name; d.mkdir()
                for n in files: (d / n).write_text("x")
                return d

            def rd(p): return json.loads(Path(p).read_text(encoding="utf-8"))

            print("=== A. 草稿入 temp ===")
            reset()
            drv = mkdrv(tmp, "e1000", ["e1000_main.c", "e1000_hw.c",
                                       "e1000.h", "README.txt"])
            ws = tmp / "ws"; (ws / "reports").mkdir(parents=True)
            (ws / "project.json").write_text(
                json.dumps({"linux_driver": str(drv),
                            "kb_dir": str(corpus)}), encoding="utf-8")
            proj = {"linux_driver": str(drv), "kb_dir": str(corpus)}
            strat = tmp / "src.md"; strat.write_text("# strategy body")

            # A1 空库写入 + 字段
            r = S._draft_to_temp(ws, proj, drv, strat)
            idx = rd(TD / "INDEX.json")
            ok("A1a 空库写入 <驱动名>.md", r["status"] == "已写入 temp"
               and r["entry_file"] == "e1000.md"
               and (TD / "e1000.md").exists())
            ok("A1b INDEX 条目字段", len(idx) == 1
               and idx[0]["driver_name"] == "e1000"
               and idx[0]["hits"] == 0
               and idx[0]["entry_file"] == "e1000.md")
            ok("A1c linux_files 仅 *.c/*.h 且排序",
               idx[0]["linux_files"] == ["e1000.h", "e1000_hw.c",
                                         "e1000_main.c"],
               str(idx[0].get("linux_files")))
            ok("A2 README.txt 被排除",
               "README.txt" not in idx[0]["linux_files"])

            # A3 知识库目录侧完全一致 → 跳过
            (TD / "e1000.md").unlink(); S._save_index(TD, [])
            (KD / "e1000.md").write_text("k")
            S._save_index(KD, [{"entry_file": "e1000.md",
                                "driver_name": "e1000",
                                "linux_dir": str(drv),
                                "linux_files": ["e1000.h", "e1000_hw.c",
                                                "e1000_main.c"],
                                "hits": 0}])
            r = S._draft_to_temp(ws, proj, drv, strat)
            ok("A3 已沉淀(知识库目录)完全一致→跳过",
               r["status"] == "未写入" and "完全一致" in r["value"]
               and not (TD / "e1000.md").exists())

            # A3b base 侧完全一致 → 跳过（工具已随附）
            (KD / "e1000.md").unlink(); S._save_index(KD, [])
            (BD / "e1000.md").write_text("k")
            S._save_index(BD, [{"entry_file": "e1000.md",
                                "driver_name": "e1000",
                                "linux_dir": "/b",
                                "linux_files": ["e1000.h", "e1000_hw.c",
                                                "e1000_main.c"],
                                "hits": 0}])
            r = S._draft_to_temp(ws, proj, drv, strat)
            ok("A3b base 完全一致→跳过", r["status"] == "未写入")

            # A4 已沉淀相关(同名不同文件集)、temp空 → 写裸名
            reset()
            (KD / "e1000.md").write_text("k")
            S._save_index(KD, [{"entry_file": "e1000.md",
                                "driver_name": "e1000", "linux_dir": "/k",
                                "linux_files": ["old_only.c"], "hits": 0}])
            r = S._draft_to_temp(ws, proj, drv, strat)
            ok("A4 已沉淀相关→写裸名+构成不同",
               r["status"] == "已写入 temp"
               and r["entry_file"] == "e1000.md"
               and "构成不同" in r["value"])

            # A5 temp 已同名+同文件集 → 跳过
            r = S._draft_to_temp(ws, proj, drv, strat)
            ok("A5 temp已同名同文件集→跳过",
               r["status"] == "未写入" and "完全一致" in r["value"])

            # A6 temp 已同名+不同文件集 → 改名 __2
            drv3 = tmp / "sub" / "e1000"; drv3.mkdir(parents=True)
            for n in ["e1000_main.c", "new_file.c"]: (drv3 / n).write_text("x")
            proj3 = {"linux_driver": str(drv3), "kb_dir": str(corpus)}
            r = S._draft_to_temp(ws, proj3, drv3, strat)
            idx = rd(TD / "INDEX.json")
            names = sorted(e["entry_file"] for e in idx)
            ok("A6 temp同名不同构成→改名__2",
               r["entry_file"] == "e1000__2.md"
               and names == ["e1000.md", "e1000__2.md"],
               f"got {r['entry_file']} idx={names}")

            # A7 第三种构成 → __3
            drv4 = tmp / "s3" / "e1000"; drv4.mkdir(parents=True)
            for n in ["e1000_main.c", "third.c"]: (drv4 / n).write_text("x")
            r = S._draft_to_temp(
                ws, {"linux_driver": str(drv4), "kb_dir": str(corpus)},
                drv4, strat)
            idx = rd(TD / "INDEX.json")
            ok("A7 第三种构成→__3", r["entry_file"] == "e1000__3.md"
               and len(idx) == 3, f"got {r['entry_file']}")

            ok("A8 每步后 INDEX 均合法",
               isinstance(rd(TD / "INDEX.json"), list))

            print("=== B. 晋升 promote_sample ===")
            # B9 正常晋升 temp → 知识库目录
            reset()
            (TD / "foo.md").write_text("# foo body")
            S._save_index(TD, [{"entry_file": "foo.md",
                                "driver_name": "foo", "linux_dir": "/x/foo",
                                "linux_files": ["a.c"], "hits": 0}])
            rc = S.promote_sample("foo", corpus)
            ok("B9a rc=0", rc == 0)
            ok("B9b 文件搬运", (KD / "foo.md").exists()
               and not (TD / "foo.md").exists())
            ok("B9c INDEX 搬运", rd(TD / "INDEX.json") == []
               and rd(KD / "INDEX.json")[0]["driver_name"] == "foo")
            ok("B9d 内容不变",
               (KD / "foo.md").read_text() == "# foo body")

            # B10 知识库目录侧已同名+同文件集 → 拒绝
            (TD / "foo.md").write_text("# again")
            S._save_index(TD, [{"entry_file": "foo.md",
                                "driver_name": "foo", "linux_dir": "/x/foo",
                                "linux_files": ["a.c"], "hits": 0}])
            rc = S.promote_sample("foo", corpus)
            ok("B10 真重复→拒绝", rc == 1 and (TD / "foo.md").exists())

            # B10b 与 base 完全一致 → 拒绝（工具已随附）
            reset()
            (BD / "bar.md").write_text("k")
            S._save_index(BD, [{"entry_file": "bar.md",
                                "driver_name": "bar", "linux_dir": "/b",
                                "linux_files": ["z.c"], "hits": 0}])
            (TD / "bar.md").write_text("# bar")
            S._save_index(TD, [{"entry_file": "bar.md",
                                "driver_name": "bar", "linux_dir": "/x/bar",
                                "linux_files": ["z.c"], "hits": 0}])
            ok("B10b base一致→拒绝",
               S.promote_sample("bar", corpus) == 1 and (TD / "bar.md").exists())

            # B11 知识库目录侧已同名+不同文件集 → 改名并入
            reset()
            (TD / "foo.md").write_text("# first")
            S._save_index(TD, [{"entry_file": "foo.md",
                                "driver_name": "foo", "linux_dir": "/x/foo",
                                "linux_files": ["a.c"], "hits": 0}])
            S.promote_sample("foo", corpus)
            (TD / "foo.md").write_text("# diff composition")
            S._save_index(TD, [{"entry_file": "foo.md",
                                "driver_name": "foo", "linux_dir": "/x/foo",
                                "linux_files": ["a.c", "b.c"], "hits": 0}])
            rc = S.promote_sample("foo", corpus)
            knames = sorted(e["entry_file"] for e in rd(KD / "INDEX.json"))
            ok("B11 构成不同→改名__2并入",
               rc == 0 and knames == ["foo.md", "foo__2.md"],
               f"rc={rc} {knames}")

            # B12 晋升不存在的驱动
            ok("B12 不存在→报错", S.promote_sample("ghost", corpus) == 1)

            # B13 INDEX 有条目但文件缺失
            S._save_index(TD, [{"entry_file": "missing.md",
                                "driver_name": "missing", "linux_dir": "/x",
                                "linux_files": ["z.c"], "hits": 0}])
            ok("B13 文件缺失→报错",
               S.promote_sample("missing", corpus) == 1)

            # B14 同名歧义：两个同名条目
            reset()
            (TD / "e.md").write_text("1"); (TD / "e__2.md").write_text("2")
            S._save_index(TD, [
                {"entry_file": "e.md", "driver_name": "e", "linux_dir": "/x",
                 "linux_files": ["a.c"], "hits": 0},
                {"entry_file": "e__2.md", "driver_name": "e",
                 "linux_dir": "/x", "linux_files": ["a.c", "b.c"],
                 "hits": 0}])
            print("  (下两行应有歧义提示)")
            ok("B14a --driver 名歧义→报错", S.promote_sample("e", corpus) == 1)
            rc = S.promote_sample("e__2.md", corpus)
            kidx = rd(KD / "INDEX.json")
            tidx = rd(TD / "INDEX.json")
            e_k = [x for x in kidx if x.get("driver_name") == "e"]
            e_t = [x for x in tidx if x.get("driver_name") == "e"]
            ok("B14b 条目文件名精确晋升", rc == 0 and len(e_k) == 1
               and (KD / e_k[0]["entry_file"]).exists()
               and len(e_t) == 1 and (TD / "e.md").exists())

            ok("B15 晋升后两 INDEX 合法",
               isinstance(rd(TD / "INDEX.json"), list)
               and isinstance(rd(KD / "INDEX.json"), list))

            print("=== C. 注入（回归）===")
            reset()
            out = S._build_samples_injection(ws)
            ok("C16 空→空提示", out == S._EMPTY_NOTE)
            (TD / "d1.md").write_text("x")
            S._save_index(TD, [{"entry_file": "d1.md", "driver_name": "d1",
                                "linux_dir": "/d1", "linux_files": ["a.c"],
                                "hits": 0}])
            out = S._build_samples_injection(ws)
            ok("C17 仅草稿→注入草稿分区", "草稿样例" in out
               and "d1.md" in out and "工具随附样例" not in out
               and "已沉淀样例" not in out)
            (KD / "k1.md").write_text("x")
            S._save_index(KD, [{"entry_file": "k1.md", "driver_name": "k1",
                                "linux_dir": "/k1", "linux_files": ["b.c"],
                                "hits": 0}])
            (BD / "b1.md").write_text("x")
            S._save_index(BD, [{"entry_file": "b1.md", "driver_name": "b1",
                                "linux_dir": "/b1", "linux_files": ["c.c"],
                                "hits": 0}])
            out = S._build_samples_injection(ws)
            ok("C18 三分区→都注入", "工具随附样例" in out
               and "已沉淀样例" in out and "草稿样例" in out
               and "b1.md" in out and "k1.md" in out)
            (KD / "INDEX.json").write_text("{bad", encoding="utf-8")
            out = S._build_samples_injection(ws)
            ok("C19 INDEX损坏→退化文件清单", "- k1.md" in out)
            S._save_index(KD, [{"entry_file": "k1.md", "driver_name": "k1",
                                "linux_dir": "/k1", "linux_files": ["b.c"],
                                "hits": 0}])
            S._save_index(KD, list(rd(KD / "INDEX.json")) + [
                {"entry_file": "ghost.md", "driver_name": "g",
                 "linux_dir": "/g", "linux_files": [], "hits": 0}])
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                S._build_samples_injection(ws)
            ok("C20 幽灵/未登记→一致性警告", "不一致" in buf.getvalue())

            # C21 工作区无 kb_dir → 分区退化为 base+temp（不崩）
            ws_nokb = tmp / "ws_nokb"; ws_nokb.mkdir()
            (ws_nokb / "project.json").write_text(
                json.dumps({"linux_driver": str(drv)}), encoding="utf-8")
            out = S._build_samples_injection(ws_nokb)
            ok("C21 无kb_dir→base+temp仍注入", "工具随附样例" in out
               and "已沉淀样例" not in out and "草稿样例" in out)

            print("=== D. 报告 ===")
            reset()
            res = {"driver": "e1000", "linux_dir": "/x/e1000",
                   "linux_files": ["a.c", "b.c"], "status": "已写入 temp",
                   "entry_file": "e1000__2.md",
                   "value": "新而有价值；改名 e1000__2.md 保留"}
            rpt = S._write_knowledge_report(
                ws, {"linux_driver": "/x/e1000"}, res)
            txt = rpt.read_text(encoding="utf-8")
            ok("D21 报告含 p1-promote 命令行", "p1-promote" in txt
               and "--output-dir" in txt and "e1000__2.md" in txt)
            ok("D22 改名反映真实文件名", "e1000__2.md" in txt
               and "| e1000__2.md |" in txt)

            print("=== E. 集成：run_strategy 复用路径（无 agent）===")
            reset()
            ews = tmp / "ews"; (ews / "P1" / "reports").mkdir(parents=True)
            (ews / "P1" / "strategy.md").write_text(
                "# existing strategy\n" + ("x" * 500))
            (ews / "project.json").write_text(json.dumps({
                "linux_driver": str(drv), "category": ["net"],
                "materials": [], "kb_dir": str(corpus)}))
            rc = S.run_strategy(ews, drv)
            eidx = rd(TD / "INDEX.json")
            ok("E23a rc=0", rc == 0)
            ok("E23b 复用路径不跑 agent 也自动草稿",
               (TD / "e1000.md").exists()
               and eidx[0]["driver_name"] == "e1000")
            ok("E23c 报告已生成",
               (ews / "P1" / "reports" / "P1-knowledge.md").exists())
        finally:
            kb.BASE_DIR, kb.TEMP_DIR = old_base, old_temp
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
