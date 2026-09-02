"""kb.py 知识库骨架测试（目录模型/域注册表/薄 INDEX/通用晋升）。

覆盖：
- kb_dir_of / kb_dir_for：相对名 → KB_ROOT 下解析；绝对路径原样；
  缺失/损坏 → None
- 薄 INDEX：load/save/upsert_entry（保 hits）/bump_hits
- promote_entries：temp → 知识库目录（文件+INDEX 行搬运、选择性、
  同名再晋升保较高 hits）
- 域注册表完整性：五域登记 + subdir 唯一

运行：python3 -m unittest tests.test_kb
"""
import sys, json, tempfile, shutil
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import unittest


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


class TestKbSkeleton(unittest.TestCase):
    maxDiff = None

    def test_registry(self):
        from porter.bootstrap import kb
        print("=== 域注册表 ===")
        ok("K1 五域登记", set(kb.DOMAINS) ==
           {"maps", "gaps", "runbook", "splits", "pitfalls"},
           str(sorted(kb.DOMAINS)))
        subdirs = [d["subdir"] for d in kb.DOMAINS.values()]
        ok("K2 subdir 唯一", len(subdirs) == len(set(subdirs)))
        ok("K3 每域有 desc", all(d.get("desc") for d in
                                 kb.DOMAINS.values()))
        # 路径计算一致性
        ok("K4 domain_* 三通道",
           kb.domain_temp("gaps") == kb.TEMP_DIR / "gaps"
           and kb.domain_base("splits") ==
           kb.BASE_DIR / "splits" / "strategies"
           and kb.domain_kb("maps", Path("/k")) == Path("/k") / "maps")

    def test_kb_dir_resolution(self):
        from porter.bootstrap import kb
        print("=== 知识库目录解析 ===")
        old_root = kb.KB_ROOT
        tmp = Path(tempfile.mkdtemp(prefix="kb_t_"))
        kb.KB_ROOT = tmp / "knowledge"
        try:
            ok("R1 相对名 → KB_ROOT/<name>",
               kb.kb_dir_of({"kb_dir": "asterinas"})
               == kb.KB_ROOT / "asterinas")
            ok("R2 绝对路径原样",
               kb.kb_dir_of({"kb_dir": "/abs/kb"}) == Path("/abs/kb"))
            ok("R3 无 kb_dir → None", kb.kb_dir_of({}) is None)
            ok("R4 非 str → None",
               kb.kb_dir_of({"kb_dir": 3}) is None
               and kb.kb_dir_of(None) is None)

            ws = tmp / "ws"; ws.mkdir()
            ok("R5 工作区无 project.json → None",
               kb.kb_dir_for(ws) is None)
            (ws / "project.json").write_text(
                json.dumps({"kb_dir": "asterinas"}), encoding="utf-8")
            ok("R6 工作区解析", kb.kb_dir_for(ws) ==
               kb.KB_ROOT / "asterinas")
            (ws / "project.json").write_text("{bad", encoding="utf-8")
            ok("R7 损坏 project.json → None",
               kb.kb_dir_for(ws) is None)
        finally:
            kb.KB_ROOT = old_root
            shutil.rmtree(tmp)

    def test_thin_index(self):
        from porter.bootstrap import kb
        print("=== 薄 INDEX ===")
        tmp = Path(tempfile.mkdtemp(prefix="kb_idx_"))
        try:
            d = tmp / "gaps"
            ok("I1 缺失 → None", kb.load_index(d) is None)
            idx = kb.upsert_entry([], "msleep.md", "msleep 忙等方案")
            idx = kb.upsert_entry(idx, "msleep.md", "msleep 忙等方案 v2")
            idx = kb.upsert_entry(idx, "cond_resched.md", "yield_now 替代")
            ok("I2 upsert 不重复", len(idx) == 2
               and idx[0]["desc"] == "msleep 忙等方案 v2"
               and idx[0]["hits"] == 0)
            kb.save_index(d, idx)
            ok("I3 落盘可读",
               [e["file"] for e in kb.load_index(d)] ==
               ["msleep.md", "cond_resched.md"])
            (d / "INDEX.json").write_text("{bad", encoding="utf-8")
            ok("I4 损坏 → None", kb.load_index(d) is None)
            kb.save_index(d, idx)
            n = kb.bump_hits(idx, ["msleep.md", "ghost.md"])
            ok("I5 bump_hits 只计命中", n == 1
               and idx[0]["hits"] == 1 and idx[1]["hits"] == 0)
            n = kb.bump_hits(idx, ["msleep.md"])
            ok("I6 累计", n == 1 and idx[0]["hits"] == 2)
        finally:
            shutil.rmtree(tmp)

    def test_promote_entries(self):
        from porter.bootstrap import kb
        print("=== 通用晋升 promote_entries ===")
        old_temp = kb.TEMP_DIR
        tmp = Path(tempfile.mkdtemp(prefix="kb_pr_"))
        kb.TEMP_DIR = tmp / "ttemp"
        try:
            tdir = kb.domain_temp("gaps")   # temp/gaps
            tdir.mkdir(parents=True)
            for n in ("msleep.md", "cond_resched.md", "unindexed.md"):
                (tdir / n).write_text(f"# {n}")
            kb.save_index(tdir, [
                {"file": "msleep.md", "desc": "msleep 忙等", "hits": 2},
                {"file": "cond_resched.md", "desc": "yield 替代",
                 "hits": 0},
                {"file": "ghost.md", "desc": "文件不存在", "hits": 0}])
            kb_dir = tmp / "corpus"

            n, moved = kb.promote_entries("gaps", None, kb_dir)
            kdir = kb_dir / "gaps"
            ok("P1 全量晋升", n == 2 and moved == [
                "msleep.md", "cond_resched.md"])
            ok("P2 文件搬运", (kdir / "msleep.md").exists()
               and not (tdir / "msleep.md").exists()
               and (tdir / "unindexed.md").exists(),
               "INDEX 未登记的散文件不搬")
            tidx = kb.load_index(tdir)
            ok("P3 temp INDEX 只剩未搬运行（ghost 文件缺失不搬）",
               [e["file"] for e in tidx] == ["ghost.md"])
            didx = kb.load_index(kdir)
            ok("P4 目标 INDEX 行就位",
               [e["file"] for e in didx] ==
               ["msleep.md", "cond_resched.md"])
            ok("P5 hits 保留", didx[0]["hits"] == 2)

            # 选择性晋升 + 同名再晋升保较高 hits + desc 更新
            (tdir / "msleep.md").write_text("# msleep v2")
            kb.save_index(tdir, [
                {"file": "msleep.md", "desc": "msleep 忙等 v2", "hits": 5}])
            n, _ = kb.promote_entries("gaps", ["msleep.md"], kb_dir)
            didx = kb.load_index(kdir)
            ms = next(e for e in didx if e["file"] == "msleep.md")
            ok("P6 同名再晋升", n == 1 and ms["hits"] == 5
               and ms["desc"] == "msleep 忙等 v2"
               and (kdir / "msleep.md").read_text() == "# msleep v2")

            # 空分区 → 0
            ok("P7 空分区 → 0",
               kb.promote_entries("gaps", None, kb_dir) == (0, []))
        finally:
            kb.TEMP_DIR = old_temp
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
