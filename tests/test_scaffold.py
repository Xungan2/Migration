"""porter/bootstrap/scaffold.py + recipe_apply.py 单元测试（P2b 框架引导）。

无 agent / 无网络 / 无 docker。覆盖：
A. recipe 校验：必键 / 路径逃逸 / 形态字段 / group 正则
B. 施工引擎：分组排序插入 / 追加 / replace / marker 幂等 / 文件缺失防崩
   / driver_home 外拒建 / journal 回滚精确还原
C. 编排闭环（session 化 2026-09-05）：成功路径（manifest + mapping 批注）/
   失败回炉同会话续接（证据指针自读）/ 回炉耗尽 → 人工关口 /
   infra 日志缺失 → 抢先中止 / 同轮质量续接（缺文件、校验缺陷）/
   session 缺失与质量续接耗尽 → 静态 panic（RuntimeError）
"""

import io
import json
import re
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

SESS = "ses_T1"


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


def _ev_jsonl(text: str, session: str = SESS) -> str:
    """opencode --format json 风格的最小事件流（含 sessionID）。"""
    return (json.dumps({"type": "step_start", "sessionID": session}) + "\n"
            + json.dumps({"type": "text", "sessionID": session,
                          "part": {"type": "text", "text": text}}) + "\n")


def _out_path_from(message: str) -> Path:
    """从编排器消息里提取施工单输出路径（取最后一个 scaffold_rN.json
    ——回炉消息中 prev_out 在前、写入目标在后）。锚定绝对路径开头，
    免得把消息里的中文前缀卷进路径。"""
    ms = re.findall(r"(/[\w.\-/]*scaffold_r\d+\.json)",
                    message.replace("`", ""))
    assert ms, f"消息未携带施工单输出路径: {message[:200]!r}"
    return Path(ms[-1])


