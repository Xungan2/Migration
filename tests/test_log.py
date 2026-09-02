"""porter/log/ 单元测试（log 子系统：core/console/store/query）。

无 agent / 无网络 / 无 docker。覆盖：
A. record 双 sink：console 行派生/逐字/console_only/store_only、
   级别阈值（PORTER_LOG_LEVEL）
B. 上下文戳优先级：显式 > ctx > bind；mount=phase 别名；作用域退出还原
C. v1.1 附加字段：落盘/缺省不落/旧行（无新字段）可读
D. 派生事件：phase_begin/phase_end/judge
E. query：runs / context_block / timeline（S2 起充实）
F. 兼容门面：porter.loop.events 再导出同源
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from porter import log as LOG
from porter.log import console as CON
from porter.log import store as ST
from porter.loop import events as EV


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def _cap(fn, *a, **kw):
    buf = io.StringIO()
    with redirect_stdout(buf):
        r = fn(*a, **kw)
    return buf.getvalue(), r


class LogCoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porter_log_t_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        EV.unbind()
        LOG.core._CTX.clear()
        self._saved = {k: os.environ.get(k) for k in
                       ("PORTER_LOG_LEVEL", "PORTER_NO_AGENT")}

    def tearDown(self):
        EV.unbind()
        LOG.core._CTX.clear()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ.set(k, v)

    def test_a_record_dual_sink(self):
        EV.bind(self.ws, "p5")
        out, r = _cap(LOG.record, "module_done", "rx-ring",
                      "rx-ring 完成（3/16）", scope="loop")
        ok("A1 双 sink 落盘", r is True)
        ok("A2 console 行派生",
           out == "[porter] loop: rx-ring 完成（3/16）\n")
        evs = ST.read_events(self.ws)
        ok("A3 store 收到", len(evs) == 1 and evs[0]["kind"] == "module_done"
           and evs[0]["subject"] == "rx-ring")
        # console_msg 逐字（格式兼容迁移通路）
        out, _ = _cap(LOG.record, "x", console_msg="[porter] 旧样式 行",
                      console_only=True)
        ok("A4 console_msg 逐字且不落盘", out == "[porter] 旧样式 行\n"
           and len(ST.read_events(self.ws)) == 1)
        # store_only 不打印
        out, r = _cap(LOG.record, "y", summary="s", store_only=True)
        ok("A5 store_only 静默", out == "" and r is True)
        # 无 console 素材：仅 store
        out, _ = _cap(LOG.record, "z", summary="only-store")
        ok("A6 无 scope 仅落盘", out == "" and
           len(ST.read_events(self.ws)) == 3)

    def test_a_level_gate(self):
        EV.bind(self.ws, "p4")
        os.environ["PORTER_LOG_LEVEL"] = "error"
        out, _ = _cap(LOG.record, "chatter", summary="x", scope="P4")
        ok("A7 info 低于阈值不打印", out == "")
        out, _ = _cap(LOG.record, "boom", summary="x", scope="P4",
                      level="error")
        ok("A8 error 放行", out == "[porter] P4: x\n")
        ok("A9 级别落盘", ST.read_events(self.ws)[-1]["level"] == "error")
        # 默认（无环境变量）info 放行——byte 兼容
        del os.environ["PORTER_LOG_LEVEL"]
        out, _ = _cap(LOG.record, "normal", summary="x", scope="P4")
        ok("A10 默认阈值放行", out == "[porter] P4: x\n")

    def test_b_ctx_precedence(self):
        EV.bind(self.ws, "p5")
        with LOG.ctx(phase="p4", module="rx-ring", attempt=2):
            _cap(LOG.record, "slice", "rx-ring", "s1 完成",
                 store_only=True)
            # 显式覆盖 ctx
            _cap(LOG.record, "fill", "rx-ring", "alloc",
                 store_only=True, attempt=3)
        evs = ST.read_events(self.ws)
        ok("B1 ctx 戳落盘", evs[0]["phase"] == "p4"
           and evs[0]["module"] == "rx-ring"
           and evs[0]["attempt"] == 2)
        ok("B2 mount=phase 别名", evs[0]["mount"] == "p4")
        ok("B3 显式覆盖 ctx", evs[1]["attempt"] == 3)
        ok("B4 作用域退出还原", LOG.ctx_stamp() == {})
        # bind 兜底（无 ctx 无显式 phase）：mount 与 phase 同源回落
        _cap(LOG.record, "plain", summary="s", store_only=True)
        evs = ST.read_events(self.ws)
        ok("B5 bind 兜底 mount+phase", evs[2]["mount"] == "p5"
           and evs[2]["phase"] == "p5")

    def test_c_v11_fields(self):
        EV.bind(self.ws, "p4")
        ref = {"log": "P4/rx/logs/MIG_a.c_100_R1.log",
               "prompt": "P4/rx/logs/MIG_a.c_100_R1.prompt.md"}
        _cap(LOG.record, "agent_start", summary="s", store_only=True,
             run_id="P4/rx/logs/MIG_a.c_100_R1", ref=ref)
        ev = ST.read_events(self.ws)[0]
        ok("C1 run_id/ref 落盘", ev["run_id"] == "P4/rx/logs/MIG_a.c_100_R1"
           and ev["ref"] == ref)
        # 旧式调用（仅存量字段）→ 无新键（phase 自动回落 bind 除外）
        ST.append_event("cmd_end", cmd="make", rc=0)
        ev2 = ST.read_events(self.ws)[1]
        ok("C2 旧式仅自动 phase", all(k not in ev2 for k in
           ("module", "level", "run_id", "ref"))
           and ev2["phase"] == "p4")
        # 旧文件（手工构造 v1 行）可读
        with open(self.ws / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": "t", "kind": "legacy",
                                "mount": "p5", "subject": "x"}) + "\n")
        ok("C3 旧行可读", ST.read_events(self.ws)[-1]["kind"] == "legacy")

    def test_d_derived_events(self):
        EV.bind(self.ws, "p6")
        out, _ = _cap(LOG.phase_begin, "p6")
        ok("D1 phase_begin 行", out == "[porter] p6: p6 开始\n")
        _cap(LOG.phase_end, "p6", module="l4", rc=0, store_only=True)
        evs = ST.read_events(self.ws)
        ok("D2 phase 事件", evs[0]["kind"] == "phase_begin"
           and evs[1]["kind"] == "phase_end"
           and evs[1]["rc"] == 0)
        out, _ = _cap(LOG.judge, "P4_rx_a.c_100", False, "pattern=MISS",
                      intent="build", log_ref="P4/logs/x.log")
        ok("D3 judge 无 scope 静默（证据流不打 console）", out == "")
        ev = ST.read_events(self.ws)[-1]
        ok("D4 judge 字段", ev["kind"] == "judge" and ev["rc"] == 1
           and ev["level"] == "error" and ev["ref"]["log"] == "P4/logs/x.log")

    def test_f_facade_same_source(self):
        ok("F1 门面同源", EV.append_event is ST.append_event
           and EV.take_failure_snapshot is LOG.take_failure_snapshot)


class QueryTest(unittest.TestCase):
    """S2：run 登记 + prompt 归档 + query API（mock subprocess，不真调）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porter_q_t_"))
        self.ws = self.tmp / "ws"
        (self.ws / "P4" / "rx" / "logs").mkdir(parents=True)
        EV.unbind()
        LOG.core._CTX.clear()

    def tearDown(self):
        EV.unbind()
        LOG.core._CTX.clear()

    def _run(self, rc=0, out="结果文本\n尾行X", prompt="指令原文…",
             stem="P4/rx/logs/MIG_a.c_100_R1", task=None):
        import porter.common.agent as AG
        fake = mock.Mock(returncode=rc, stdout=out, stderr="")
        buf = io.StringIO()
        with mock.patch.object(AG.subprocess, "run", return_value=fake), \
                redirect_stdout(buf):
            AG.run_agent(prompt, workdir=self.ws, log_stem=str(
                self.ws / stem), task=task)
        return str(self.ws / stem), buf.getvalue()

    def test_e_run_registry(self):
        EV.bind(self.ws, "p4")
        stem, cout = self._run(task={"phase": "p4", "module": "rx",
                                     "step": "migrate", "attempt": 2})
        ok("E0 console 行 byte 兼容", cout.startswith(
            f"[porter] agent: {self.ws}/P4/rx/logs/MIG_a.c_100_R1 "
            f"(model=") and "rc=0" in cout)
        # E1 输入归档成对
        ok("E1 prompt.md 归档",
           (self.ws / "P4/rx/logs/MIG_a.c_100_R1.prompt.md")
           .read_text(encoding="utf-8") == "指令原文…")
        ok("E1b .log 成对",
           "尾行X" in (self.ws / "P4/rx/logs/MIG_a.c_100_R1.log")
           .read_text(encoding="utf-8"))
        evs = ST.read_events(self.ws)
        s, e = evs[0], evs[1]
        ok("E2 事件带 run_id/ref/戳",
           s["kind"] == "agent_start" and s["run_id"].endswith(stem)
           and s["ref"]["prompt"].endswith(".prompt.md")
           and s["module"] == "rx" and s["attempt"] == 2
           and e["rc"] == 0)
        rs = LOG.query.runs(self.ws)
        ok("E3 配对成 run", len(rs) == 1 and rs[0]["rc"] == 0
           and rs[0]["duration_sec"] is not None
           and rs[0]["log"].endswith("MIG_a.c_100_R1.log"))
        blk = LOG.query.context_block(self.ws, "P4/rx/logs/MIG_a.c_100_R1")
        ok("E4 上下文块", "rc=0" in blk and "尾行X" in blk
           and "上一次 agent 运行" in blk)
        ok("E5 无匹配空串",
           LOG.query.context_block(self.ws, "nope/zzz") == "")
        tl = LOG.query.timeline(self.ws, module="rx")
        ok("E6 时间线", [t["kind"] for t in tl] ==
           ["agent_start", "agent_end"])

    def test_e_legacy_events_pairing(self):
        """旧式事件（无 run_id/ref，intent 配对）同样可查——兼容判据。"""
        EV.bind(self.ws, "p5")
        ST.append_event("agent_start", intent="P5/logs/P5_ut_R1",
                        cmd="p")
        ST.append_event("agent_end", intent="P5/logs/P5_ut_R1", rc=1,
                        summary="bad")
        rs = LOG.query.runs(self.ws)
        ok("E7 旧式配对", len(rs) == 1 and rs[0]["rc"] == 1
           and rs[0]["log"] == "P5/logs/P5_ut_R1.log")

    def test_e_unbound_still_prints(self):
        """未绑定：console 行照打（byte 兼容旧 print），store no-op。"""
        _, cout = self._run()
        ok("E8 未绑定 console 照打", "[porter] agent:" in cout
           and ST.read_events(self.ws) == [])


