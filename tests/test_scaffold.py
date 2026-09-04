"""porter/bootstrap/scaffold.py + recipe_apply.py 单元测试（P2b 框架引导）。

无 agent / 无网络 / 无 docker。覆盖：
A. recipe 校验：必键 / 路径逃逸 / 形态字段 / group 正则
B. 施工引擎：分组排序插入 / 追加 / replace / marker 幂等 / 文件缺失防崩
   / driver_home 外拒建 / journal 回滚精确还原
C. 编排闭环：成功路径（manifest + mapping 批注）/ 失败回炉带证据 /
   回炉耗尽 → 人工关口 / infra 日志缺失 → 抢占中止
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter.bootstrap import recipe_apply, scaffold
from porter.loop import events as EV
from porter import log as LOG


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def _recipe(driver: str = "e1000") -> dict:
    return {
        "driver": driver,
        "language": "rust",
        "driver_home": f"kernel/core/comps/{driver}",
        "files": [
            {"relpath": f"kernel/core/comps/{driver}/src/lib.rs",
             "content": "// skeleton lib\n"},
            {"relpath": f"kernel/core/comps/{driver}/src/probes.rs",
             "content": "// dorm\n"},
        ],
        "edits": [
            {"id": "members", "file": "Cargo.toml", "action": "insert",
             "marker": f"\"kernel/core/comps/{driver}\"",
             "insert": f"    \"kernel/core/comps/{driver}\",\n",
             "group": "^\\s*\"kernel/core/comps/", "evidence": "Cargo.toml:9"},
        ],
        "acceptance_patterns": ["skeleton probe hit"],
        "probe_channel": {"dormitory_rel": "src/probes.rs",
                          "call_site_desc": "init 末尾调 run_all()",
                          "print_idiom": "ostd::info!(\"…\")",
                          "gen_rules": "init 上下文不可睡眠"},
        "test_substrate": {"marker": "#[ktest]", "how": "ktest 注册"},
        "api_claims": [{"linux_api": "pci_register_driver",
                        "usage": "注册驱动", "evidence": "pci.rs:57"}],
    }


def _runner() -> dict:
    return {"build": {"cmd": "make", "timeout_full_sec": 60,
                      "timeout_inc_sec": 30, "success_pattern": ""},
            "boot": {"cmd": "make run", "timeout_sec": 60,
                     "log_is_stdout": True, "log_file": None,
                     "success_pattern": "BOOTED", "panic_pattern": "panic"},
            "inject_device": {
                "mechanism": "env",
                "env": {"EXTRA_QEMU_ARGS": "-device <DEVICE_ARGS>"},
                "example_args": {"net": "e1000"}}}


class RecipeValidationTest(unittest.TestCase):
    """A：施工单轻校验。"""

    def test_a1_valid(self):
        ok("A1 合法施工单零缺陷",
           recipe_apply.validate_recipe(_recipe(), "e1000") == [])

    def test_a2_missing_keys(self):
        r = _recipe()
        del r["probe_channel"]
        ok("A2 缺 probe_channel 为缺陷",
           any("probe_channel" in e
               for e in recipe_apply.validate_recipe(r, "e1000")))

    def test_a3_escape(self):
        r = _recipe()
        r["files"][0]["relpath"] = "/etc/passwd"
        errs = recipe_apply.validate_recipe(r, "e1000")
        ok("A3 绝对路径为缺陷", any("逃逸" in e for e in errs))

    def test_a4_outside_home(self):
        r = _recipe()
        r["files"][0]["relpath"] = "elsewhere/lib.rs"
        ok("A4 driver_home 外为缺陷",
           any("driver_home" in e
               for e in recipe_apply.validate_recipe(r, "e1000")))

    def test_a5_replace_shape(self):
        r = _recipe()
        r["edits"][0].update(action="replace")     # 缺 find/replace
        ok("A5 replace 缺字段为缺陷",
           any("replace" in e
               for e in recipe_apply.validate_recipe(r, "e1000")))

    def test_a6_bad_group(self):
        r = _recipe()
        r["edits"][0]["group"] = "([unclosed"
        ok("A6 group 正则非法为缺陷",
           any("group" in e
               for e in recipe_apply.validate_recipe(r, "e1000")))


class ApplyEngineTest(unittest.TestCase):
    """B：施工引擎 + 回滚。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porter_scaffold_t_"))
        self.os = self.tmp / "os"
        (self.os / "kernel" / "core" / "comps").mkdir(parents=True)
        (self.os / "Cargo.toml").write_text(
            'members = [\n    "kernel/core/comps/zzz",\n]\n',
            encoding="utf-8")
        self.journal = self.tmp / "journal.json"

    def test_b1_insert_sorted(self):
        res = recipe_apply.apply_recipe(self.os, _recipe(), self.journal)
        text = (self.os / "Cargo.toml").read_text(encoding="utf-8")
        ok("B1 组间字典序插入（e1000 在 zzz 前）",
           text.index("comps/e1000") < text.index("comps/zzz"))
        ok("B2 created 记录", len(res["created"]) == 2)
        ok("B3 无跳过", res["skipped"] == [])

    def test_b2_marker_idempotent(self):
        recipe_apply.apply_recipe(self.os, _recipe(), self.journal)
        text1 = (self.os / "Cargo.toml").read_text(encoding="utf-8")
        res2 = recipe_apply.apply_recipe(self.os, _recipe(), self.journal)
        text2 = (self.os / "Cargo.toml").read_text(encoding="utf-8")
        ok("B4 二次 apply 幂等", text1 == text2
           and res2["created"] == [] and res2["edits_applied"] == ["members(已在)"])

    def test_b3_missing_file_no_crash(self):
        r = _recipe()
        r["edits"].append({"id": "x", "file": "nope/Kconfig",
                           "action": "insert", "marker": "mk",
                           "insert": "x\n"})
        res = recipe_apply.apply_recipe(self.os, r, self.journal)
        ok("B5 目标文件缺失 ⚠ 跳过不崩",
           "x@nope/Kconfig" in res["skipped"]
           and any("不存在" in w for w in res["warnings"]))

    def test_b4_outside_home_refused(self):
        r = _recipe()
        r["files"].append({"relpath": "outside/lib.rs", "content": "x"})
        res = recipe_apply.apply_recipe(self.os, r, self.journal)
        ok("B6 driver_home 外拒建",
           not (self.os / "outside").exists()
           and "outside/lib.rs" in res["skipped"])

    def test_b5_replace_and_rollback(self):
        (self.os / "net.c").write_text("if (virtio_net()) {\n",
                                       encoding="utf-8")
        r = _recipe()
        r["edits"] = [{"id": "pref", "file": "net.c", "action": "replace",
                       "marker": "e1000_probe() ||",
                       "find": "if (virtio_net()) {",
                       "replace": "if (e1000_probe() || virtio_net()) {"}]
        recipe_apply.apply_recipe(self.os, r, self.journal)
        ok("B7 replace 生效", "e1000_probe() ||" in
           (self.os / "net.c").read_text(encoding="utf-8"))
        recipe_apply.rollback(self.os, self.journal)
        ok("B8 回滚还原原文", (self.os / "net.c").read_text(
            encoding="utf-8") == "if (virtio_net()) {\n")

    def test_b6_rollback_full(self):
        before = (self.os / "Cargo.toml").read_text(encoding="utf-8")
        recipe_apply.apply_recipe(self.os, _recipe(), self.journal)
        ok("B9 文件已建", (self.os / "kernel/core/comps/e1000/src/lib.rs")
           .exists())
        recipe_apply.rollback(self.os, self.journal)
        ok("B10 回滚删除新建文件", not (
            self.os / "kernel/core/comps/e1000").exists())
        ok("B11 回滚还原编辑", (self.os / "Cargo.toml")
           .read_text(encoding="utf-8") == before)

    def test_b7_insert_no_group_appends(self):
        (self.os / "Kbuild").write_text("obj-y += core/\n", encoding="utf-8")
        r = _recipe()
        r["edits"] = [{"id": "k", "file": "Kbuild", "action": "insert",
                       "marker": "obj-y += drivers/e1000/",
                       "insert": "obj-y += drivers/e1000/"}]
        recipe_apply.apply_recipe(self.os, r, self.journal)
        text = (self.os / "Kbuild").read_text(encoding="utf-8")
        ok("B12 无 group 追加末尾（不插顶部）",
           text == "obj-y += core/\nobj-y += drivers/e1000/\n")


