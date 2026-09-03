"""porter/common/vcs.py 单元测试（mock git 子进程；unittest 形态）。

覆盖（git 管理模块）：
  A. 底层封装：identity 注入 / 禁用零调用 / commit 流（paths 暂存 →
     幂等空变更 → hash 返回）/ 台账写入
  B. 分支：ensure_branch 四态（在位/切回/新建/失败）
  C. 仓发现：并行多仓 + 嵌套坍缩 + 构建目录跳过
  D. hook_p0：登记/分支生成/用户分支冲突 rc2/resume 幂等
  E. commit_target：路径→仓映射 / status 捕获
  F. 可移植：export_all（增量/全量 bundle + manifest）/ import_bundle
  G. commit_chain：台账 → git show 文件清单
运行：python3 tests/test_vcs.py 或 unittest discover
"""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.common import vcs


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


class _GitMock:
    """假 git：记录全部调用；routes 键 = 子命令参数前缀（最长匹配）。"""

    def __init__(self, routes=None, default=(0, "")):
        self.calls: list[list[str]] = []
        self.routes = routes or {}
        self.default = default

    def _tail(self, cmd):
        i = 3                                    # git -C <repo>
        while i < len(cmd) and cmd[i] == "-c":
            i += 2
        return cmd[i:] if i < len(cmd) else []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        tail = self._tail(cmd)
        best = None
        for key, val in self.routes.items():
            kt = key.split()
            if tail[:len(kt)] == kt and (best is None
                                         or len(kt) > len(best[0].split())):
                best = (key, val)
        if best is not None:
            rc, out = best[1]() if callable(best[1]) else best[1]
            return SimpleNamespace(returncode=rc, stdout=out, stderr="")
        rc, out = self.default
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")

    def args_of(self, key) -> list[list[str]]:
        """子命令参数前缀为 key 的调用（返回 key 之后的参数尾段）。"""
        kt = key.split()
        out = []
        for cmd in self.calls:
            tail = self._tail(cmd)
            if tail[:len(kt)] == kt:
                out.append(tail[len(kt):])
        return out


