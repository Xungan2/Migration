"""六案例回测（§15.8 实施完成判据）：分诊命中 + 升级报告字段完整。

夹具 = tests/fixtures/replay/*.json（素材取自 defects.json history /
runner.json unit_test notes / §14/§16；旧日志缺口按可验证部分降级断言
并在夹具 degraded 字段注明）。每案例流程镜像挂载点行为：
事件预写 → 失败即快照 → triage（规则或 canned agent）→ apply（按夹具）
→ 升级报告 → 断言回路命中 + 六字段 + 夹具报告子串。

ktest 静默案例额外断言（§15.8）：events+快照在场时，报告应包含当时
排查走弯路的三项关键证据——ut 命令原文 / rc=0 与特征缺失的矛盾 /
console 参数状态。
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
    os.environ.pop("PORTER_NO_AGENT", None)   # canned agent 走 mock 路径
    os.environ.setdefault("PORTER_SELF_DIAGNOSIS", "1")  # §15 重放：强制开


FIXTURES = Path(__file__).resolve().parent / "fixtures" / "replay"
import porter.common.agent as AG
from porter.loop import diagnose as DG
from porter.loop import events as EV
from porter.loop import triage as TR


def ok(name, cond, extra=""):
    if not cond:
        raise AssertionError(f"{name}  {extra}")
    print(f"  ✅ {name}")


def _replay(fx: dict) -> None:
    case = fx["case"]
    tmp = Path(tempfile.mkdtemp(prefix=f"porter_rp_{case}_"))
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
    ev["events_tail"] = EV.read_events(ws)
    ev["runner"] = fx.get("runner")
    ev["_workdir"] = tmp

    # 失败即快照（重跑/分诊之前——镜像挂载点纪律）
    snap = EV.take_failure_snapshot(ws, ev["source"], ev["subject"],
                                    f"replay: {case}",
                                    runner=fx.get("runner"))
    ok(f"{case}:快照在场", snap is not None)

    if fx.get("agent_verdict"):
        canned = fx["agent_verdict"]
        with mock.patch.object(
                TR.agent, "run_agent",
                return_value=(0, "```json\n" + json.dumps(canned)
                              + "\n```")):
            v = TR.run_triage(ws, ev, use_agent=True)
    else:
        v = TR.run_triage(ws, ev, use_agent=True)   # 规则路径

    exp = fx["expect"]
    ok(f"{case}:回路命中 {exp['circuit']}", v["circuit"] == exp["circuit"],
       f"实际 {v['circuit']}/{v.get('rule_id')}——{v.get('notes')}")
    if exp.get("rule_id"):
        ok(f"{case}:规则命中 {exp['rule_id']}",
           v.get("rule_id") == exp["rule_id"])
    if exp.get("suggested_fix"):
        ok(f"{case}:定向修复建议",
           v.get("suggested_fix") == exp["suggested_fix"])

    if fx.get("apply"):
        app = TR.apply_verdict(ws, ev, v, gate_ok=True)
        blob = json.dumps(app, ensure_ascii=False)
        for side in ("defects.json", "platform_patches.json",
                     "runner.json"):
            p = ws / side
            if p.exists():
                blob += p.read_text(encoding="utf-8")
        for sub in fx.get("apply_assert") or []:
            ok(f"{case}:处置含[{sub[:24]}]", sub in blob,
               f"applied={app['applied']}")

    # 隔离 failures.md 活文档（TOOL_ROOT 重定向到临时仓根——测试不污染真库）
    fake_root = tmp / "toolroot"
    (fake_root / "knowledge").mkdir(parents=True)
    (fake_root / "knowledge" / "failures.md").write_text(
        "# failures\n\n## 候选区（agent 自动附上来的，待人工晋升）\n",
        encoding="utf-8")
    with mock.patch.object(AG, "TOOL_ROOT", fake_root):
        report, _stop = DG.generate_escalation_report(
            ws, ev["source"], ev["subject"],
            symptom=ev.get("detail") or "", triage_verdicts=[v])
    ok(f"{case}:报告六字段",
       all(k in report and report[k] is not None for k in
           ("symptom", "env_snapshot", "excluded", "experiments",
            "remaining", "reproduce", "evidence_files")))
    ok(f"{case}:evidence 指快照",
       report["evidence_files"]
       and all(x.startswith("failure-snapshot-")
               for x in report["evidence_files"]))
    rblob = json.dumps(report, ensure_ascii=False)
    for sub in fx.get("report_assert") or []:
        ok(f"{case}:报告含[{sub[:24]}]", sub in rblob)

    # ktest 静默专项：三项弯路证据必须在场（§15.8）
    if fx.get("ktest_silent_extras"):
        ok(f"{case}:证据①ut 命令原文", "osdk test" in report["reproduce"])
        ok(f"{case}:证据②rc=0 矛盾",
           any(str(e.get("result", "")).startswith("rc=0")
               for e in report["experiments"]))
        ok(f"{case}:证据③console 参数状态",
           "console=ttyS0" in rblob)


class ReplayTest(unittest.TestCase):
    def test_r_replay_all(self):
        files = sorted(FIXTURES.glob("*.json"))
        ok("夹具六案例齐", len(files) == 6, f"实际 {len(files)}")
        for f in files:
            fx = json.loads(f.read_text(encoding="utf-8"))
            with self.subTest(case=fx["case"]):
                _replay(fx)
        print("  ✅ 回放矩阵：六案例全过（降级项见各夹具 degraded 字段）")


if __name__ == "__main__":
    unittest.main()