class ScaffoldOrchestrationTest(unittest.TestCase):
    """C：编排闭环（agent/probe/门禁全打桩）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porter_scaff_o_"))
        self.ws = self.tmp / "ws"
        self.os = self.tmp / "os"
        (self.os / "kernel" / "core" / "comps").mkdir(parents=True)
        (self.os / "Cargo.toml").write_text(
            'members = [\n    "kernel/core/comps/zzz",\n]\n',
            encoding="utf-8")
        self.ws.mkdir()
        drv = self.tmp / "drv" / "e1000"
        drv.mkdir(parents=True)
        (self.ws / "project.json").write_text(json.dumps({
            "linux_driver": str(drv), "target_os": str(self.os),
            "category": ["net"]}), encoding="utf-8")
        (self.ws / "runner.json").write_text(json.dumps(_runner()),
                                             encoding="utf-8")
        EV.unbind()
        LOG.core._CTX.clear()

    def tearDown(self):
        EV.unbind()
        LOG.core._CTX.clear()

    def _agent_outputs(self, recipes: list[dict]):
        it = iter(recipes)

        def _fake(prompt, workdir, log_stem, timeout_sec=0, task=None):
            try:
                r = next(it)
            except StopIteration:
                r = recipes[-1]
            return 0, json.dumps({"recipe": r})
        return _fake

    def _patch_probes(self, verdicts: list[dict]):
        """verdicts: 每次 boot_and_log 返回 (ok, log, state)。"""
        import contextlib
        it = iter(verdicts)
        from porter.env import probe as env_probe
        from porter.loop import probes as loop_probes

        def _boot(ws, phase_dir, target_os, proj, label):
            try:
                v = next(it)
            except StopIteration:
                v = verdicts[-1]
            return v

        @contextlib.contextmanager
        def _cm():
            with mock.patch.object(
                    env_probe, "probe_build",
                    return_value={"item": "b", "ok": True,
                                  "detail": "rc=0"}) as pb, \
                    mock.patch.object(loop_probes, "boot_and_log",
                                      side_effect=_boot) as bl:
                yield pb, bl
        return _cm()

    def test_c1_success(self):
        import porter.bootstrap.scaffold as SC
        with self._patch_probes([(True, "skeleton probe hit\n", "stdout")]) \
                as (pb, bl), \
             mock.patch.object(SC.agent, "run_agent",
                               side_effect=self._agent_outputs([_recipe()])), \
             mock.patch("porter.common.vcs.commit_target") as ct, \
             redirect_stdout(io.StringIO()):
            rc = SC.run_scaffold(self.ws, self.os)
        ok("C1 成功路径 rc=0", rc == 0, rc)
        m = scaffold.load_manifest(self.ws)
        ok("C2 manifest 落盘含宿舍/契约",
           m and m["dormitory"].endswith("src/probes.rs")
           and m["probe_channel"]["print_idiom"])
        ok("C3 验证结论入 manifest",
           m["verified"]["patterns"] == {"skeleton probe hit": 1})
        ok("C4 vcs commit 被调", ct.call_count == 1)
        ok("C5 目标树已施工", (self.os / "kernel/core/comps/e1000/src/lib.rs")
           .exists())
        ok("C6 幂等：重跑复用", SC.run_scaffold(self.ws, self.os) == 0)

    def test_c2_mapping_annotation(self):
        import porter.bootstrap.scaffold as SC
        (self.ws / "P2").mkdir()
        (self.ws / "P2" / "mapping.json").write_text(json.dumps({
            "entries": [
                {"linux_api": "pci_register_driver", "kind": "function",
                 "verdict": "adapt", "target": "X", "evidence": "a.rs:1",
                 "notes": "", "risk": "med", "confidence": "medium",
                 "domain": "linux/pci.h"},
                {"linux_api": "unrelated", "kind": "function",
                 "verdict": "gap", "target": "", "evidence": "", "notes": "",
                 "risk": "low", "confidence": "low", "domain": "x"}],
            "redesigns": []}), encoding="utf-8")
        with self._patch_probes([(True, "skeleton probe hit\n", "stdout")]), \
                mock.patch.object(SC.agent, "run_agent",
                                  side_effect=self._agent_outputs([_recipe()])), \
                mock.patch("porter.common.vcs.commit_target"), \
                redirect_stdout(io.StringIO()):
            rc = SC.run_scaffold(self.ws, self.os)
        ok("C7 rc=0", rc == 0)
        ents = json.loads((self.ws / "P2" / "mapping.json")
                          .read_text(encoding="utf-8"))["entries"]
        e = next(x for x in ents if x["linux_api"] == "pci_register_driver")
        ok("C8 批注：confidence→high + verified_by",
           e["confidence"] == "high"
           and "verified_by=scaffold(P2b)" in e["notes"])
        ok("C9 保守：risk 不动（med 保持）", e["risk"] == "med")
        g = next(x for x in ents if x["linux_api"] == "unrelated")
        ok("C10 gap 条目不动", g["confidence"] == "low")

    def test_c3_rework_loop(self):
        import porter.bootstrap.scaffold as SC
        verdicts = [(True, "BOOTED\n", "stdout"),        # r1: 特征 MISS
                    (True, "skeleton probe hit\n", "stdout")]   # r2: pass
        prompts = []

        def _fake(prompt, workdir, log_stem, timeout_sec=0, task=None):
            prompts.append(prompt)
            return 0, json.dumps({"recipe": _recipe()})
        with self._patch_probes(verdicts), \
                mock.patch.object(SC.agent, "run_agent", side_effect=_fake), \
                mock.patch("porter.common.vcs.commit_target"), \
                redirect_stdout(io.StringIO()):
            rc = SC.run_scaffold(self.ws, self.os)
        ok("C11 回炉后收敛 rc=0", rc == 0)
        ok("C12 第 2 轮 prompt 携带失败证据",
           len(prompts) == 2 and "验证失败证据" in prompts[1]
           and "未命中" in prompts[1])
        m = scaffold.load_manifest(self.ws)
        ok("C13 attempts 记录", m["attempts"] == 2)

    def test_c4_exhaustion_gate(self):
        import porter.bootstrap.scaffold as SC
        from porter.loop import gates
        bad = _recipe()
        bad["acceptance_patterns"] = ["永远不命中"]
        verdicts = [(True, "skeleton probe hit\n", "stdout")] * 3
        with self._patch_probes(verdicts), \
                mock.patch.object(SC.agent, "run_agent",
                                  side_effect=self._agent_outputs([bad])), \
                mock.patch.object(gates, "panic") as gp, \
                redirect_stdout(io.StringIO()):
            rc = SC.run_scaffold(self.ws, self.os)
        ok("C14 回炉耗尽 rc=3", rc == 3)
        ok("C15 人工关口 p2.scaffold.fail",
           gp.call_count == 1
           and gp.call_args.args[1]["id"] == "p2.scaffold.fail")

    def test_c5_infra_preempt(self):
        import porter.bootstrap.scaffold as SC
        from porter.loop import gates
        with self._patch_probes([(False, "", "missing")]), \
                mock.patch.object(SC.agent, "run_agent",
                                  side_effect=self._agent_outputs([_recipe()])), \
                mock.patch.object(gates, "panic") as gp, \
                redirect_stdout(io.StringIO()):
            rc = SC.run_scaffold(self.ws, self.os)
        ok("C16 日志缺失抢占中止 rc=3（不烧回炉轮次）",
           rc == 3 and gp.call_count == 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