class AdoptTest(unittest.TestCase):
    """A/B 收编：tail 助手 / 域事件 module 戳 / bind phase 回落。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porter_ad_t_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        EV.unbind()

    def tearDown(self):
        EV.unbind()

    def test_h_tail_helpers(self):
        ok("H1 tail_text 尾行",
           LOG.query.tail_text("a\nb\nc\nd", 2) == "c\nd")
        ok("H2 tail_text 空安全", LOG.query.tail_text("", 5) == ""
           and LOG.query.tail_text("x", 0) == "")
        (self.ws / "P4" / "logs").mkdir(parents=True)
        log = self.ws / "P4" / "logs" / "b.log"
        log.write_text("\n".join(f"l{i}" for i in range(100)),
                       encoding="utf-8")
        blk = LOG.query.tail_block(self.ws, "P4/logs/b.log", 40,
                                   "上一次构建失败（修复后重做本片）")
        ok("H3 tail_block 形态", blk.startswith(
            "\n\n---\n\n## 上一次构建失败（修复后重做本片）\n```\nl60\n")
            and blk.endswith("l99\n```"))
        ok("H4 缺文件空串", LOG.query.tail_block(
            self.ws, "nope.log", 40, "t") == "")

    def test_h_domain_module_stamp(self):
        from porter.loop import routing as RT
        EV.bind(self.ws, "loop")
        RT._record_hit(self.ws, "R1", "p3.gap.readb",
                       gate={"module": "rx-ring", "phase": "P3"})
        ev = ST.read_events(self.ws)[0]
        ok("H5 policy-hit module 戳", ev["kind"] == "policy-hit"
           and ev["module"] == "rx-ring" and ev["subject"] == "p3.gap.readb")
        hits = json.loads((self.ws / "policy_hits.json")
                          .read_text(encoding="utf-8"))
        ok("H6 计数面不受影响", hits["hits"]["R1"] == 1)

    def test_h_snapshot_event_phase(self):
        LOG.take_failure_snapshot(self.ws, "p6", "P6.boot", "r")
        ev = [e for e in ST.read_events(self.ws)
              if e["kind"] == "snapshot"][0]
        ok("H7 snapshot 事件 phase 回落", ev["mount"] == "p6"
           and ev["phase"] == "p6")


class S4Test(unittest.TestCase):
    """S4：judge 证据流 / phase 界标 / 快照钳制 / 入口 init。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porter_s4_t_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        EV.unbind()

    def tearDown(self):
        EV.unbind()

    def _runner(self):
        return {"build": {"cmd": "make kernel",
                          "timeout_full_sec": 60,
                          "success_pattern": "OK"},
                "boot": {"cmd": "make run", "timeout_sec": 60,
                         "log_file": "qemu.log",
                         "success_pattern": "BOOTED",
                         "panic_pattern": "panic"},
                "inject_device": {"mechanism": "env", "env": {}}}

    def test_g_judge_stream(self):
        import porter.env.probe as PB
        EV.bind(self.ws, "p4")
        fake = mock.Mock(returncode=0, stdout="OK", stderr="")
        with mock.patch.object(PB.subprocess, "run",
                               return_value=fake), \
                redirect_stdout(io.StringIO()):
            b = PB.probe_build(self.ws, self.tmp, self._runner(),
                               label="P4_rx_build")
        ok("G1 judge build 事件", b["ok"] is True)
        evs = [e for e in ST.read_events(self.ws) if e["kind"] == "judge"]
        ok("G2 judge 字段", evs and evs[0]["intent"] == "build"
           and evs[0]["subject"] == "P4_rx_build"
           and evs[0]["ref"]["log"].endswith("P4_rx_build.log"))
        # boot 路径（stdout 模式日志）
        runner = self._runner()
        runner["boot"]["log_is_stdout"] = True
        fake2 = mock.Mock(returncode=0, stdout="BOOTED here", stderr="")
        with mock.patch.object(PB.subprocess, "run",
                               return_value=fake2), \
                redirect_stdout(io.StringIO()):
            r = PB.probe_boot(self.ws, self.tmp, runner, label="P4_rx_boot")
        ok("G3 boot judge", r["ok"] is True)
        j = [e for e in ST.read_events(self.ws)
             if e["kind"] == "judge" and e["intent"] == "boot"]
        ok("G4 boot judge 双信号记录", j and "pattern=hit" in j[0]["summary"]
           and j[0]["phase"] == "p4")

    def test_g_phase_markers(self):
        """p3/p4/p5 成功路径打 phase_begin/end（借 test_mounts 式夹具过重，
        直接验助手行为 + run_p5 无 ctx 时不炸）。"""
        EV.bind(self.ws, "p5")
        LOG.phase_begin("p5", module="rx", store_only=True)
        LOG.phase_end("p5", module="rx", rc=0, store_only=True)
        evs = ST.read_events(self.ws)
        ok("G5 界标成对", [e["kind"] for e in evs] ==
           ["phase_begin", "phase_end"]
           and evs[0]["module"] == "rx" and evs[1]["rc"] == 0)

    def test_g_snapshot_clip(self):
        big = self.tmp / "qemu.log"
        payload = b"A" * (6 * 1024 * 1024)
        big.write_bytes(payload)
        runner = {"boot": {"cmd": "make run", "log_file": str(big)}}
        snap = LOG.take_failure_snapshot(self.ws, "p5", "m.c1", "r",
                                         runner=runner,
                                         target_os=self.tmp)
        man = json.loads((snap / "manifest.json").read_text(
            encoding="utf-8"))
        ok("G6 钳制触发", man["files"]["qemu_log"].get("clipped") is True
           and man["files"]["qemu_log"]["size"] == len(payload))
        copied = (snap / "qemu.log").stat().st_size
        ok("G7 钳制体积有界", copied < len(payload)
           and copied <= (1 + 2) * 1024 * 1024 + 4096)
        content = (snap / "qemu.log").read_bytes()
        ok("G8 头尾保留+标记", content.startswith(b"AAAA")
           and content.endswith(b"AAAA") and b"clipped" in content)

    def test_g_entry_bind(self):
        import porter.main as M
        # kb 入口 rc2 路径不应炸且未 bind；合法工作区走 bind——用 gate list
        # 不动 log；直接验 _log_bind 助手
        ws2 = self.tmp / "w2"
        ws2.mkdir()
        (ws2 / "project.json").write_text("{}", encoding="utf-8")
        M._log_bind(ws2, "kb")
        ok("G9 入口 init 绑定", (EV.bound() or {}).get("mount") == "kb")

    def test_g_runpy_events(self):
        """loop/run.py 的 print→record 迁移：module_done 等事件落账。"""
        import porter.loop.run as RUN
        from porter.loop.state import LoopState
        # 最小 loop_state + deps
        (self.ws / "P1" / "modules").mkdir(parents=True)
        (self.ws / "P1" / "modules" / "deps.json").write_text(json.dumps(
            {"order": ["modA"], "edges": {"modA": []}}), encoding="utf-8")
        (self.ws / "project.json").write_text("{}", encoding="utf-8")
        (self.ws / "loop_state.json").write_text(json.dumps(
            {"order": ["modA"],
             "modules": {"modA": {"phase": "done"}}}), encoding="utf-8")
        EV.bind(self.ws, "loop")
        with mock.patch.object(RUN.gates, "checkpoint_digest",
                               return_value=None), \
                redirect_stdout(io.StringIO()) as buf:
            rc = RUN.run_loop(self.ws)
        ok("G10 loop 完成", rc == 0)
        out = buf.getvalue()
        ok("G11 console byte 兼容", "[porter] loop: 全部模块已完成" in out)
        evs = ST.read_events(self.ws)
        ok("G12 all_done 落账", any(e["kind"] == "all_done"
                                    for e in evs))
        ok("G13 report_written 带 ref",
           any(e["kind"] == "report_written"
               and e.get("ref", {}).get("report")
               == "reports/loop_report.md" for e in evs))


