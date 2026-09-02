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

    def test_select_kb(self):
        from porter.bootstrap import kb
        print("=== p0 --kb 选择（select_kb）===")
        old_root, old_base, old_tool = kb.KB_ROOT, kb.BASE_DIR, kb.TOOL_ROOT
        tmp = Path(tempfile.mkdtemp(prefix="kb_sel_"))
        kb.KB_ROOT = tmp / "knowledge"
        kb.BASE_DIR = kb.KB_ROOT / "base"
        kb.TOOL_ROOT = tmp
        try:
            (kb.BASE_DIR / "splits" / "strategies").mkdir(parents=True)
            (kb.BASE_DIR / "splits" / "strategies" / "INDEX.json").write_text(
                "[]", encoding="utf-8")

            ok("S1 new 复制 base", kb.select_kb("new", "corpus") ==
               kb.KB_ROOT / "corpus"
               and (kb.KB_ROOT / "corpus" / "splits" / "strategies" /
                    "INDEX.json").exists())
            ok("S2 new 已存在 → 拒绝",
               kb.select_kb("new", "corpus") is None)
            ok("S3 new 空目录", kb.select_kb("new", "empty1",
                                             empty=True) is not None
               and not any((kb.KB_ROOT / "empty1").iterdir()))
            for bad in ("base", "temp", "a/b", "", "..x", "with space"):
                ok(f"S4 非法名 {bad!r} → 拒绝",
                   kb.select_kb("new", bad) is None)
            ok("S5 非法模式", kb.select_kb("clone", "foo") is None)
            ok("S6 use 不存在 → 拒绝", kb.select_kb("use", "ghost")
               is None)
            ok("S7 use 既有", kb.select_kb("use", "corpus") ==
               kb.KB_ROOT / "corpus")

            # git ignore 追加
            (tmp / ".gitignore").write_text("migrations/\n",
                                            encoding="utf-8")
            kb.select_kb("new", "ignored", empty=True, git_ignore=True)
            gi = (tmp / ".gitignore").read_text(encoding="utf-8")
            ok("S8 gitignore 追加", "knowledge/ignored/" in gi.splitlines())
            kb.select_kb("new", "ignored2", empty=True, git_ignore=True)
            gi2 = (tmp / ".gitignore").read_text(encoding="utf-8")
            ok("S9 重复追加不重复",
               gi2.count("knowledge/ignored/") == 1)
        finally:
            kb.KB_ROOT, kb.BASE_DIR, kb.TOOL_ROOT = \
                old_root, old_base, old_tool
            shutil.rmtree(tmp)

    def test_p0_kb_decision(self):
        import argparse
        from porter.bootstrap import kb
        from porter import main as M
        print("=== p0 知识库决策（rc2 逼显式 + 记录）===")
        old_root, old_base = kb.KB_ROOT, kb.BASE_DIR
        tmp = Path(tempfile.mkdtemp(prefix="kb_p0_"))
        kb.KB_ROOT = tmp / "knowledge"
        kb.BASE_DIR = kb.KB_ROOT / "base"
        try:
            (kb.BASE_DIR / "splits").mkdir(parents=True)
            ws = tmp / "ws"
            args = argparse.Namespace(
                linux_driver="/x", target_os="/y", materials=None,
                output_dir=str(ws), category=None,
                kb=None, kb_empty=False, kb_git="track")

            # rc 2：新工作区无 --kb
            rc, name = M._p0_kb_decision(args, ws / "project.json")
            ok("D1 无 --kb 新工作区 → rc 2", rc == 2 and name is None)

            # rc 2：旧工作区未记录 kb_dir
            ws.mkdir()
            (ws / "project.json").write_text(
                json.dumps({"linux_driver": "/x"}), encoding="utf-8")
            rc, name = M._p0_kb_decision(args, ws / "project.json")
            ok("D2 无 --kb 旧工作区无记录 → rc 2", rc == 2)

            # 复用：已记录 kb_dir
            (ws / "project.json").write_text(
                json.dumps({"linux_driver": "/x", "kb_dir": "corpus"}),
                encoding="utf-8")
            rc, name = M._p0_kb_decision(args, ws / "project.json")
            ok("D3 已记录 → 复用", rc is None and name is None)

            # --kb use 既有目录 → 决策成功
            (kb.KB_ROOT / "corpus").mkdir(parents=True)
            args.kb = ["use", "corpus"]
            rc, name = M._p0_kb_decision(args, ws / "project.json")
            ok("D4 --kb use 既有 → 通过", rc is None and name == "corpus")

            # --kb new 非法 → rc 2
            args.kb = ["new", "base"]
            rc, name = M._p0_kb_decision(args, ws / "project.json")
            ok("D5 --kb new 保留名 → rc 2", rc == 2)

            # _record_kb 落盘
            args.kb = ["use", "corpus"]
            _rc, name = M._p0_kb_decision(args, ws / "project.json")
            M._record_kb(ws / "project.json", name)
            proj = json.loads((ws / "project.json").read_text(
                encoding="utf-8"))
            ok("D6 kb_dir 记入 project.json", proj["kb_dir"] == "corpus")
            M._record_kb(ws / "project.json", name)  # 幂等
            ok("D7 记录幂等",
               json.loads((ws / "project.json").read_text(
                   encoding="utf-8"))["kb_dir"] == "corpus")

            # cmd_p0 端到端 rc 2（新工作区、无 --kb——T1 之前即返回）
            ws2 = tmp / "ws2"
            args2 = argparse.Namespace(
                linux_driver="/x", target_os="/y", materials=None,
                output_dir=str(ws2), category=None,
                kb=None, kb_empty=False, kb_git="track")
            import io, contextlib
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = M.cmd_p0(args2)
            ok("D8 cmd_p0 无 --kb → rc 2（指引打印）", rc == 2
               and "未指定知识库目录" in buf.getvalue()
               and not ws2.exists())
        finally:
            kb.KB_ROOT, kb.BASE_DIR = old_root, old_base
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