class _AgentStub:
    """_opencode_json_runner 桩：按序回放脚本并记录调用。

    脚本元素：dict=把该 recipe（{"recipe":…} 包装）写入消息指定的输出
    路径；None=不写文件（缺文件场景）；str=按原文写入（坏 JSON 场景）。
    回复文本模仿真实 agent：写成功 =「已写入 <路径>」一行。
    """

    def __init__(self, script, session=SESS):
        self.script = list(script)
        self.it = iter(self.script)
        self.calls = []

    def __call__(self, message, workdir, log_stem, timeout_sec=0,
                 session_id=None, model=None, task=None):
        self.calls.append({"message": message, "session_id": session_id,
                           "stem": str(log_stem)})
        try:
            step = next(self.it)
        except StopIteration:
            step = self.script[-1]
        if step is not None:
            p = _out_path_from(message)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(step if isinstance(step, str)
                         else json.dumps({"recipe": step},
                                         ensure_ascii=False),
                         encoding="utf-8")
            reply = f"已写入 {p}"
        else:
            reply = "（忘了写文件）"
        return 0, _ev_jsonl(reply)

    @property
    def messages(self):
        return [c["message"] for c in self.calls]


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

    def _agent_stub(self, script, session=SESS):
        """_opencode_json_runner 桩：按序回放脚本并记录调用。

        脚本元素：dict=把该 recipe 写入消息指定的输出路径（{"recipe":…}
        包装）；None=不写文件（缺文件场景）；str=按原文写入（坏 JSON）。
        """
        return _AgentStub(script, session)

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
        stub = _AgentStub([_recipe()])
        with self._patch_probes([(True, "skeleton probe hit\n", "stdout")]) \
                as (pb, bl), \
             mock.patch.object(SC.agent, "_opencode_json_runner", stub), \
             mock.patch("porter.common.vcs.commit_target") as ct, \
             redirect_stdout(io.StringIO()):
            rc = SC.run_scaffold(self.ws, self.os)
        ok("C1 成功路径 rc=0", rc == 0, rc)
        ok("C1b 单次调用（无质量续接）", len(stub.calls) == 1)
        ok("C1c agent 把 recipe 写进了下发路径",
           (self.ws / "P2/reports/out/scaffold_r1.json").exists())
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
                mock.patch.object(SC.agent, "_opencode_json_runner",
                                  _AgentStub([_recipe()])), \
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

    def test_c3_rework_loop_session(self):
        """r1 特征 MISS → 同会话续接（证据指针自读）→ r2 收敛。"""
        import porter.bootstrap.scaffold as SC
        verdicts = [(True, "BOOTED\n", "stdout"),        # r1: 特征 MISS
                    (True, "skeleton probe hit\n", "stdout")]   # r2: pass
        stub = _AgentStub([_recipe(), _recipe()])
        with self._patch_probes(verdicts), \
                mock.patch.object(SC.agent, "_opencode_json_runner", stub), \
                mock.patch("porter.common.vcs.commit_target"), \
                redirect_stdout(io.StringIO()):
            rc = SC.run_scaffold(self.ws, self.os)
        ok("C11 回炉后收敛 rc=0", rc == 0)
        ok("C11b 会话贯穿：r2 复用 r1 的 session",
           stub.calls[0]["session_id"] is None
           and stub.calls[1]["session_id"] == SESS)
        m2 = stub.messages[1]
        ok("C12 r2 消息=证据指针增量（非全量重发）",
           "SKILL: P2b" not in m2 and len(m2) < len(stub.messages[0]) // 2)
        ok("C12b 完整验证结果指针下发（自读）",
           "P2B_scaffold_verify_r1.log" in m2 and "**自行读取**" in m2)
        ok("C12c 上轮/本轮施工单路径均在消息里",
           "scaffold_r1.json" in m2 and "scaffold_r2.json" in m2)
        ev_file = self.ws / "P2" / "logs" / "P2B_scaffold_verify_r1.log"
        ok("C12d 验证结果完整落盘（含未命中特征）",
           ev_file.exists() and "未命中" in ev_file.read_text())
        ok("C12e r2 输出写的是新路径 scaffold_r2.json",
           (self.ws / "P2/reports/out/scaffold_r2.json").exists())
        m = scaffold.load_manifest(self.ws)
        ok("C13 attempts 记录", m["attempts"] == 2)

    def test_c4_exhaustion_gate(self):
        import porter.bootstrap.scaffold as SC
        from porter.loop import gates
        bad = _recipe()
        bad["acceptance_patterns"] = ["永远不命中"]
        verdicts = [(True, "skeleton probe hit\n", "stdout")] * 3
        with self._patch_probes(verdicts), \
                mock.patch.object(SC.agent, "_opencode_json_runner",
                                  _AgentStub([bad, bad, bad])), \
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
                mock.patch.object(SC.agent, "_opencode_json_runner",
                                  _AgentStub([_recipe()])), \
                mock.patch.object(gates, "panic") as gp, \
                redirect_stdout(io.StringIO()):
            rc = SC.run_scaffold(self.ws, self.os)
        ok("C16 日志缺失抢占中止 rc=3（不烧回炉轮次）",
           rc == 3 and gp.call_count == 0)

    def test_c6_quality_continuation_missing_file(self):
        """缺文件 → 同轮微增量续接（不烧轮次、同会话）→ 补写成功。"""
        import porter.bootstrap.scaffold as SC
        stub = _AgentStub([None, _recipe()])
        with self._patch_probes([(True, "skeleton probe hit\n", "stdout")]), \
                mock.patch.object(SC.agent, "_opencode_json_runner", stub), \
                mock.patch("porter.common.vcs.commit_target"), \
                redirect_stdout(io.StringIO()):
            rc = SC.run_scaffold(self.ws, self.os)
        ok("C17 缺文件经一次续接修复 rc=0", rc == 0)
        ok("C17b 续接消息=微增量（无 skill 重发）",
           "SKILL: P2b" not in stub.messages[1]
           and "施工单文件不可用" in stub.messages[1]
           and "写完" in stub.messages[1])
        ok("C17c 同会话续接", stub.calls[1]["session_id"] == SESS)
        ok("C17d 仍在第 1 轮（未烧回炉轮次）",
           "r1_R2" in stub.calls[1]["stem"])
        m = scaffold.load_manifest(self.ws)
        ok("C17e attempts=1", m["attempts"] == 1)

    def test_c6b_stale_out_file_guard(self):
        """重跑残留：上次的 scaffold_r1.json 不得被当成本次产物。"""
        import porter.bootstrap.scaffold as SC
        stale_dir = self.ws / "P2" / "reports" / "out"
        stale_dir.mkdir(parents=True)
        (stale_dir / "scaffold_r1.json").write_text(
            json.dumps({"recipe": _recipe()}), encoding="utf-8")
        stub = _AgentStub([None, _recipe()])      # 首发忘写 → 续接补写
        with self._patch_probes([(True, "skeleton probe hit\n", "stdout")]), \
                mock.patch.object(SC.agent, "_opencode_json_runner", stub), \
                mock.patch("porter.common.vcs.commit_target"), \
                redirect_stdout(io.StringIO()):
            rc = SC.run_scaffold(self.ws, self.os)
        ok("C17f 残留文件不短路质量续接（首发前已清）",
           rc == 0 and len(stub.calls) == 2)

    def test_c7_quality_continuation_validate_defects(self):
        """校验缺陷 → 同轮续接带缺陷清单 → 修复后同轮成功。"""
        import porter.bootstrap.scaffold as SC
        broken = _recipe()
        del broken["probe_channel"]
        stub = _AgentStub([broken, _recipe()])
        with self._patch_probes([(True, "skeleton probe hit\n", "stdout")]), \
                mock.patch.object(SC.agent, "_opencode_json_runner", stub), \
                mock.patch("porter.common.vcs.commit_target"), \
                redirect_stdout(io.StringIO()):
            rc = SC.run_scaffold(self.ws, self.os)
        ok("C18 校验缺陷经同轮续接修复 rc=0", rc == 0)
        ok("C18b 续接消息含缺陷清单与重写指示",
           "校验缺陷" in stub.messages[1]
           and "probe_channel" in stub.messages[1])
        m = scaffold.load_manifest(self.ws)
        ok("C18c attempts=1（缺陷不烧轮）", m["attempts"] == 1)

    def test_c8_session_missing_static_panic(self):
        """session_id 解析不到 = 静态 panic（RuntimeError，非人工关口）。"""
        import porter.bootstrap.scaffold as SC
        from porter.loop import gates
        no_sess = json.dumps(
            {"type": "text", "part": {"type": "text", "text": "没写文件"}})
        with self._patch_probes([(True, "hit", "stdout")]), \
                mock.patch.object(SC.agent, "_opencode_json_runner",
                                  return_value=(0, no_sess + "\n")) as mr, \
                mock.patch.object(gates, "panic") as gp, \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError) as cm:
                SC.run_scaffold(self.ws, self.os)
        ok("C19 静态 panic：session id not found",
           "session id not found" in str(cm.exception))
        ok("C19b 不走人工关口", gp.call_count == 0)
        ok("C19c 首次调用即 panic（不重试）", mr.call_count == 1)

    def test_c9_quality_exhausted_static_panic(self):
        """质量续接耗尽（AGENT_TRIES 次仍无文件）= 静态 panic。"""
        import porter.bootstrap.scaffold as SC
        from porter.loop import gates
        stub = _AgentStub([None, None])
        with self._patch_probes([(True, "hit", "stdout")]), \
                mock.patch.object(SC.agent, "_opencode_json_runner", stub), \
                mock.patch.object(gates, "panic") as gp, \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError) as cm:
                SC.run_scaffold(self.ws, self.os)
        ok("C20 质量续接耗尽 panic", "施工单输出质量问题" in str(cm.exception))
        ok("C20b 恰好 AGENT_TRIES 次调用", len(stub.calls) == 2)
        ok("C20c 不走人工关口", gp.call_count == 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
