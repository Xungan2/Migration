"""porter/env/probe.py 驱动级判定（check_driver）单元测试 + T3 v2
（4-session 流水线）session 循环测试。

无 agent / 无网络 / 无 docker。覆盖：
A. 四态判定：unconfigured / hit / MISS / fail_pattern 命中（+ fail-only）
B. check_driver=False（P2+ 共享方默认）：行为与旧版一致（无 driver 键）
C. judge 证据流：驱动级独立一行（<label>:driver），与内核级行分开归因
D. validate_runner 可选字段：null/缺省合法；空串/非字符串为缺陷
E. T5 门禁：未配置 → ⚠ 告警行但不拦；已配置 → 已配置行
F. T3 v2 session 循环（全 stub）：成功流/终验回炉/耗尽→总结→关口→
   答案→续跑/质量 panic/session 丢失 panic/幂等/原样重提交防重放/
   锚点缺失不烧轮/断点续跑（converged 跳过）/ut mechanism=none
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

import porter.env.probe as PB
import porter.env.extract as EX
import porter.common.agent as AG
from porter.env.extract import validate_runner
from porter.env.gate import run_gate
from porter import log as LOG
from porter.log import store as ST
from porter.loop import events as EV


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def _runner(**inj_extra):
    """合法最小 runner（stdout 日志模式，便于桩注入日志全文）。"""
    inj = {"mechanism": "env",
           "env": {"EXTRA_QEMU_ARGS": "-device <DEVICE_ARGS>"},
           "example_args": {"net": "e1000"}}
    inj.update(inj_extra)
    return {"build": {"cmd": "make", "timeout_full_sec": 60,
                      "timeout_inc_sec": 30, "success_pattern": ""},
            "boot": {"cmd": "make run", "timeout_sec": 60,
                     "log_is_stdout": True, "log_file": None,
                     "success_pattern": "BOOTED", "panic_pattern": "panic"},
            "inject_device": inj}


_BOOT_LOG = ("BOOTED\n"
             "e1000 0000:00:03.0 eth0: (PCI:33MHz:32-bit) "
             "52:54:00:12:34:56\n")


class DriverCheckTest(unittest.TestCase):
    """A/B/C：probe_boot_with_device 的驱动级判定四态 + 默认关闭语义。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porter_envprobe_t_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        EV.unbind()
        LOG.core._CTX.clear()

    def tearDown(self):
        EV.unbind()
        LOG.core._CTX.clear()

    def _run_with_dev(self, runner, stdout=_BOOT_LOG, check_driver=True):
        EV.bind(self.ws, "p0")
        fake = mock.Mock(returncode=0, stdout=stdout, stderr="")
        with mock.patch.object(PB.subprocess, "run",
                               return_value=fake), \
                redirect_stdout(io.StringIO()) as cap:
            r = PB.probe_boot_with_device(
                self.ws, self.tmp, runner, ["net"],
                label="boot_with_device", check_driver=check_driver)
        return r, cap.getvalue()

    def _judge_events(self):
        return [e for e in ST.read_events(self.ws)
                if e["kind"] == "judge"]

    def test_a1_unconfigured(self):
        r, _ = self._run_with_dev(_runner())
        ok("A1 unconfigured 不改变 ok", r["ok"] is True)
        ok("A2 driver_check 标记", r.get("driver_check") == "unconfigured")
        ok("A3 detail 追加", r["detail"].endswith("driver=unconfigured"))
        ok("A4 无驱动级 judge 行",
           not any(e["subject"] == "boot_with_device:driver"
                   for e in self._judge_events()))

    def test_a2_hit(self):
        r, _ = self._run_with_dev(
            _runner(driver_success_pattern="eth0: (PCI:33MHz:32-bit)"))
        ok("A5 命中 → ok", r["ok"] is True)
        ok("A6 driver_check=hit", r.get("driver_check") == "hit")
        drv = [e for e in self._judge_events()
               if e["subject"] == "boot_with_device:driver"]
        ok("A7 驱动级 judge 行存在且 PASS",
           drv and drv[0]["summary"].startswith("PASS"))

    def test_a3_miss(self):
        r, _ = self._run_with_dev(
            _runner(driver_success_pattern="virtio_net"))
        ok("A8 MISS → ok=False（内核三信号本身全过）", r["ok"] is False)
        ok("A9 driver_check=MISS", r.get("driver_check") == "MISS")
        evs = self._judge_events()
        kern = [e for e in evs if e["subject"] == "boot_with_device"]
        drv = [e for e in evs
               if e["subject"] == "boot_with_device:driver"]
        ok("A10 归因分离：内核行 PASS / 驱动行 FAIL",
           kern and kern[0]["summary"].startswith("PASS")
           and drv and drv[0]["summary"].startswith("FAIL"))

    def test_a4_fail_pattern_hit(self):
        log = _BOOT_LOG + "e1000: probe failed!\n"
        r, _ = self._run_with_dev(
            _runner(driver_success_pattern="eth0: (PCI:33MHz:32-bit)",
                    driver_fail_pattern="e1000: probe failed"),
            stdout=log)
        ok("A11 fail 特征命中 → ok=False", r["ok"] is False)
        ok("A12 driver_check 含 fail=hit",
           r.get("driver_check") == "hit fail=hit")

    def test_a5_fail_only_no_hit(self):
        r, _ = self._run_with_dev(
            _runner(driver_success_pattern=None,
                    driver_fail_pattern="e1000: probe failed"))
        ok("A13 仅配 fail 且未命中 → ok=True",
           r["ok"] is True and r.get("driver_check") == "unset fail=no-hit")

    def test_b1_check_driver_off(self):
        """P2+/P3-P6 共享方不传 check_driver → 结果无 driver 键（旧语义）。"""
        r, _ = self._run_with_dev(
            _runner(driver_success_pattern="virtio_net"),
            check_driver=False)
        ok("B1 ok 不受驱动特征影响", r["ok"] is True)
        ok("B2 无 driver_check 键", "driver_check" not in r)
        ok("B3 detail 无 driver 段", "driver=" not in r["detail"])

    def test_c1_device_args_substituted(self):
        """env 机制 <DEVICE_ARGS> 占位替换仍工作（机制未被重构破坏）。"""
        EV.bind(self.ws, "p0")
        fake = mock.Mock(returncode=0, stdout=_BOOT_LOG, stderr="")
        with mock.patch.object(PB.subprocess, "run",
                               return_value=fake) as mrun, \
                redirect_stdout(io.StringIO()):
            PB.probe_boot_with_device(self.ws, self.tmp, _runner(), ["net"],
                                      check_driver=True)
        env = (mrun.call_args.kwargs or {}).get("env")
        ok("C1 占位符已替换为设备实例",
           env and env.get("EXTRA_QEMU_ARGS") == "-device e1000")