class VcsBase(unittest.TestCase):
    """公共：PORTER_VCS=1 隔离 + 空 config + 临时目录。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="porter_vcs_t_"))
        self._env = mock.patch.dict(os.environ, {"PORTER_VCS": "1"})
        self._env.start()
        self._cfg = mock.patch.object(vcs, "_load_cfg",
                                      return_value={"enabled": True})
        self._cfg.start()

    def tearDown(self):
        self._cfg.stop()
        self._env.stop()
        shutil.rmtree(self._tmp, ignore_errors=True)

    def ws(self, name="ws", git=True, proj=None) -> Path:
        w = self._tmp / name
        w.mkdir(parents=True, exist_ok=True)
        if git:
            (w / ".git").mkdir(exist_ok=True)
        (w / "project.json").write_text(json.dumps(proj or {
            "linux_driver": "/drv/e1000", "target_os": str(self._tmp / "os"),
        }), encoding="utf-8")
        return w

    def gitmock(self, routes=None, default=(0, "")):
        g = _GitMock(routes, default)
        p = mock.patch.object(vcs.subprocess, "run", side_effect=g)
        p.start()
        self.addCleanup(p.stop)
        return g


# ---------- A. 底层封装 ----------

class TestLowlevel(VcsBase):

    def test_identity_injected(self):
        with mock.patch.object(vcs, "_load_cfg", return_value={
                "enabled": True,
                "identity": {"name": "porter", "email": "p@x.io"}}):
            g = self.gitmock()
            vcs._git(self._tmp, "status")
        cmd = g.calls[0]
        ok("A1 user.name 注入", "user.name=porter" in cmd)
        ok("A2 user.email 注入", "user.email=p@x.io" in cmd)
        ok("A3 gpgsign 关闭", "commit.gpgsign=false" in cmd)
        ok("A4 用户 hook 跳过", "core.hooksPath=/dev/null" in cmd)

    def test_disabled_zero_calls(self):
        w = self.ws()
        with mock.patch.dict(os.environ, {"PORTER_VCS": "0"}):
            g = self.gitmock()
            h = vcs.commit_workspace(w, "m")
            hs = vcs.commit_target(w, "m")
        ok("A5 enabled=false → 全跳过", h is None and hs == [])
        ok("A6 零 git 调用", g.calls == [])

    def test_workspace_not_repo_noop(self):
        w = self.ws(git=False)
        g = self.gitmock()
        ok("A7 无 .git → no-op", vcs.commit_workspace(w, "m") is None)
        ok("A8 零 git 调用", g.calls == [])

    def test_commit_flow_and_ledger(self):
        w = self.ws()
        g = self.gitmock({
            "diff --cached --quiet": (1, ""),      # 有暂存变更
            "rev-parse HEAD": (0, "deadbeef\n"),
        })
        h = vcs.commit_workspace(w, "P0: done", phase="P0")
        ok("A9 返回 hash", h == "deadbeef")
        add_tail = g.args_of("add")[0]
        ok("A10 add -A + 排除台账/exports",
           add_tail[0:3] == ["-A", "--", "."]
           and ":(exclude)vcs_commits.json" in add_tail
           and ":(exclude)exports" in add_tail)
        msg = g.args_of("commit")[0][1]
        ok("A11 消息含 trailer", "Porter-Phase: P0" in msg
           and "P0: done" in msg)
        led = vcs.load_ledger(w)
        ok("A12 台账写入", len(led) == 1 and led[0]["hash"] == "deadbeef"
           and led[0]["repo_kind"] == "workspace"
           and led[0]["phase"] == "P0")

    def test_commit_idempotent(self):
        w = self.ws()
        g = self.gitmock({"diff --cached --quiet": (0, "")})  # 无变更
        ok("A13 空变更 → None", vcs.commit_workspace(w, "m") is None)
        ok("A14 不产生 commit", g.args_of("commit") == [])
        ok("A15 台账空", vcs.load_ledger(w) == [])

    def test_commit_filters_nonexistent(self):
        w = self.ws()
        g = self.gitmock()
        ok("A16 路径全不存在 → None",
           vcs.commit(w, "m", paths=["ghost.rs"]) is None)
        ok("A17 不产生 add", g.args_of("add") == [])

    def test_commit_paths_staged(self):
        w = self.ws()
        (w / "a.rs").write_text("x", encoding="utf-8")
        g = self.gitmock({
            "diff --cached --quiet": (1, ""),
            "rev-parse HEAD": (0, "h1\n"),
        })
        h = vcs.commit(w, "m", paths=["a.rs", "ghost.rs"])
        ok("A18 指定路径 commit", h == "h1")
        ok("A19 add 只带存在路径", g.args_of("add") == [["--", "a.rs"]])


# ---------- B. 分支 ----------

class TestBranch(VcsBase):

    def test_ensure_branch_states(self):
        r = self._tmp / "r1"
        r.mkdir()
        # 在位
        g = self.gitmock({"branch --show-current": (0, "porter/x\n")})
        ok("B1 已在分支 → True", vcs.ensure_branch(r, "porter/x"))
        ok("B2 无 checkout", g.args_of("checkout") == [])
        # 存在但不在位 → 切回
        state = {"cur": "main"}
        g = self.gitmock({
            "branch --show-current": lambda: (0, state["cur"] + "\n"),
            "rev-parse": (0, "refs/heads/porter/x\n"),
            "checkout": lambda: (0, state.update({"cur": "porter/x"}) or ""),
        })
        ok("B3 切回既有分支", vcs.ensure_branch(r, "porter/x"))
        ok("B4 checkout 不带 -b", g.args_of("checkout") == [["porter/x"]])
        # 不存在 → 新建
        g = self.gitmock({
            "branch --show-current": (0, "main\n"),
            "rev-parse": (1, ""),
        })
        ok("B5 新建分支", vcs.ensure_branch(r, "porter/y"))
        ok("B6 checkout -b", g.args_of("checkout") == [["-b", "porter/y"]])
        # 切换失败（脏树冲突）→ False
        g = self.gitmock({
            "branch --show-current": (0, "main\n"),
            "rev-parse": (0, "r\n"),
            "checkout": (1, "error: Your local changes..."),
        })
        ok("B7 冲突 → False", not vcs.ensure_branch(r, "porter/z"))


# ---------- C. 仓发现 ----------

class TestRegisterRepos(VcsBase):

    def test_parallel_and_nested(self):
        # 场景 1：顶层自身是 git 仓 → 只管顶层（内部一切嵌套仓不管理）
        os_ = self._tmp / "os"
        for sub in ("", "sub", "par", "par/deep", "target", "regular"):
            (os_ / sub / ".git").mkdir(parents=True, exist_ok=True)
        with mock.patch.object(vcs, "head", return_value="B0"):
            repos = vcs.register_repos(os_)
        roots = {r["root"] for r in repos}
        ok("C1 顶层仓保留", str(os_) in roots)
        ok("C2 顶层在位 → 内部仓全为嵌套不管",
           roots == {str(os_)}, roots)
        ok("C3 baseline 记录", all(r["baseline"] == "B0" for r in repos))

        # 场景 2：顶层不是 git 仓 → 并行子仓各自管理，嵌套坍缩到最外层
        tree = self._tmp / "flat"
        for sub in ("kernel", "libs", "libs/deep", "apps"):
            (tree / sub / ".git").mkdir(parents=True, exist_ok=True)
        (tree / "docs").mkdir(parents=True, exist_ok=True)
        with mock.patch.object(vcs, "head", return_value="B1"):
            repos2 = vcs.register_repos(tree)
        roots2 = {r["root"] for r in repos2}
        ok("C4 并行仓各自保留",
           roots2 == {str(tree / "kernel"), str(tree / "libs"),
                      str(tree / "apps")}, roots2)
        ok("C5 嵌套坍缩（libs/deep 并入 libs）",
           str(tree / "libs" / "deep") not in roots2)

    def test_gen_branch_name(self):
        n = vcs.gen_branch_name("e1000")
        ok("C6 分支名格式",
           re.fullmatch(r"porter/e1000-\d{8}-[0-9a-f]{4}", n), n)
        n2 = vcs.gen_branch_name("we ird/name")
        ok("C7 驱动名净化", n2.startswith("porter/we-ird-name-"), n2)


# ---------- D. hook_p0 ----------

class TestHookP0(VcsBase):

    def test_register_and_branch(self):
        tos = self._tmp / "os"
        tos.mkdir()
        w = self.ws(proj={"linux_driver": "/drv/e1000",
                          "target_os": str(tos)})
        with mock.patch.object(vcs, "register_repos",
                               return_value=[{"root": str(tos),
                                              "baseline": "B0"}]), \
             mock.patch.object(vcs, "branch_exists", return_value=False), \
             mock.patch.object(vcs, "ensure_branch",
                               return_value=True) as eb, \
             mock.patch.object(vcs, "init_repo", return_value=True) as ir:
            rc = vcs.hook_p0(w, tos)
        ok("D1 rc 0", rc == 0)
        vv = json.loads((w / "project.json").read_text(
            encoding="utf-8"))["vcs"]
        ok("D2 project.json['vcs'] 写入", vv["branch"].startswith(
            "porter/e1000-") and vv["repos"][0]["baseline"] == "B0")
        ok("D3 目标仓建分支", eb.call_count >= 1)
        ok("D4 工作区 init 同分支", ir.call_args[0][1] == vv["branch"])

    def test_user_branch_clash(self):
        tos = self._tmp / "os"
        tos.mkdir()
        w = self.ws(proj={"linux_driver": "/drv/e1000",
                          "target_os": str(tos)})
        with mock.patch.object(vcs, "register_repos",
                               return_value=[{"root": str(tos),
                                              "baseline": "B0"}]), \
             mock.patch.object(vcs, "branch_exists", return_value=True):
            rc = vcs.hook_p0(w, tos, branch="porter/old")
        ok("D5 分支已存在 → rc 2", rc == 2)

    def test_resume_idempotent(self):
        tos = self._tmp / "os"
        tos.mkdir()
        w = self.ws(proj={"linux_driver": "/drv/e1000",
                          "target_os": str(tos),
                          "vcs": {"branch": "porter/x",
                                  "repos": [{"root": str(tos),
                                             "baseline": "B0"}]}})
        with mock.patch.object(vcs, "register_repos") as rr, \
             mock.patch.object(vcs, "ensure_branch",
                               return_value=True) as eb, \
             mock.patch.object(vcs, "init_repo", return_value=True):
            rc = vcs.hook_p0(w, tos)
        ok("D6 resume rc 0", rc == 0)
        ok("D7 不重复登记", rr.call_count == 0)
        ok("D8 只补齐分支", eb.call_count == 1)

    def test_no_repos_still_inits_ws(self):
        tos = self._tmp / "os"
        tos.mkdir()
        w = self.ws(proj={"linux_driver": "/drv/e1000",
                          "target_os": str(tos)})
        with mock.patch.object(vcs, "register_repos", return_value=[]), \
             mock.patch.object(vcs, "init_repo", return_value=True) as ir:
            rc = vcs.hook_p0(w, tos)
        ok("D9 非 git 树 → rc 0", rc == 0)
        ok("D10 工作区照常 init（无分支名）", ir.call_args[0][1] is None)
        vv = json.loads((w / "project.json").read_text(
            encoding="utf-8"))["vcs"]
        ok("D11 空登记落盘", vv == {"branch": None, "repos": []})


# ---------- E. commit_target ----------

class TestCommitTarget(VcsBase):

    def _ws2repos(self):
        tos = self._tmp / "osA"
        other = self._tmp / "osB"
        for d in (tos, other):
            (d / "kernel" / "core" / "comps" / "e1000" / "src").mkdir(
                parents=True, exist_ok=True)
            (d / ".git").mkdir(exist_ok=True)
        (tos / "kernel" / "core" / "comps" / "e1000" / "src" / "lib.rs") \
            .write_text("x", encoding="utf-8")
        w = self.ws(proj={"linux_driver": "/drv/e1000", "target_os": str(tos),
                          "vcs": {"branch": None,
                                  "repos": [{"root": str(tos),
                                             "baseline": "B"},
                                            {"root": str(other),
                                             "baseline": "C"}]}})
        return w, tos, other

    def test_paths_map_to_owning_repo(self):
        w, tos, other = self._ws2repos()
        g = self.gitmock({
            "diff --cached --quiet": (1, ""),
            "rev-parse HEAD": (0, "hA\n"),
        })
        hs = vcs.commit_target(
            w, "P4[m]: fill + migrate",
            paths=["kernel/core/comps/e1000"], phase="P4")
        ok("E1 单仓命中", hs == ["hA"])
        adds = {tuple(a) for a in g.args_of("add")}
        ok("E2 只 add 归属仓路径",
           adds == {("--", "kernel/core/comps/e1000")})
        repos_called = {c[2] for c in g.calls}
        ok("E3 其他仓零调用", str(other) not in repos_called)
        ok("E4 台账 repo_kind=target_os",
           vcs.load_ledger(w)[0]["repo_kind"] == "target_os")

    def test_capture_all_repos(self):
        w, tos, other = self._ws2repos()
        g = self.gitmock({
            "diff --cached --quiet": (1, ""),
            "rev-parse HEAD": (0, "h\n"),
        })
        hs = vcs.commit_target(w, "solve: fix-code x")
        ok("E5 status 捕获逐仓 commit", hs == ["h", "h"])
        ok("E6 每仓 add -A", len(g.args_of("add")) == 2)

    def test_legacy_fallback_top_repo(self):
        tos = self._tmp / "osC"
        (tos / ".git").mkdir(parents=True)
        w = self.ws(proj={"linux_driver": "/drv/e1000",
                          "target_os": str(tos)})   # 无 vcs 节
        g = self.gitmock({
            "diff --cached --quiet": (1, ""),
            "rev-parse HEAD": (0, "h\n"),
        })
        ok("E7 旧工作区兜底顶层仓（绝对路径 + git 仓）",
           vcs.commit_target(w, "m") == ["h"])

    def test_rejects_cwd_and_tool_repo(self):
        # 2026-09-04 事故回归：缺/相对 target_os 时 Path("") 归一化为 CWD，
        # dirty-CWD 下把进程所在的工具仓误提交（8 条 solve[d1] commit）。
        g = self.gitmock()
        w1 = self.ws(proj={"linux_driver": "/drv/e1000"})   # 缺 target_os
        ok("E8 缺 target_os → 拒绝（不落 CWD）",
           vcs.commit_target(w1, "m") == [])
        w2 = self.ws(proj={"linux_driver": "/drv/e1000",
                           "target_os": "relative/os"})
        ok("E9 相对 target_os → 拒绝", vcs.commit_target(w2, "m") == [])
        w3 = self.ws(proj={"linux_driver": "/drv/e1000",
                           "target_os": str(vcs.TOOL_ROOT)})
        ok("E10 工具仓自身 → 拒绝", vcs.commit_target(w3, "m") == [])
        ok("E11 全程零 git 调用", g.calls == [])


# ---------- F. 可移植 ----------

class TestPortability(VcsBase):

    def test_export_all(self):
        tos = self._tmp / "os"
        tos.mkdir()
        (tos / ".git").mkdir()
        w = self.ws(proj={"linux_driver": "/drv/e1000",
                          "target_os": str(tos),
                          "vcs": {"branch": "porter/x",
                                  "repos": [{"root": str(tos),
                                             "baseline": "B0"}]}})
        g = self.gitmock()
        m = vcs.export_all(w)
        ok("F1 manifest 返回", m.get("branch") == "porter/x"
           and len(m["files"]) == 2)
        bundles = g.args_of("bundle")
        ok("F2 目标仓增量包", ["create", str(w / "exports" /
           "target-os-0-os.bundle"), "B0..HEAD"] in bundles)
        ok("F3 工作区全量包", ["create", str(w / "exports" /
           "workspace.bundle"), "HEAD"] in bundles)
        man = json.loads((w / "exports" / "manifest.json")
                         .read_text(encoding="utf-8"))
        ok("F4 manifest.json 落盘", man["branch"] == "porter/x"
           and {f["bundle"] for f in man["files"]}
           == {"target-os-0-os.bundle", "workspace.bundle"})

    def test_import_bundle(self):
        r = self._tmp / "repo"
        r.mkdir()
        (r / ".git").mkdir()
        (self._tmp / "b.bundle").write_text("x", encoding="utf-8")
        g = self.gitmock({"rev-parse": (1, "")})     # 分支不存在 → 新建
        okk, detail = vcs.import_bundle(self._tmp / "b.bundle", r,
                                        branch="porter/x")
        ok("F5 import 成功", okk and "porter/x" in detail)
        ok("F6 fetch HEAD", g.args_of("fetch")[0][0:1] == [str(
            self._tmp / "b.bundle")] and g.args_of("fetch")[0][-1] == "HEAD")
        ok("F7 建分支自 FETCH_HEAD",
           g.args_of("branch") == [["porter/x", "FETCH_HEAD"]])
        ok("F8 缺 bundle → False",
           not vcs.import_bundle(self._tmp / "nope.bundle", r)[0])
        ok("F9 非 git 仓 → False",
           not vcs.import_bundle(self._tmp / "b.bundle", self._tmp / "nr")[0])


# ---------- G. commit 链 ----------

class TestCommitChain(VcsBase):

    def test_chain_files_parsed(self):
        w = self.ws()
        vcs._ledger_append(w, {"repo_kind": "workspace", "repo": str(w),
                               "phase": "P4", "msg": "P4[m]", "hash": "H1"})
        g = self.gitmock({"show": (0, "M\tkernel/core/comps/e1000/src/lib.rs\n"
                                   "A\tCargo.toml\ncommit-meta\n")})
        chain = vcs.commit_chain(w)
        ok("G1 台账条目展开", len(chain) == 1)
        files = {f["status"]: f["path"] for f in chain[0]["files"]}
        ok("G2 文件状态解析", files.get("M", "").endswith("lib.rs")
           and files.get("A") == "Cargo.toml")

    def test_chain_empty_ledger(self):
        w = self.ws()
        self.gitmock()
        ok("G3 空台账 → 空链", vcs.commit_chain(w) == [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
