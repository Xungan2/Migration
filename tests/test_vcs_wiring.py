"""porter vcs 接线层测试（不执行真 run_agent；mock git / patch 断言）。

覆盖：
  A. agent 隔离 seam：agent_pre/agent_post（仅工作区仓；消息/台账/幂等）
  B. 隔离语义（真 git，缺 git 跳过）：pre→产物→post，diff 恰为该次产物
  C. 接线点：panic 停车 commit / answers 消费 commit / loop 模块 done
     commit / P4 末目标树 commit / P2 末双 commit
  D. 工作区 .gitignore 写入幂等
运行：python3 tests/test_vcs_wiring.py 或 unittest discover
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.common import vcs

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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
            from types import SimpleNamespace
            return SimpleNamespace(returncode=rc, stdout=out, stderr="")
        rc, out = self.default
        from types import SimpleNamespace
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")

    def commit_msgs(self) -> list[str]:
        """全部 git commit -m <msg> 的消息列表。"""
        out = []
        for tail in (self._tail(c) for c in self.calls):
            if tail[:1] == ["commit"] and "-m" in tail:
                out.append(tail[tail.index("-m") + 1])
        return out


_CFG = {"enabled": True,
        "identity": {"name": "porter-test", "email": "t@porter.local"}}


class WiringBase(unittest.TestCase):
    """公共：PORTER_VCS=1 + 测试身份 config + 临时目录。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="porter_vcs_w_"))
        self._env = mock.patch.dict(os.environ, {"PORTER_VCS": "1"})
        self._env.start()
        self._cfg = mock.patch.object(vcs, "_load_cfg", return_value=_CFG)
        self._cfg.start()
        # 隔离 candidates 钩子的全局 temp 落点（防污染工具仓 knowledge/temp）
        from porter.bootstrap import kb as _kb
        self._temp = mock.patch.object(_kb, "TEMP_DIR",
                                       self._tmp / "kbtemp")
        self._temp.start()
        self.addCleanup(self._temp.stop)

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


# ---------- A. agent 隔离 seam ----------

class TestAgentSeam(WiringBase):

    def test_pre_post_messages_and_ledger(self):
        w = self.ws()
        g = self.gitmock({
            "diff --cached --quiet": (1, ""),
            "rev-parse HEAD": (0, "aaa111\n"),
        })
        h1 = vcs.agent_pre(w, "P4/hw/logs/MIG_s2_R1")
        h2 = vcs.agent_post(w, "P4/hw/logs/MIG_s2_R1", 0)
        ok("A1 返回 hash", h1 == "aaa111" and h2 == "aaa111")
        msgs = g.commit_msgs()
        ok("A2 pre/agent 成对消息",
           len(msgs) == 2 and msgs[0] == "pre-agent: P4/hw/logs/MIG_s2_R1\n\n"
           "Porter-Phase: agent"
           and msgs[1] == "agent: P4/hw/logs/MIG_s2_R1 rc=0\n\n"
           "Porter-Phase: agent")
        led = vcs.load_ledger(w)
        ok("A3 台账成对相邻", [(e["msg"].split(":")[0], e["phase"])
                              for e in led] == [("pre-agent", "agent"),
                                                ("agent", "agent")])

    def test_post_records_nonzero_rc(self):
        w = self.ws()
        g = self.gitmock({
            "diff --cached --quiet": (1, ""),
            "rev-parse HEAD": (0, "b\n"),
        })
        vcs.agent_post(w, "P2/logs/MAP_b1", 127)
        ok("A4 失败调用也留痕（rc=127）",
           "agent: P2/logs/MAP_b1 rc=127" in g.commit_msgs()[0])

    def test_noop_cases(self):
        g = self.gitmock()
        ok("A5 无 .git → no-op",
           vcs.agent_pre(self.ws(git=False), "x") is None)
        with mock.patch.dict(os.environ, {"PORTER_VCS": "0"}):
            ok("A6 禁用 → no-op", vcs.agent_pre(self.ws(), "x") is None)
        g2 = self.gitmock({"diff --cached --quiet": (0, "")})
        ok("A7 无变更 → 不产空 commit",
           vcs.agent_post(self.ws(), "x", 0) is None
           and g2.commit_msgs() == [])


