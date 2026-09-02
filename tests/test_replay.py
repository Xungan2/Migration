"""六案例回测（错误处理模块·求解循环的回归基线）。

夹具 = tests/fixtures/replay/*.json（素材取自 defects.json history /
runner.json unit_test notes / §14/§16——历史案例先于本体系，按可验证
部分回放并在夹具 degraded 字段注明）。每案例流程镜像挂载点行为：

事件预写 → 失败即快照 → 求解循环（canned agent verdict，mock）→
动作执行（按夹具）→ 终态断言（solved/parked/escalated/early-exit）→
未解决者断言升级报告六字段与夹具报告子串。

案例 → 新契约映射（旧 triage 语义 → solve 动作词表）：
  image-lock-25min   infra/SIG-01 重跑      → rerun + 复验自愈 → solved
  ktest-silent-3h    infra/SIG-02 定向修复  → fix-runner（console 参数）→ solved
  update-itr         criteria 需改目标树    → escalate（超边界转人工）+ 报告
  rx-path            migration/SIG-05 复合  → fix-code ×2 同签名 → early-exit + 报告
  reset-hw-stale     criteria/SIG-04 假缺陷 → fix-criteria(close_stale) 闭账
  intx-delivery      platform/SIG-06 泊车   → park + 登记 → parked
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def setUpModule():
    global _SD_OLD
    os.environ.pop("PORTER_NO_AGENT", None)   # canned agent 走 mock 路径
    _SD_OLD = os.environ.get("PORTER_SELF_DIAGNOSIS")
    os.environ["PORTER_SELF_DIAGNOSIS"] = "1"


def tearDownModule():
    if _SD_OLD is None:
        os.environ.pop("PORTER_SELF_DIAGNOSIS", None)
    else:
        os.environ["PORTER_SELF_DIAGNOSIS"] = _SD_OLD


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "replay"
import porter.common.agent as AG
from porter.bootstrap import kb as KB
from porter.loop import errorloop as EL
from porter.loop import events as EV


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def _solve_verdict(case: str, fx: dict) -> dict:
    """案例 → canned 求解 verdict（旧 triage 语义的动作词表转译）。"""
    if case == "image-lock-25min":
        return {"status": "done", "circuit": "infra", "action": "rerun",
                "evidence": [{"file": "<build-out>", "line": 0,
                              "quote": "resource temporarily "
                                       "unavailable"}],
                "summary": "docker 镜像锁（SIG-01）——幂等重跑即愈",
                "confidence": 0.9,
                "kb_consulted": ["docker-resource-lock.md"]}
    if case == "ktest-silent-3h":
        ut = fx["runner"]["unit_test"]
        return {"status": "done", "circuit": "infra",
                "action": "fix-runner",
                "fix": {"runner_patch": {
                    "unit_test": {"cmd": ut["cmd"]
                                  + ' --kcmd-args="console=ttyS0"'
                                  + ' --kcmd-args="earlycon"'}}},
                "evidence": [{"file": "knowledge/asterinas/failures/"
                                      "ktest-silent-console-args.md",
                              "line": 0,
                              "quote": "rc==0 但特征缺失——console 缓存"
                                       "参数被清空"}],
                "summary": "ktest 静默（SIG-02）——显式补 console 参数",
                "confidence": 0.85,
                "kb_consulted": ["silent-success-contradiction.md"]}
    if case == "update-itr":
        return {"status": "blocked", "circuit": "criteria",
                "action": "escalate",
                "evidence": [{"file": "drivers/net/ethernet/intel/"
                                      "e1000/e1000_main.c", "line": 0,
                              "quote": "e1000_update_itr 逐分支对照：函数"
                                       "与 C 一致，测试期望只推了一层"
                                       "分支"}],
                "summary": "测试期望错（update_itr 模式）——修正需改"
                           "目标树测试代码，超出边界，升级人工",
                "signature_candidates": ["SIG-03-test-expectation"],
                "confidence": 0.85}
    if case == "rx-path":
        return {"status": "done", "circuit": "migration",
                "action": "fix-code",
                "evidence": [{"file": "os_rx_irq.rs", "line": 0,
                              "quote": "rx_bytes=0 而 tx_bytes=64——复合"
                                       "型（LBM 不可用 + 接线）"}],
                "summary": "RX 复合缺陷（SIG-05）——分解独立链逐条清偿",
                "confidence": 0.75}
    if case == "reset-hw-stale":
        return {"status": "done", "circuit": "criteria",
                "action": "fix-criteria",
                "fix": {"target": "close_stale"},
                "evidence": [{"file": "kernel/core/comps/e1000/src/"
                                      "os_probe.rs", "line": 765,
                              "quote": "hw.reset_hw()（probe 直调）"}],
                "summary": "计划/文档过期型假缺陷（SIG-04）——对照代码"
                           "实测核计划",
                "confidence": 0.9}
    if case == "intx-delivery":
        return {"status": "done", "circuit": "platform", "action": "park",
                "fix": {"gap": "INTX-DELIVERY"},
                "evidence": [{"file": "<l4-log>", "line": 0,
                              "quote": "icr=0x14 但 irq_count=0（设备侧"
                                       "已证，消费侧恒零）"}],
                "summary": "平台缺口（SIG-06）——OSTD ioapic 电平触发"
                           "缺失，泊车 + 上游登记",
                "confidence": 0.85}
    raise AssertionError(f"未知案例 {case}")


def _verify_script(case: str):
    """案例 → 复验脚本（返回 verify callable）。"""
    if case == "image-lock-25min":
        return lambda: (True, None)          # 重跑自愈
    if case == "ktest-silent-3h":
        return lambda: (True, None)          # 参数补全后自愈
    if case == "rx-path":
        seq = [(False, {"detail": "rx_bytes=0 tx_bytes=64（仍是）",
                        "boot_log": "e1000 stats: rx=0 tx=64"}),
               (False, {"detail": "rx_bytes=0 tx_bytes=64（仍是）",
                        "boot_log": "e1000 stats: rx=0 tx=64"})]
        return lambda: seq.pop(0)
    if case == "reset-hw-stale":
        return lambda: (True, None)          # 假缺陷闭账即解决
    return lambda: (True, None)              # escalate/park 不进复验


def _replay(fx: dict) -> None:
    case = fx["case"]
    tmp = Path(tempfile.mkdtemp(prefix=f"porter_rp2_{case}_"))
    ws = tmp / "ws"
    ws.mkdir()
    if fx.get("runner"):
        (ws / "runner.json").write_text(
            json.dumps(fx["runner"], ensure_ascii=False), encoding="utf-8")
    for e in fx.get("events") or []:
        EV.append_event(ws=ws, **e)

    ev = dict(fx["evidence"])
    if ev.get("defect"):
        (ws / "defects.json").write_text(json.dumps(
            {"defects": [ev["defect"]]}, ensure_ascii=False),
            encoding="utf-8")
    ev["_workdir"] = tmp

    # 失败即快照（求解之前——镜像挂载点纪律）
    snap = EV.take_failure_snapshot(ws, ev["source"], ev["subject"],
                                    f"replay: {case}",
                                    runner=fx.get("runner"))
    ok(f"{case}:快照在场", snap is not None)

    verdict = _solve_verdict(case, fx)
    with mock.patch.object(
            EL.agent, "run_agent",
            return_value=(0, "```json\n" + json.dumps(
                verdict, ensure_ascii=False) + "\n```")):
        outcome = EL.run_solve_loop(ws, ev, _verify_script(case))

    exp = fx["expect"]
    ok(f"{case}:归责一致 {exp['circuit']}",
       outcome["rounds"] and outcome["rounds"][0].get("circuit")
       == exp["circuit"],
       f"实际 rounds={outcome['rounds']}")

    # 终态映射（新契约）
    terminal = {"image-lock-25min": "solved",
                "ktest-silent-3h": "solved",
                "update-itr": "escalated",
                "rx-path": "early-exit",
                "reset-hw-stale": "solved",
                "intx-delivery": "parked"}[case]
    ok(f"{case}:终态 {terminal}", outcome["status"] == terminal,
       f"实际 {outcome['status']}")

    # 处置落盘断言（apply_assert：solved/applied 的正本效果）
    blob = json.dumps(outcome, ensure_ascii=False)
    for side in ("defects.json", "platform_patches.json", "runner.json"):
        p = ws / side
        if p.exists():
            blob += p.read_text(encoding="utf-8")
    for sub in fx.get("apply_assert") or []:
        ok(f"{case}:处置含[{sub[:24]}]", sub in blob,
           f"applied={[a for r in outcome['rounds'] for a in r.get('applied') or []]}")

    # 未解决终态 → 报告六字段 + 夹具报告子串
    if terminal in ("escalated", "early-exit"):
        rep = outcome.get("report")
        ok(f"{case}:报告在场", rep is not None
           and outcome.get("report_path"))
        ok(f"{case}:报告六字段",
           all(k in rep and rep[k] is not None for k in
               ("symptom", "env_snapshot", "excluded", "experiments",
                "remaining", "reproduce", "evidence_files")))
        ok(f"{case}:evidence 指快照",
           rep["evidence_files"]
           and all(x.startswith("failure-snapshot-")
                   for x in rep["evidence_files"]))
        rblob = json.dumps(rep, ensure_ascii=False)
        for sub in fx.get("report_assert") or []:
            ok(f"{case}:报告含[{sub[:24]}]", sub in rblob)


class ReplayTest(unittest.TestCase):
    def test_r_replay_all(self):
        old_root = KB.KB_ROOT, KB.BASE_DIR, KB.TEMP_DIR
        KB.KB_ROOT = Path(tempfile.mkdtemp(prefix="rp_kb_")) / "knowledge"
        KB.BASE_DIR = KB.KB_ROOT / "base"
        KB.TEMP_DIR = KB.KB_ROOT / "temp"
        fake_root = KB.KB_ROOT.parent
        (fake_root / "knowledge").mkdir(parents=True, exist_ok=True)
        (fake_root / "knowledge" / "failures.md").write_text(
            "# failures\n\n## 候选区（agent 自动附上来的，待人工晋升）\n",
            encoding="utf-8")
        try:
            files = sorted(FIXTURES.glob("*.json"))
            ok("夹具六案例齐", len(files) == 6, f"实际 {len(files)}")
            with mock.patch.object(AG, "TOOL_ROOT", fake_root):
                for f in files:
                    fx = json.loads(f.read_text(encoding="utf-8"))
                    with self.subTest(case=fx["case"]):
                        _replay(fx)
            print("  ✅ 回放矩阵：六案例全过（降级项见各夹具 degraded 字段）")
        finally:
            KB.KB_ROOT, KB.BASE_DIR, KB.TEMP_DIR = old_root


if __name__ == "__main__":
    unittest.main()