class CliTest(unittest.TestCase):
    """S3：porter log CLI（tail/runs/show/timeline）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porter_cli_t_"))
        self.ws = self.tmp / "ws"
        (self.ws / "P4" / "rx" / "logs").mkdir(parents=True)
        EV.unbind()
        import porter.main as M
        self.M = M

    def tearDown(self):
        EV.unbind()

    def _main(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.M.main(["log", "--output-dir", str(self.ws), *argv])
        return rc, buf.getvalue()

    def test_c_cli(self):
        import porter.common.agent as AG
        EV.bind(self.ws, "p4")
        fake = mock.Mock(returncode=0, stdout="out1\nout2", stderr="")
        with mock.patch.object(AG.subprocess, "run", return_value=fake), \
                redirect_stdout(io.StringIO()):
            AG.run_agent("p", workdir=self.ws,
                         log_stem=str(self.ws / "P4/rx/logs/MIG_a_100_R1"),
                         task={"phase": "p4", "module": "rx"})
        rc, out = self._main("tail", "-n", "10")
        ok("C-1 tail 列出", rc == 0 and "agent_start" in out
           and "agent_end" in out)
        rc, out = self._main("tail", "--kind", "agent_end")
        ok("C-2 kind 过滤", rc == 0 and "agent_start" not in out
           and "agent_end" in out)
        rc, out = self._main("runs")
        ok("C-3 runs 列出", rc == 0 and "MIG_a_100_R1" in out
           and "rc=0" in out)
        rc, out = self._main("show", "MIG_a_100_R1")
        ok("C-4 show 尾匹配+日志", rc == 0 and '"rc": 0' in out
           and "out1" in out and "输入归档" in out)
        rc, out = self._main("show", "nope_zz")
        ok("C-5 show 未命中 rc2", rc == 2)
        rc, out = self._main("timeline", "--module", "rx")
        ok("C-6 timeline", rc == 0 and "agent_start" in out)
        # 空工作区
        empty = self.tmp / "empty"
        empty.mkdir()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.M.main(["log", "--output-dir", str(empty), "tail"])
        ok("C-7 空工作区 rc1", rc == 1)


if __name__ == "__main__":
    unittest.main()