# ---------- B. 隔离语义（真 git） ----------

@unittest.skipUnless(shutil.which("git"), "需要 git 二进制")
class TestAgentIsolationRealGit(WiringBase):

    def test_diff_pre_post_is_exactly_call_artifacts(self):
        w = self._tmp / "realws"
        w.mkdir()
        (w / "project.json").write_text('{"linux_driver": "/d/e1000"}',
                                        encoding="utf-8")
        vcs._git(w, "init")
        stem = "P4/hw/logs/MIG_s2_R1"
        h1 = vcs.agent_pre(w, stem)
        ok("B1 pre commit 产生", h1 is not None)
        # 模拟该次调用的 ws 侧产物（编排器写的 prompt/log）
        d = w / "P4" / "hw" / "logs"
        d.mkdir(parents=True)
        (d / f"{stem.split('/')[-1]}.prompt.md").write_text("p", encoding="utf-8")
        (d / f"{stem.split('/')[-1]}.log").write_text("l", encoding="utf-8")
        h2 = vcs.agent_post(w, stem, 0)
        ok("B2 post commit 产生", h2 is not None and h2 != h1)
        rc, out = vcs._git(w, "diff", "--name-only", h1, h2)
        files = sorted(ln for ln in out.splitlines() if ln)
        ok("B3 diff pre..post 恰为该次调用产物",
           files == ["P4/hw/logs/MIG_s2_R1.log",
                     "P4/hw/logs/MIG_s2_R1.prompt.md"], files)
        ok("B4 再 post 无变更 → None", vcs.agent_post(w, stem, 0) is None)
        led = vcs.load_ledger(w)
        ok("B5 台账两条相邻",
           [e["msg"].split(":")[0] for e in led] == ["pre-agent", "agent"])


# ---------- C. 接线点 ----------

