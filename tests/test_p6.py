"""porter/loop/p6.py 单元测试（无 agent / 无网络 / 无 docker；unittest 形态）。

覆盖（P6 系统验收工具地基，plan: P6-1）：
H. config/review_gates：缺省 agent / human 读取
I. defects.json 账本：登记 / 四字段强制闭账 / 泊车 / 持久化
J. 聚合模式：acceptance（P5 优先 + 旧 P4 兼容）/ deferred 全景 /
   L4 e2e 待定稿计数 → health.json/.md
K. L4 定稿门：schema 校验 / agent 续跑 / human 停车（exit 3 +
   REVIEW.md + questions）→ answers.md 放行 → finalized
L. 执行模式：SLIRP boot / 全判据重判 / deferred 哨兵清偿 +
   __P5__→__P6__ 规范化 / L4 判定（含 park）/ 判定与退出码 /
   硬失败路径 / 待 L4 pending 语义
运行：python3 tests/test_p6.py 或 unittest discover
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def setUpModule():
    # 挂载②红项分诊会走 agent 兜底——本模块单测禁真调（§15 实施纪律）
    os.environ.setdefault("PORTER_NO_AGENT", "1")
    # execute/诊断路径测试：强制开 §15 自诊（仓级 config 默认 bypass）
    os.environ.setdefault("PORTER_SELF_DIAGNOSIS", "1")


def tearDownModule():
    os.environ.pop("PORTER_NO_AGENT", None)
    os.environ.pop("PORTER_SELF_DIAGNOSIS", None)


from porter.loop import p6 as P6


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


# ---------- fixture ----------

_L4_DRAFT = [
    {"id": "modA.arp-e2e", "title": "ARP 真实收发", "form": "流量驱动",
     "expr": "L4 modA\\.arp-e2e PASS", "rationale": "SLIRP 回应 ARP",
     "disposition": "clear"},
    {"id": "modA.regs-dump", "title": "regs dump", "form": "内核自测",
     "expr": "", "rationale": "ethtool 面已 skip——泊车", "disposition": "park"},
]


def _mk_p6_ws() -> tuple[Path, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="porter_p6_t_"))
    ws = tmp / "ws"
    tos = tmp / "os"
    tos.mkdir(parents=True)
    (ws / "P3" / "modA" / "reports").mkdir(parents=True)
    (ws / "P3" / "modB" / "reports").mkdir(parents=True)
    (ws / "P4" / "modA" / "reports").mkdir(parents=True)
    (ws / "P5" / "modB" / "reports").mkdir(parents=True)
    (ws / "project.json").write_text(json.dumps(
        {"target_os": str(tos), "linux_driver": str(tmp / "drv"),
         "category": ["net"]}), encoding="utf-8")
    (ws / "runner.json").write_text(json.dumps({
        "build": {"cmd": "true", "timeout_full_sec": 5,
                  "success_pattern": ""},
        "boot": {"cmd": "true", "timeout_sec": 5, "log_file": "boot.log",
                 "success_pattern": "OK", "panic_pattern": "panic"},
        "inject_device": {
            "mechanism": "env", "env": {"EXTRA_QEMU_ARGS": "<DEVICE_ARGS>"},
            "example_args": {"net": "-device e1000",
                             "net-user": "-netdev user,id=e1 "
                                         "-device e1000,netdev=e1"}},
        "unit_test": {"mechanism": "cargo-osdk-test", "cmd": "true",
                      "timeout_sec": 5, "success_pattern": "passed; 0 failed;",
                      "fail_pattern": "failures:"}}), encoding="utf-8")
    (ws / "loop_state.json").write_text(json.dumps({
        "order": ["modA", "modB"],
        "modules": {"modA": {"phase": "done", "attempts": {}},
                    "modB": {"phase": "done", "attempts": {},
                             "skipped": True,
                             "skip_reason": "no face"}}}), encoding="utf-8")
    (ws / "P3" / "modA" / "reports" / "criteria.json").write_text(
        json.dumps({"criteria": [
            {"id": "modA.compile", "layer": "L1", "kind": "compile",
             "expr": "", "deferred_by": None},
            {"id": "modA.boot", "layer": "L2", "kind": "boot",
             "expr": "", "deferred_by": None},
            {"id": "modA.hello-log", "layer": "L3", "kind": "log_pattern",
             "expr": "HELLO=[1-9]", "deferred_by": None},
            {"id": "modA.thing-ut", "layer": "L0", "kind": "unit_test",
             "expr": "thing_test", "deferred_by": None},
            {"id": "modA.arp-e2e", "layer": "L4", "kind": "e2e",
             "expr": "", "deferred_by": None},
            {"id": "modA.later-log", "layer": "L3", "kind": "log_pattern",
             "expr": "LATER", "deferred_by": ["modB"]}]}),
        encoding="utf-8")
    (ws / "P3" / "modB" / "reports" / "criteria.json").write_text(
        json.dumps({"criteria": []}), encoding="utf-8")
    # modA 验收在旧位置（P4/）；modB 在新位置（P5/）
    (ws / "P4" / "modA" / "reports" / "acceptance.json").write_text(
        json.dumps({"module": "modA", "pass": True, "results": []}),
        encoding="utf-8")
    (ws / "P5" / "modB" / "reports" / "acceptance.json").write_text(
        json.dumps({"module": "modB", "pass": True, "results": []}),
        encoding="utf-8")
    # deferred：哨兵 open（旧 __P5__）+ cleared + e2e open（待泊车）
    (ws / "deferred.json").write_text(json.dumps({"entries": [
        {"id": "modA.sentinel-log", "module": "modA",
         "criterion": {"id": "modA.sentinel-log", "layer": "L3",
                       "kind": "log_pattern", "expr": "SENT=[1-9]",
                       "deferred_by": ["__P5__"]},
         "deferred_by": ["__P5__"], "status": "open",
         "registered": "2026-08-31T00:00:00", "history": []},
        {"id": "modA.done-log", "module": "modA",
         "criterion": {"id": "modA.done-log", "layer": "L3",
                       "kind": "log_pattern", "expr": "X",
                       "deferred_by": ["modB"]},
         "deferred_by": ["modB"], "status": "cleared",
         "registered": "2026-08-31T00:00:00", "history": []},
        {"id": "modA.regs-dump", "module": "modA",
         "criterion": {"id": "modA.regs-dump", "layer": "L4", "kind": "e2e",
                       "expr": "", "deferred_by": ["__P5__"]},
         "deferred_by": ["__P5__"], "status": "open",
         "registered": "2026-08-31T00:00:00", "history": []}]}),
        encoding="utf-8")
    (ws / "answers.md").write_text("# answers\n", encoding="utf-8")
    return ws, tos


def _write_draft(ws: Path, criteria=None):
    (ws / "P6" / "reports").mkdir(parents=True, exist_ok=True)
    (ws / "P6" / "reports" / "l4_criteria.json").write_text(
        json.dumps({"status": "draft",
                    "criteria": criteria if criteria is not None
                    else _L4_DRAFT}), encoding="utf-8")


def _patch_exec(monkey_boot_log: str):
    """打桩 build/boot/ktest（手工 monkey-patch，finally 恢复）。"""
    saved = (P6.probe_mod.probe_build, P6.probe_mod.probe_boot,
             P6.probe_mod._run)

    def fake_build(ws, target_os, runner, label="build"):
        return {"item": label, "ok": True, "detail": "rc=0"}

    def fake_boot(ws, target_os, runner, extra_env=None, cmd_suffix=None,
                  label="boot"):
        (target_os / "boot.log").write_text(monkey_boot_log,
                                            encoding="utf-8")
        return {"item": label, "ok": True, "detail": "rc=0"}

    def fake_run(cmd, cwd, env, timeout_sec, log_path):
        out = "test result: ok. 2 passed; 0 failed;\n" \
              "test thing_test ... ok\n"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(out, encoding="utf-8")
        return 0, out

    P6.probe_mod.probe_build = fake_build
    P6.probe_mod.probe_boot = fake_boot
    P6.probe_mod._run = fake_run
    return saved


def _restore_exec(saved):
    P6.probe_mod.probe_build, P6.probe_mod.probe_boot, P6.probe_mod._run \
        = saved


_AGENT = {"review_gates": {"l4_criteria_finalization": "agent"}}
_HUMAN = {"review_gates": {"l4_criteria_finalization": "human"}}

_BOOT_GOOD = ("Successfully booted\nHELLO=5\nSENT=7\n"
              "L4 modA.arp-e2e PASS arp ok\n")


# ---------- H. config ----------

class TestConfig(unittest.TestCase):
    def test_config(self):
        print("H. config/review_gates")
        ok("缺失配置 → {}", P6.load_config(Path("/nonexistent/x.json")) == {})
        ok("缺省门 = agent", P6.review_gate_mode({}) == "agent")
        ok("human 读取", P6.review_gate_mode(_HUMAN) == "human")
        ok("非法值回退 agent", P6.review_gate_mode(
            {"review_gates": {"l4_criteria_finalization": "x"}}) == "agent")


# ---------- I. defects ----------

class TestDefects(unittest.TestCase):
    def test_defects(self):
        print("I. defects.json 账本")
        ws, _tos = _mk_p6_ws()
        e = P6.add_defect(ws, "RX-PATH", "RX 数据通路未通",
                          "probe_receive rx_bytes=0")
        ok("登记 open", e["status"] == "open" and e["attempts"] == 0
           and e["discovered"]["evidence"].startswith("probe_receive"))
        try:
            P6.close_defect(ws, "RX-PATH", "", "fix", "ev")
            raise SystemExit("应抛 ValueError")
        except ValueError as ex:
            ok("闭账四字段强制", "root_cause" in str(ex))
        ok("attempts bump", P6.bump_defect(ws, "RX-PATH", "D2",
                                           "第一次尝试") == 1)
        e2 = P6.close_defect(ws, "RX-PATH", "QEMU 无 LBM_MAC 回环",
                             "改 PHY loopback", "p6 --execute 全绿")
        ok("闭账 fixed", e2["status"] == "fixed"
           and e2["regression_evidence"] == "p6 --execute 全绿")
        e3 = P6.park_defect(ws, "RX-PATH2" if False else "REGS", "x") \
            if False else None
        P6.add_defect(ws, "REGS", "regs-dump 挂账", "ethtool skip")
        P6.park_defect(ws, "REGS", "随 ethtool 面未来落地")
        d = P6.load_defects(ws)
        ok("持久化", {x["id"] for x in d["defects"]} == {"RX-PATH", "REGS"}
           and next(x for x in d["defects"]
                    if x["id"] == "REGS")["status"] == "parked")
        try:
            P6.close_defect(ws, "NOPE", "a", "b", "c")
            raise SystemExit("应抛 ValueError")
        except ValueError:
            ok("不存在缺陷拒绝", True)
        shutil.rmtree(ws.parent)


# ---------- J. 聚合 ----------

class TestAggregate(unittest.TestCase):
    def test_aggregate(self):
        print("J. 聚合模式")
        ws, _tos = _mk_p6_ws()
        rc = P6.run_p6(ws)
        ok("rc=0", rc == 0)
        h = json.loads((ws / "P6" / "reports" / "health.json")
                       .read_text(encoding="utf-8"))
        ok("两模块", len(h["modules"]) == 2)
        ma = next(m for m in h["modules"] if m["module"] == "modA")
        mb = next(m for m in h["modules"] if m["module"] == "modB")
        ok("modA 旧位置 acceptance 兼容", ma["acceptance_pass"] is True
           and ma["acceptance_legacy"])
        ok("modB 新位置 + skipped", mb["acceptance_pass"] is True
           and mb["skipped"])
        ok("L4 e2e 待定稿计数", h["e2e_pending_total"] == 1)
        ok("deferred open 2 / cleared 1", h["deferred"]["open"] == 2
           and h["deferred"]["cleared"] == 1)
        ok("哨兵识别", all(e["sentinel"] for e in h["deferred"]["open_entries"]))
        ok("health.md 落盘", (ws / "P6" / "reports" / "health.md").exists())
        shutil.rmtree(ws.parent)


# ---------- K. L4 定稿门 ----------

class TestFinalizeGate(unittest.TestCase):
    def test_gate(self):
        print("K. L4 定稿门（两模式各一验）")
        ws, _tos = _mk_p6_ws()
        # 缺草案
        ok("缺草案 rc=2", P6.run_p6(ws, finalize_flag=True, cfg=_AGENT) == 2)
        # schema 错误
        _write_draft(ws, [{"id": "x", "title": "t", "form": "梦游",
                           "expr": "e", "rationale": "r",
                           "disposition": "clear"}])
        ok("schema 错误 rc=1",
           P6.run_p6(ws, finalize_flag=True, cfg=_AGENT) == 1)
        # agent 模式直通
        _write_draft(ws)
        ok("agent 续跑 rc=0",
           P6.run_p6(ws, finalize_flag=True, cfg=_AGENT) == 0)
        doc = json.loads(P6.l4_criteria_path(ws).read_text(encoding="utf-8"))
        ok("finalized", doc["status"] == "finalized" and len(doc["criteria"]) == 2)
        # human 模式停车 → answers 放行
        doc["status"] = "draft"
        P6.l4_criteria_path(ws).write_text(json.dumps(doc), encoding="utf-8")
        (ws / "human_questions.md").unlink(missing_ok=True)
        rc = P6.run_p6(ws, finalize_flag=True, cfg=_HUMAN)
        ok("human 停车 rc=3", rc == 3)
        ok("REVIEW.md 落盘",
           (ws / "P6" / "reports" / "l4_criteria_REVIEW.md").exists())
        q = (ws / "human_questions.md").read_text(encoding="utf-8")
        led = json.loads((ws / "gates.json").read_text(encoding="utf-8"))
        gate = [g for g in led["gates"] if g["id"] == "p6.l4.finalize"]
        ok("审批关口已登记（账本）",
           len(gate) == 1 and gate[0]["status"] == "open"
           and gate[0]["lane"] == "checkpoint")
        ok("渲染含表单", "@p6.l4.finalize" in q and "verdict" in q)
        doc = json.loads(P6.l4_criteria_path(ws).read_text(encoding="utf-8"))
        ok("停车保持 draft", doc["status"] == "draft")
        # 未放行重跑仍停
        ok("未放行重跑 rc=3",
           P6.run_p6(ws, finalize_flag=True, cfg=_HUMAN) == 3)
        (ws / "answers.md").write_text(
            "# answers\n\nl4_criteria_finalization: approve\n",
            encoding="utf-8")
        ok("放行后 rc=0",
           P6.run_p6(ws, finalize_flag=True, cfg=_HUMAN) == 0)
        doc = json.loads(P6.l4_criteria_path(ws).read_text(encoding="utf-8"))
        ok("finalized（human 放行）", doc["status"] == "finalized")
        shutil.rmtree(ws.parent)


# ---------- L. 执行模式 ----------

class TestExecute(unittest.TestCase):
    def test_execute_green(self):
        print("L1. 执行模式全绿（哨兵清偿 + 规范化 + park）")
        ws, _tos = _mk_p6_ws()
        _write_draft(ws)
        self.assertEqual(P6.run_p6(ws, finalize_flag=True, cfg=_AGENT), 0)
        saved = _patch_exec(_BOOT_GOOD)
        try:
            rc = P6.run_p6(ws, execute_flag=True, l4=True)
        finally:
            _restore_exec(saved)
        ok("全绿 rc=0", rc == 0)
        h = json.loads((ws / "P6" / "reports" / "health.json")
                       .read_text(encoding="utf-8"))
        v = h["verdict"]
        ok("判定 all_green_except_parked", v["all_green_except_parked"])
        ok("哨兵清偿 modA.sentinel-log",
           "modA.sentinel-log" in v["deferred_cleared"])
        ok("regs-dump 泊车", v["parked"] == ["modA.regs-dump"])
        ok("L4 判据 PASS", any(r["id"] == "modA.arp-e2e" and r["ok"]
                               for r in h["results"]))
        ok("设备参数 = SLIRP", h["device_args"].startswith("-netdev user"))
        d = json.loads((ws / "deferred.json").read_text(encoding="utf-8"))
        sent = next(e for e in d["entries"]
                    if e["id"] == "modA.sentinel-log")
        ok("哨兵规范化 __P6__", sent["deferred_by"] == ["__P6__"])
        ok("regs-dump 保持 open（泊车非清偿）",
           next(e for e in d["entries"] if e["id"] == "modA.regs-dump")
           ["status"] == "open")
        shutil.rmtree(ws.parent)

    def test_execute_pending_semantics(self):
        print("L2. 无 --l4：e2e deferred 记 pending 不阻塞")
        ws, _tos = _mk_p6_ws()
        _write_draft(ws)
        saved = _patch_exec(_BOOT_GOOD)
        try:
            rc = P6.run_p6(ws, execute_flag=True, l4=False)
        finally:
            _restore_exec(saved)
        ok("无 --l4 仍 rc=0（pending 不阻塞）", rc == 0)
        h = json.loads((ws / "P6" / "reports" / "health.json")
                       .read_text(encoding="utf-8"))
        ok("regs-dump 在 pending", h["verdict"]["deferred_pending_l4"]
           == ["modA.regs-dump"])
        ok("L4 判据 DEFER", any(r["id"] == "modA.arp-e2e"
                                and r["ok"] is None
                                for r in h["results"]))
        shutil.rmtree(ws.parent)

    def test_execute_hard_fail(self):
        print("L3. 硬失败路径")
        ws, _tos = _mk_p6_ws()
        _write_draft(ws)
        self.assertEqual(P6.run_p6(ws, finalize_flag=True, cfg=_AGENT), 0)
        saved = _patch_exec("Successfully booted\n（无判据日志）\n")
        try:
            rc = P6.run_p6(ws, execute_flag=True, l4=True)
        finally:
            _restore_exec(saved)
        ok("rc=3（PORTER_NO_AGENT 下求解降级 → p6.unsolved 关口）",
           rc == 3)
        h = json.loads((ws / "P6" / "reports" / "health.json")
                       .read_text(encoding="utf-8"))
        v = h["verdict"]
        ok("hello-log FAIL", "modA.hello-log" in v["failing"])
        ok("哨兵未清偿计入", "modA.sentinel-log" in v["deferred_uncleared"])
        ok("非全绿", not v["all_green_except_parked"])
        ok("solve 零轮 + 关口在场", h.get("solve") == [] and any(
            g["id"] == "p6.unsolved" for g in json.loads(
                (ws / "gates.json").read_text(encoding="utf-8"))["gates"]))
        shutil.rmtree(ws.parent)

    def test_execute_requires_finalized_l4(self):
        print("L4. --l4 前置 finalized")
        ws, _tos = _mk_p6_ws()
        _write_draft(ws)          # draft（未定稿）
        saved = _patch_exec(_BOOT_GOOD)
        try:
            rc = P6.run_p6(ws, execute_flag=True, l4=True)
        finally:
            _restore_exec(saved)
        ok("未定稿 rc=2", rc == 2)
        shutil.rmtree(ws.parent)


if __name__ == "__main__":
    unittest.main(verbosity=2)