class ValidateRunnerDriverTest(unittest.TestCase):
    """D：driver_* 可选字段的契约校验。"""

    def test_d1_absent_ok(self):
        ok("D1 缺省合法", not any("driver_" in d
                                  for d in validate_runner(_runner())))

    def test_d2_null_ok(self):
        r = _runner(driver_success_pattern=None, driver_fail_pattern=None)
        ok("D2 null 合法", not any("driver_" in d
                                   for d in validate_runner(r)))

    def test_d3_valid_str_ok(self):
        r = _runner(driver_success_pattern="e1000 eth0")
        ok("D3 非空字符串合法", not any("driver_" in d
                                        for d in validate_runner(r)))

    def test_d4_empty_defect(self):
        r = _runner(driver_success_pattern="  ")
        ok("D4 空串为缺陷", any("driver_success_pattern" in d
                                for d in validate_runner(r)))

    def test_d5_non_str_defect(self):
        r = _runner(driver_fail_pattern=5)
        ok("D5 非字符串为缺陷", any("driver_fail_pattern" in d
                                    for d in validate_runner(r)))


class GateDriverRowTest(unittest.TestCase):
    """E：T5 门禁对驱动级判定配置的呈现（⚠ 不拦）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porter_envgate_t_"))
        self.ws = self.tmp / "ws"
        (self.ws / "P0" / "reports").mkdir(parents=True)

    def _prime(self, runner):
        (self.ws / "project.json").write_text(json.dumps({
            "linux_driver": "linux/drivers/net/ethernet/intel/e1000",
            "target_os": str(self.tmp / "os"), "category": ["net"]}),
            encoding="utf-8")
        (self.ws / "runner.json").write_text(json.dumps(runner),
                                             encoding="utf-8")
        results = [{"item": n, "ok": True, "detail": "rc=0 …"}
                   for n in ("build", "boot", "boot_with_device")]
        (self.ws / "P0" / "reports" / "T3_development.json").write_text(
            json.dumps({"kind": "development", "results": results,
                        "hard_gate_pass": True}), encoding="utf-8")

    def test_e1_unconfigured_warn_not_fail(self):
        self._prime(_runner())
        with redirect_stdout(io.StringIO()):
            passed = run_gate(self.ws)
        ok("E1 未配置 → 门禁仍过", passed is True)
        report = (self.ws / "P0" / "reports" / "p0_report.md") \
            .read_text(encoding="utf-8")
        ok("E2 报告含 ⚠ 告警行", "⚠ 未配置 driver_success_pattern" in report)

    def test_e2_configured_row(self):
        self._prime(_runner(driver_success_pattern="e1000 eth0"))
        with redirect_stdout(io.StringIO()):
            passed = run_gate(self.ws)
        ok("E3 已配置 → 门禁过", passed is True)
        report = (self.ws / "P0" / "reports" / "p0_report.md") \
            .read_text(encoding="utf-8")
        ok("E4 报告含已配置行", "已配置" in report
           and "e1000 eth0" in report)


# ---------- F. T3 v2 session 循环（全 stub，零真实 agent/probe） ----------

def _ev_jsonl(session: str, text: str) -> str:
    """opencode --format json 风格最小事件流（含 sessionID）。"""
    return (json.dumps({"type": "step_start", "sessionID": session}) + "\n"
            + json.dumps({"type": "text", "sessionID": session,
                          "part": {"text": text}}) + "\n")


def _sec_build(**kw):
    d = {"cmd": "make all", "timeout_full_sec": 100,
         "timeout_inc_sec": 50, "success_pattern": "done"}
    d.update(kw)
    return d


def _sec_boot(**kw):
    d = {"cmd": "runit", "timeout_sec": 60, "log_is_stdout": True,
         "log_file": None, "success_pattern": "BOOTED",
         "panic_pattern": "panic"}
    d.update(kw)
    return d


def _sec_inject(**kw):
    d = {"mechanism": "env", "env": {"DEV_ARGS": "<DEVICE_ARGS>"},
         "cmd_suffix": None, "example_args": {"net": "netdev1"},
         "driver_success_pattern": None, "driver_fail_pattern": None}
    d.update(kw)
    return d


def _sec_ut_none():
    return {"mechanism": "none"}


def _md_for(cap: str, section: dict, drop_anchor: str = "",
            wrap_json: bool = False) -> str:
    """能力 md 片段（无尾部 json 块——子集等值检查已退役，
    2026-09-06 定案；wrap_json 仅保留参数位兼容旧用例，不生效）。"""
    parts = []
    for a in EX._required_anchors(cap):
        if a == drop_anchor:
            continue
        parts.append(f"{a}\n\n内容（file:line 证据）。")
    return f"## {cap}\n\n" + "\n\n".join(parts) + "\n"


class _FakeAgent:
    """_opencode_json_runner 桩：按脚本执行副作用并回放事件流。

    脚本项：{"do": "test", "req": 路径, "body": {...}}
            {"do": "frag", "json": 路径, "md": 路径, "section": {...},
             "md_text": ...}（md_text 缺省由 _md_for 生成）
            {"do": "summary", "path": ..., "text": ...}
            {"do": "nothing"}
    """

    def __init__(self, script, session="ses_T3"):
        self.script = list(script)
        self.calls = []
        self.session = session

    def __call__(self, message, workdir, log_stem, timeout_sec=0,
                 session_id=None, model=None, task=None):
        self.calls.append({"message": message, "session_id": session_id})
        act = self.script.pop(0) if self.script else {"do": "nothing"}
        kind = act.get("do")
        text = "已写入"
        if kind == "test":
            Path(act["req"]).write_text(
                json.dumps(act["body"], ensure_ascii=False),
                encoding="utf-8")
        elif kind == "frag":
            Path(act["json"]).write_text(
                json.dumps(act["section"], ensure_ascii=False, indent=2),
                encoding="utf-8")
            Path(act["md"]).write_text(
                act.get("md_text") or _md_for(
                    act["cap"], act["section"],
                    drop_anchor=act.get("drop_anchor", "")),
                encoding="utf-8")
        elif kind == "summary":
            Path(act["path"]).write_text(act["text"], encoding="utf-8")
        elif kind == "nothing":
            text = "（无产出）"
        return 0, _ev_jsonl(self.session, text)


class ExtractSessionTest(unittest.TestCase):
    """F：4-session 流水线（agent/probe 全 stub）。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="porter_t3v2_t_"))
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.os = self.tmp / "os"
        self.os.mkdir()
        (self.ws / "project.json").write_text(json.dumps(
            {"linux_driver": "drv", "target_os": str(self.os)}),
            encoding="utf-8")
        EV.unbind()
        LOG.core._CTX.clear()
        self.out = self.ws / "P0" / "reports" / "out"

    def tearDown(self):
        EV.unbind()
        LOG.core._CTX.clear()

    # ---- 公共桩 ----

    def _probes(self, build_results=None, boot_results=None,
                inject_results=None):
        """patch 四个探测入口；*_results 为 side_effect 序列（缺省恒过）。"""
        pb = lambda seq: (mock.Mock(side_effect=seq) if seq
                          else mock.Mock(return_value={"item": "x",
                                                       "ok": True,
                                                       "detail": "rc=0"}))
        b = pb(build_results)
        bo = pb(boot_results)
        inj = pb(inject_results)
        run = mock.Mock(return_value=(0, "stub output"))
        run.side_effect = \
            lambda cmd, cwd, env, timeout_sec, log_path: (
                Path(log_path).write_text("stub output\n",
                                          encoding="utf-8"), (0, "stub"))[1]
        p1 = mock.patch.object(PB, "probe_build", b)
        p2 = mock.patch.object(PB, "probe_boot", bo)
        p3 = mock.patch.object(PB, "probe_boot_with_device", inj)
        p4 = mock.patch.object(PB, "_run", run)
        for p in (p1, p2, p3, p4):
            p.start()
        self.addCleanup(lambda: [p.stop() for p in (p1, p2, p3, p4)])
        return {"build": b, "boot": bo, "inject": inj, "run": run}

    def _agent(self, script):
        fake = _FakeAgent(script)
        patcher = mock.patch.object(AG, "_opencode_json_runner", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    def _script_full(self):
        """四能力一次通过：每能力 测试请求×1 + 片段×1。"""
        o = self.out
        s = []
        s.append({"do": "test", "req": str(o / "build_test.json"),
                  "body": {"cmd": "make all"}})
        s.append({"do": "frag", "cap": "build",
                  "json": str(o / "build.json"), "md": str(o / "build.md"),
                  "section": _sec_build()})
        s.append({"do": "test", "req": str(o / "boot_test.json"),
                  "body": {"cmd": "runit"}})
        s.append({"do": "frag", "cap": "boot",
                  "json": str(o / "boot.json"), "md": str(o / "boot.md"),
                  "section": _sec_boot()})
        s.append({"do": "frag", "cap": "inject",
                  "json": str(o / "inject.json"), "md": str(o / "inject.md"),
                  "section": _sec_inject()})
        s.append({"do": "frag", "cap": "unit_test",
                  "json": str(o / "unit_test.json"),
                  "md": str(o / "unit_test.md"), "section": _sec_ut_none()})
        return s

    def _run(self):
        with redirect_stdout(io.StringIO()):
            return EX.extract_env(self.ws, self.os, [], ["net"])

    # ---- 用例 ----

    def test_f1_full_success(self):
        """四能力全过：双产物 + 冻结指纹 + T3_development + memo。"""
        self._probes()
        fake = self._agent(self._script_full())
        rc = self._run()
        ok("F1 rc=0", rc == 0)
        ok("F2 runner.json 在场",
           (self.ws / "runner.json").exists())
        ok("F3 runner.md 在场", (self.ws / "runner.md").exists())
        runner = json.loads((self.ws / "runner.json").read_text())
        ok("F4 四节齐", all(k in runner for k in
                            ("build", "boot", "inject_device",
                             "unit_test", "meta")))
        ok("F5 冻结指纹", "t3_frozen" in json.loads(
            (self.ws / "project.json").read_text()))
        dev = json.loads((self.ws / "P0" / "reports" /
                          "T3_development.json").read_text())
        items = {r["item"] for r in dev["results"]}
        ok("F6 T3_development 三行",
           items == {"build", "boot", "boot_with_device"})
        ok("F7 memo.md 在场", (self.ws / "P0" / "reports" /
                               "memo.md").exists())
        ok("F8 session 语义（每能力各起一段新会话、段内续接）",
           fake.calls[0]["session_id"] is None
           and fake.calls[1]["session_id"] == "ses_T3"
           and fake.calls[2]["session_id"] is None)
        ok("F9 后续 session prompt 引用前序片段",
           "build.json" in fake.calls[2]["message"])

    def test_f2_verify_fail_rework(self):
        """终验 FAIL 烧轮回炉，修订后收敛。"""
        self._probes(build_results=[
            {"item": "build", "ok": False, "detail": "rc=2"},
            {"item": "build", "ok": True, "detail": "rc=0"}])
        o = self.out
        s = [{"do": "frag", "cap": "build", "json": str(o / "build.json"),
              "md": str(o / "build.md"), "section": _sec_build()},
             {"do": "frag", "cap": "build", "json": str(o / "build.json"),
              "md": str(o / "build.md"),
              "section": _sec_build(cmd="make all V=1")}]
        s += self._script_full()[2:]          # boot/inject/ut 照常
        fake = self._agent(s)
        rc = self._run()
        ok("F10 回炉后 rc=0", rc == 0)
        ok("F11 FAIL 证据回灌", any("终验 FAIL" in c["message"]
                                    for c in fake.calls))
        state = json.loads((self.ws / "P0" / "reports" /
                            "t3_state.json").read_text())
        ok("F12 轮账记 2", state["caps"]["build"]["rounds_used"] == 2)

    def test_f3_exhaust_gate_answer_resume(self):
        """轮耗尽 → agent 总结 → 关口 p0.t3.build → rc 3；人答 → 续跑。"""
        self._probes()
        o = self.out
        s = [{"do": "test", "req": str(o / "build_test.json"),
              "body": {"cmd": f"try {i}"}} for i in range(EX.MAX_ROUNDS)]
        s.append({"do": "summary", "path": str(o / "build_summary.md"),
                  "text": "# 总结\n\n## 给开发者的问题\nQ1: 用什么命令？"})
        fake = self._agent(s)
        rc = self._run()
        ok("F13 耗尽 rc=3", rc == 3)
        ok("F14 总结文件在场", (o / "build_summary.md").exists())
        ok("F15 总结请求续接同 session",
           fake.calls[-1]["session_id"] == "ses_T3")
        from porter.loop import gates as gates_mod
        ledger = gates_mod.GateLedger(self.ws).load()
        g = ledger.find("p0.t3.build")
        ok("F16 关口已开", g is not None and g["status"] == "open")
        ok("F17 上下文含总结",
           any("build_summary.md" in c for c in g["context_files"]))
        state = json.loads((self.ws / "P0" / "reports" /
                            "t3_state.json").read_text())
        ok("F18 状态 exhausted",
           state["caps"]["build"]["status"] == "exhausted")
        # 人工答案入账本（模拟 process_answered_gates 之后的状态）
        g["answer"] = {"answers": "用 make everything 命令即可"}
        g["status"] = "applied"
        ledger.save()
        # 续跑：build 直接产片段（答案生效），其余照常
        resume = [{"do": "frag", "cap": "build",
                   "json": str(o / "build.json"), "md": str(o / "build.md"),
                   "section": _sec_build(cmd="make everything")}]
        resume += self._script_full()[2:]
        fake2 = self._agent(resume)
        rc = self._run()
        ok("F19 续跑 rc=0", rc == 0)
        ok("F20 答案注入续跑 prompt",
           "make everything" in fake2.calls[0]["message"])
        ok("F21 总结指针注入", "build_summary.md" in fake2.calls[0]["message"])
        ok("F22 后续能力 session 重建",
           fake2.calls[1]["session_id"] is None)

    def test_f4_quality_panic(self):
        """连续无有效产出 → RuntimeError（静态 panic）。"""
        self._probes()
        self._agent([{"do": "nothing"}] * 10)
        with self.assertRaises(RuntimeError):
            self._run()

    def test_f5_session_missing_panic(self):
        """事件流无 sessionID → RuntimeError。"""
        self._probes()
        dead = mock.Mock(return_value=(1, "TIMEOUT"))
        p = mock.patch.object(AG, "_opencode_json_runner", dead)
        p.start()
        self.addCleanup(p.stop)
        with self.assertRaises(RuntimeError):
            self._run()

    def test_f6_idempotent_reuse(self):
        """runner.json 存在 → 复用 rc 0，零 agent 调用。"""
        (self.ws / "runner.json").write_text("{}", encoding="utf-8")
        fake = self._agent([])
        rc = self._run()
        ok("F23 复用 rc=0 且零调用", rc == 0 and fake.calls == [])

    def test_f7_unchanged_resubmit_no_replay(self):
        """原样重提交被拒（不重跑终验），修订后才重验。"""
        probes = self._probes(build_results=[
            {"item": "build", "ok": False, "detail": "rc=2"},
            {"item": "build", "ok": True, "detail": "rc=0"}])
        o = self.out
        s = [{"do": "frag", "cap": "build", "json": str(o / "build.json"),
              "md": str(o / "build.md"), "section": _sec_build()},
             {"do": "frag", "cap": "build", "json": str(o / "build.json"),
              "md": str(o / "build.md"), "section": _sec_build()},
             {"do": "frag", "cap": "build", "json": str(o / "build.json"),
              "md": str(o / "build.md"),
              "section": _sec_build(cmd="make v2")}]
        s += self._script_full()[2:]
        fake = self._agent(s)
        rc = self._run()
        ok("F24 收敛 rc=0", rc == 0)
        ok("F25 未变化反馈发出", any("未变化" in c["message"]
                                      for c in fake.calls))
        ok("F26 终验只跑 2 次（防重放）",
           probes["build"].call_count == 2)

    def test_f8_anchor_missing_quality(self):
        """锚点缺失 = 质量问题（不烧轮、不跑终验）。"""
        probes = self._probes()
        o = self.out
        s = [{"do": "frag", "cap": "build", "json": str(o / "build.json"),
              "md": str(o / "build.md"), "section": _sec_build(),
              "drop_anchor": "### 实测耗时"},
             {"do": "frag", "cap": "build", "json": str(o / "build.json"),
              "md": str(o / "build.md"), "section": _sec_build()}]
        s += self._script_full()[2:]
        fake = self._agent(s)
        rc = self._run()
        ok("F27 补锚点后收敛 rc=0", rc == 0)
        ok("F28 锚点缺陷反馈", any("实测耗时" in c["message"]
                                    for c in fake.calls))
        ok("F29 首份片段未触发终验",
           probes["build"].call_count == 1)

    def test_f9_resume_converged_skip(self):
        """状态全 converged 时收官重建，零 agent 调用（断点续跑）。"""
        self._probes()
        self._agent(self._script_full())
        rc = self._run()
        assert rc == 0
        (self.ws / "runner.json").unlink()
        (self.ws / "runner.md").unlink()
        fake = self._agent([])
        rc = self._run()
        ok("F30 收官重建 rc=0 且零调用", rc == 0 and fake.calls == [])
        ok("F31 runner 双文件重建",
           (self.ws / "runner.json").exists()
           and (self.ws / "runner.md").exists())

    def test_f10_ut_none_no_probe(self):
        """ut mechanism=none：合法显式结论，不探测即收敛（合入 F1）。"""
        self._probes()
        self._agent(self._script_full())
        rc = self._run()
        state = json.loads((self.ws / "P0" / "reports" /
                            "t3_state.json").read_text())
        ok("F32 ut verify_result 为 none 结论",
           rc == 0 and state["caps"]["unit_test"]["verify_result"]["ok"]
           is True)

    def test_f11_wrapped_subset_ok(self):
        """无尾块也能收敛 + .json 包装形态解包（2026-09-06 定案：
        md 尾块等值检查退役——md 无任何 ```json 块即收敛；.json 仍认
        {"build":{...}} 包装——_parse_section 解包语义保留）。"""
        o = self.out
        s = [{"do": "frag", "cap": "build", "json": str(o / "build.json"),
              "md": str(o / "build.md"),
              "section": {EX.SECTION_KEY["build"]: _sec_build()},
              "md_text": _md_for("build", _sec_build())}]
        # boot 片段同样用包装形态落盘：验 inject 终验的前序节读取解包
        s += [{"do": "frag", "cap": "boot", "json": str(o / "boot.json"),
               "md": str(o / "boot.md"),
               "section": {EX.SECTION_KEY["boot"]: _sec_boot()},
               "md_text": _md_for("boot", _sec_boot())},
              {"do": "frag", "cap": "inject",
               "json": str(o / "inject.json"), "md": str(o / "inject.md"),
               "section": _sec_inject()},
              {"do": "frag", "cap": "unit_test",
               "json": str(o / "unit_test.json"),
               "md": str(o / "unit_test.md"), "section": _sec_ut_none()}]
        self._probes()
        self._agent(s)
        rc = self._run()
        md_text = (o / "build.md").read_text()
        ok("F33 无尾块也能收敛（md 无 json 块）",
           rc == 0 and "```json" not in md_text)
        runner = json.loads((self.ws / "runner.json").read_text())
        ok("F34 runner.json 已解包", "cmd" in runner["build"]
           and "cmd" in runner["boot"])
        ok("F35 inject 终验读到解包 boot 节（无 KeyError）", rc == 0)

    def test_f12_anchor_empty_body_defect(self):
        """锚点标题在场但正文空 = 质量缺陷（e2e 轮 3 实录）。"""
        md = _md_for("build", _sec_build())
        md_empty = md.replace("### 坑史\n\n内容（file:line 证据）。",
                              "### 坑史\n")
        self.assertNotEqual(md, md_empty)
        d_full = EX._frag_quality_defects("build", _sec_build(), md)
        d_empty = EX._frag_quality_defects("build", _sec_build(), md_empty)
        ok("F36 空小节被判缺陷",
           not any("内容为空" in x for x in d_full)
           and any("坑史」内容为空" in x for x in d_empty))

    def test_f13_injection_evidence_diff(self):
        """注入生效判据（差分语义；e2e 轮 3-6 四种绕法实录）。

        特征/token 须「注入轮命中 ∧ 裸轮未命中」；单侧/双侧命中、
        无证据均打回。
        """
        bare = "BOOTED\nFound device: Network\n"
        # ① 特征差分命中 → 过
        inj1 = _sec_inject(
            example_args={"net": "-device e1000"},
            driver_success_pattern="e1000: eth0 up")
        ok1, n1 = EX._injection_evidence_check(
            inj1, bare + "e1000: eth0 up\n", bare)
        ok("F37 特征差分命中 → 过", ok1)
        # ② 轮 6 绕法：特征锚在默认设备行（裸轮也命中）→ 打回
        inj2 = _sec_inject(
            example_args={"net": "-netdev user -net nic,netdev=e1"},
            driver_success_pattern="Found device: Network")
        ok2, n2 = EX._injection_evidence_check(inj2, bare, bare)
        ok("F38 裸轮也命中 → 打回",
           (not ok2) and "与注入无关" in n2)
        # ③ 轮 5 绕法：model= token 单侧伪造（注入轮=裸轮）→ 打回
        inj3 = _sec_inject(example_args={
            "net": "-netdev user,id=e1 -net nic,model=e1000,netdev=e1"})
        ok3, n3 = EX._injection_evidence_check(inj3, bare, bare)
        ok("F39 无差分 token → 打回",
           (not ok3) and "差分" in n3)
        # ④ 轮 6 变体：无特征无 token → 无可判定证据
        inj4 = _sec_inject(
            example_args={"net": "-netdev user -net nic,netdev=e1"},
            driver_success_pattern=None)
        ok4, n4 = EX._injection_evidence_check(inj4, bare, bare)
        ok("F40 无可判定证据 → 打回",
           (not ok4) and "无可判定证据" in n4)
        # ⑤ 特征注入轮未命中 → 打回
        ok5, n5 = EX._injection_evidence_check(inj1, bare, bare)
        ok("F41 特征注入轮未命中 → 打回",
           (not ok5) and "注入轮未命中" in n5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