class TestWiringPoints(WiringBase):

    def test_panic_stop_commit(self):
        from porter.loop import gates as gates_mod
        w = self.ws()
        g = self.gitmock({
            "diff --cached --quiet": (1, ""),
            "rev-parse HEAD": (0, "c0ffee\n"),
        })
        rc = gates_mod.panic(w, {
            "id": "loop.attempts.hw-p4", "kind": "retry",
            "gate_type": "failure", "phase": "P5", "module": "hw",
            "question": "attempts 烧穿"})
        ok("C1 panic 返回 3", rc == 3)
        msgs = g.commit_msgs()
        ok("C2 停车前 commit",
           any(m.startswith("stop: loop.attempts.hw-p4") for m in msgs),
           msgs)
        ok("C3 phase trailer",
           any("Porter-Phase: p5" in m for m in msgs))

    def test_answers_consumed_commit(self):
        from porter.loop import gates as gates_mod
        w = self.ws()
        led = gates_mod.GateLedger(w).load()
        led.add(**{
            "id": "g1", "kind": "memo", "gate_type": "decision",
            "phase": "P0", "blocking": False, "question": "看一眼",
            "answer_form": [{"field": "note", "type": "text",
                             "required": False}],
        })
        led.save()
        (w / "answers.md").write_text("## @g1\nnote: 已确认\n",
                                      encoding="utf-8")
        g = self.gitmock({
            "diff --cached --quiet": (1, ""),
            "rev-parse HEAD": (0, "d1\n"),
        })
        applied, invalid = gates_mod.process_answered_gates(w)
        ok("C4 答案被消费", applied == 1 and invalid == 0)
        ok("C5 answers commit",
           any(m.startswith("answers: 1 applied") for m in g.commit_msgs()))

    def test_loop_module_done_commit(self):
        from porter.loop import run as run_mod
        w = self.ws()
        (w / "P1" / "modules").mkdir(parents=True)
        (w / "P1" / "modules" / "deps.json").write_text(json.dumps(
            {"order": ["m1"], "edges": {}}), encoding="utf-8")
        with mock.patch.object(run_mod.p3, "run_p3", return_value=0), \
             mock.patch.object(run_mod.p4, "run_p4", return_value=0), \
             mock.patch.object(run_mod.p5, "run_p5", return_value=0), \
             mock.patch.object(vcs, "commit_workspace") as cw:
            rc = run_mod.run_loop(w)
        called = [c.args[1] for c in cw.call_args_list
                  if len(c.args) >= 2]
        ok("C6 loop 模块 done commit",
           "loop: module m1 done" in called, called)
        ok("C7 loop 正常推进（FM 检查点停车也视为到达）",
           rc in (0, 3), rc)

    def test_p4_end_target_commit(self):
        from porter.loop import p4 as p4_mod
        w = self.ws()
        (w / "runner.json").write_text("{}", encoding="utf-8")
        rep = w / "P3" / "m1" / "reports"
        rep.mkdir(parents=True)
        for f in ("surface.json", "criteria.json", "gap_decisions.json"):
            (rep / f).write_text("{}", encoding="utf-8")
        with mock.patch.object(p4_mod, "_step_fill", return_value=0), \
             mock.patch.object(p4_mod, "_step_migrate",
                               return_value=(0, [])), \
             mock.patch.object(p4_mod, "_quick_smoke", return_value=True), \
             mock.patch.object(vcs, "commit_target") as ct:
            rc = p4_mod.run_p4(w, "m1", ["m1"])
        ok("C8 run_p4 桩跑通过", rc == 0, rc)
        ok("C9 P4 末目标树 commit",
           ct.call_count == 1
           and ct.call_args.args[1] == "P4[m1]: fill + migrate"
           and ct.call_args.kwargs.get("phase") == "P4")
        paths = ct.call_args.kwargs.get("paths") or []
        ok("C10 commit 范围含 crate 与接线文件",
           paths[0] == "kernel/core/comps/e1000"
           and "Cargo.toml" in paths, paths)

    def test_p2_end_commits(self):
        from porter.bootstrap import run as p2run
        from porter.bootstrap import mapping, skeleton, pregen
        w = self.ws()
        (w / "runner.json").write_text("{}", encoding="utf-8")
        (w / "P1" / "modules").mkdir(parents=True)
        (w / "P1" / "modules" / "deps.json").write_text(
            '{"order": [], "edges": {}}', encoding="utf-8")
        with mock.patch.object(mapping, "run_map", return_value=0), \
             mock.patch.object(skeleton, "run_skeleton", return_value=0), \
             mock.patch.object(pregen, "run_pregen", return_value=0), \
             mock.patch.object(p2run, "_acceptance", return_value=True), \
             mock.patch.object(vcs, "commit_target") as ct, \
             mock.patch.object(vcs, "commit_workspace") as cws:
            rc = p2run.run_p2(w, self._tmp / "drv", self._tmp / "os")
        ok("C11 run_p2 桩跑通过", rc == 0, rc)
        paths = ct.call_args.kwargs.get("paths") or []
        ok("C12 P2 末目标树 commit（无 manifest → 接线面）",
           ct.call_count == 1
           and all(p in paths for p in ("Cargo.toml", "Components.toml",
                                        "kernel/core/src/net/iface/init.rs")),
           paths)
        ok("C13 P2 末工作区 commit",
           cws.call_args.args[1] == "P2: done")


# ---------- D. 工作区 .gitignore ----------

class TestWsGitignore(WiringBase):

    def test_write_idempotent(self):
        w = self.ws()
        ok("D1 首写成功", vcs.write_ws_gitignore(w))
        gi = (w / ".gitignore").read_text(encoding="utf-8")
        ok("D2 两条目齐", "/vcs_commits.json" in gi.splitlines()
           and "/exports/" in gi.splitlines())
        vcs.write_ws_gitignore(w)
        gi2 = (w / ".gitignore").read_text(encoding="utf-8")
        ok("D3 幂等（内容不变）", gi == gi2)

    def test_appends_to_foreign(self):
        w = self.ws()
        (w / ".gitignore").write_text("*.secret\n", encoding="utf-8")
        ok("D4 追加成功", vcs.write_ws_gitignore(w))
        lines = (w / ".gitignore").read_text(encoding="utf-8").splitlines()
        ok("D5 原有内容保留", "*.secret" in lines)
        ok("D6 条目补齐", "/exports/" in lines
           and "/vcs_commits.json" in lines)


if __name__ == "__main__":
    unittest.main(verbosity=2)
