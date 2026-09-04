"""porter/env/probe.py 驱动级判定（check_driver）单元测试。

无 agent / 无网络 / 无 docker。覆盖：
A. 四态判定：unconfigured / hit / MISS / fail_pattern 命中（+ fail-only）
B. check_driver=False（P2+ 共享方默认）：行为与旧版一致（无 driver 键）
C. judge 证据流：驱动级独立一行（<label>:driver），与内核级行分开归因
D. validate_runner 可选字段：null/缺省合法；空串/非字符串为缺陷
E. T5 门禁：未配置 → ⚠ 告警行但不拦；已配置 → 已配置行
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
