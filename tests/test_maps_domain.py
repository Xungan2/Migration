"""maps 域收成/晋升测试（draft_knowledge / promote_map，薄 INDEX）。

覆盖：
A. 草稿（draft_knowledge）：双文件落盘/薄 INDEX 行 desc/hits 保留/
   旧富格式行清理/缺 mapping 跳过
B. 晋升（promote_map）：双文件搬运/INDEX 行/同名替换保 hits/
   多目标歧义/缺文件/驱动名不匹配

运行：python3 -m unittest tests.test_maps_domain
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


class TestMapsDomain(unittest.TestCase):
    maxDiff = None

    def test_draft_and_promote(self):
        from porter.bootstrap import kb
        from porter.bootstrap import knowledge as K
        print("=== maps 域：收成 + 晋升（薄 INDEX）===")
        old_temp = kb.TEMP_DIR
        tmp = Path(tempfile.mkdtemp(prefix="kb_maps_"))
        kb.TEMP_DIR = tmp / "ttemp"
        try:
            TD = kb.domain_temp("maps")
            ws = tmp / "ws"
            (ws / "P2" / "reports").mkdir(parents=True)
            (ws / "project.json").write_text(json.dumps({
                "linux_driver": "/x/drivers/net/e1000",
                "target_os": "/y/asterinas"}), encoding="utf-8")
            mapping = {"entries": [
                {"linux_api": "a", "verdict": "direct"},
                {"linux_api": "b", "verdict": "adapt"},
                {"linux_api": "c", "verdict": "adapt"},
                {"linux_api": "d", "verdict": "gap"}],
                "redesigns": [{"id": "r1"}], "wiring": []}
            (ws / "P2" / "mapping.json").write_text(
                json.dumps(mapping), encoding="utf-8")
            (ws / "P2" / "mapping.md").write_text("# mapping\n",
                                                  encoding="utf-8")

            print("=== A. 草稿 ===")
            rc = K.draft_knowledge(ws)
            idx = rd(TD / "INDEX.json")
            ok("M1 rc=0 双文件落盘", rc == 0
               and (TD / "e1000@asterinas.md").exists()
               and (TD / "e1000@asterinas.json").exists())
            ok("M2 薄 INDEX 行", len(idx) == 1
               and idx[0]["file"] == "e1000@asterinas.md"
               and idx[0]["hits"] == 0)
            ok("M3 desc 含计数与换思路",
               "direct 1" in idx[0]["desc"]
               and "adapt 2" in idx[0]["desc"] and "gap 1" in idx[0]["desc"]
               and "换思路 1" in idx[0]["desc"]
               and "e1000@asterinas.json" in idx[0]["desc"])

            # 旧富格式残留行 + hits 保留：手写旧行后重收成
            kb.save_index(TD, [
                {"entry_stem": "e1000@asterinas", "entry_file":
                 "e1000@asterinas.md", "driver_name": "e1000",
                 "hits": 5},
                {"entry_file": "other@z.md", "driver_name": "other",
                 "hits": 0}])
            K.draft_knowledge(ws)
            idx = rd(TD / "INDEX.json")
            mine = [e for e in idx
                    if e.get("file") == "e1000@asterinas.md"]
            ok("M4 旧富行清理 + hits 保留",
                len(mine) == 1 and mine[0]["hits"] == 5
                and len([e for e in idx
                         if e.get("entry_file") ==
                         "e1000@asterinas.md"]) == 0
                and any(e.get("entry_file") == "other@z.md"
                        for e in idx))

            # 缺 mapping → rc 1
            (ws / "P2" / "mapping.json").unlink()
            ok("M5 缺 mapping → rc 1", K.draft_knowledge(ws) == 1)
            (ws / "P2" / "mapping.json").write_text(
                json.dumps(mapping), encoding="utf-8")
            K.draft_knowledge(ws)

            print("=== B. 晋升 ===")
            kb_dir = tmp / "corpus"
            rc = K.promote_map("e1000", kb_dir, target="asterinas")
            kdir = kb.domain_kb("maps", kb_dir)
            ok("M6 rc=0 双文件搬运", rc == 0
               and (kdir / "e1000@asterinas.md").exists()
               and (kdir / "e1000@asterinas.json").exists()
               and not (TD / "e1000@asterinas.md").exists())
            kidx = rd(kdir / "INDEX.json")
            ok("M7 目标 INDEX 薄行 + hits 保留",
               len(kidx) == 1 and kidx[0]["file"] == "e1000@asterinas.md"
               and kidx[0]["hits"] == 5)
            ok("M8 temp INDEX 只剩他人", [e.get("file", e.get("entry_file"))
                                        for e in rd(TD / "INDEX.json")]
               == ["other@z.md"])

            # 同名再晋升（活文档替换）：草稿重收成（hits 清零态）→ 晋升
            # 后目标侧保留较高 hits
            kb.save_index(kdir, [{"file": "e1000@asterinas.md",
                                  "desc": "旧版", "hits": 9}])
            K.draft_knowledge(ws)     # temp 重建草稿（hits=5 继承自 temp 残留？）
            # temp 残留已被 M6 清空 → 新草稿 hits=0
            rc = K.promote_map("e1000", kb_dir)
            kidx = rd(kdir / "INDEX.json")
            ok("M9 同名替换保较高 hits", rc == 0 and len(kidx) == 1
               and kidx[0]["hits"] == 9)

            # M9b 旁车折叠：运行时咨询计数在晋升时并入、键清除
            kb.save_hits_sidecar(kb_dir, {"maps/e1000@asterinas.md": 4})
            K.draft_knowledge(ws)
            rc = K.promote_map("e1000", kb_dir)
            kidx = rd(kdir / "INDEX.json")
            ok("M9b promote_map 折叠旁车（9+4=13）", rc == 0
               and kidx[0]["hits"] == 13
               and kb.load_hits_sidecar(kb_dir) == {})

            # 多目标歧义
            (TD / "foo@bar.md").write_text("x", encoding="utf-8")
            (TD / "foo@qux.md").write_text("y", encoding="utf-8")
            kb.save_index(TD, [
                {"file": "foo@bar.md", "desc": "foo bar", "hits": 0},
                {"file": "foo@qux.md", "desc": "foo qux", "hits": 0}])
            ok("M10 多目标歧义 → rc 1",
               K.promote_map("foo", kb_dir) == 1)
            ok("M11 指定 target 晋升",
               K.promote_map("foo", kb_dir, target="bar") == 0
               and (kdir / "foo@bar.md").exists())
            # 缺文件（INDEX 有行无文件）
            kb.save_index(TD, [{"file": "ghost@z.md", "desc": "g",
                                "hits": 0}])
            ok("M12 缺文件 → rc 1",
               K.promote_map("ghost", kb_dir, target="z") == 1)
            # 无匹配
            ok("M13 无匹配 → rc 1",
               K.promote_map("nobody", kb_dir) == 1)
        finally:
            kb.TEMP_DIR = old_temp
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
