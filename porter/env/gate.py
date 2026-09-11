"""gate.py — T5 门禁检查（纯脚本，机器可检）。

P0 退出条件（全部显式结论，禁止空白项）：
1. project.json 完整（身份/类别已填——manual/回落均为合法显式值）
2. runner.json 存在且机器校验通过
3. T3 开发能力三项（build/boot/boot_with_device）均有显式结果且全 PASS
   （双信号判定；任一 FAIL = 门禁不通过）

产出 reports/p0_report.md（人读）+ exit code（0=过）。
"""

from __future__ import annotations

import json
from pathlib import Path

from .extract import validate_runner
from .. import log as _log


def run_gate(ws: Path) -> bool:
    checks: list[tuple[str, bool, str]] = []

    # 1. project.json 完整性
    proj_path = ws / "project.json"
    if not proj_path.exists():
        _log.console_line("[porter] gate: project.json 缺失")
        return False
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    checks.append(("project.json 完整",
                   bool(proj.get("linux_driver") and proj.get("target_os")),
                   "身份字段"))
    cat = proj.get("category")
    checks.append(("类别已定（含人工/回落）", cat is not None, f"category={cat}"))

    # 2. runner.json
    runner_path = ws / "runner.json"
    runner = None
    if runner_path.exists():
        runner = json.loads(runner_path.read_text(encoding="utf-8"))
        defects = validate_runner(runner)
        checks.append(("runner.json 机器校验", not defects,
                       "通过" if not defects else "; ".join(defects)))
    else:
        checks.append(("runner.json 存在", False, "缺失"))

    # 3. T3 三项显式结果（硬门禁）
    p0 = ws / "P0"
    dev_path = p0 / "reports" / "T3_development.json"
    if dev_path.exists():
        dev = json.loads(dev_path.read_text(encoding="utf-8"))
        items = {r["item"]: r for r in dev.get("results", [])}
        for name in ("build", "boot", "boot_with_device"):
            r = items.get(name)
            if r is None:
                checks.append((f"T3 {name} 有显式结果", False, "缺失"))
            else:
                checks.append((f"T3 {name} {'PASS' if r['ok'] else 'FAIL'}",
                               r["ok"], r["detail"]))
    else:
        checks.append(("T3 探测执行", False, "P0/reports/T3_development.json 缺失"))

    # 3.5 boot_with_device 驱动级判定配置：配置了 driver_success_pattern
    #     → 驱动结论已含在 T3 boot_with_device 行（MISS 即该项 FAIL）；
    #     未配置 → ⚠ 告警跳过（不拦——目标 OS 无该类别内置驱动时合法，
    #     如 Asterinas P0 尚无驱动；适配有内置驱动的新内核时强烈建议配置）
    inj = (runner or {}).get("inject_device") or {}
    if inj.get("driver_success_pattern"):
        checks.append(("boot_with_device 驱动级判定", True,
                       f"已配置 {inj['driver_success_pattern']!r}"
                       "（结论见 T3 boot_with_device 行）"))
    else:
        checks.append(("boot_with_device 驱动级判定", True,
                       "⚠ 未配置 driver_success_pattern——跳过驱动级判定"
                       "（目标 OS 无该类别内置驱动时合法；有内置驱动的目标"
                       "建议配置，P0 才能验证'驱动成功启动'而非仅内核启动）"))

    # 4. unit_test 烟测（第一道，2026-08-30 双道烟测定案）：
    #    真跑 smoke_cmd（agent 在目标树最小已有单测 crate 上验证过的形态）
    #    断言特征命中。缺 smoke_cmd = 告警跳过（非门禁失败）——存量/异构
    #    目标兼容；真跑失败 = 门禁失败（机制主张被证伪）。
    ut = (runner or {}).get("unit_test") or {}
    if not ut:
        checks.append(("unit_test 烟测", True,
                       "runner 无 unit_test 节（loop 补探回填时二道烟测兜底）"))
    elif ut.get("mechanism") == "none":
        checks.append(("unit_test 烟测", True,
                       "mechanism=none（目标 OS 无内核单测机制，L0 将转 deferred）"))
    elif not ut.get("smoke_cmd"):
        checks.append(("unit_test 烟测", True,
                       "⚠ 无 smoke_cmd——跳过（建议补：agent 探明时应在最小"
                       "已有单测 crate 上实跑验证）"))
    else:
        from ..loop.ut_verify import smoke_unit_test_config
        target_os = Path(proj["target_os"])
        ok, detail = smoke_unit_test_config(ws, target_os, runner, ut,
                                            label="P0_unit_test_smoke")
        checks.append((f"unit_test 烟测 {'PASS' if ok else 'FAIL'}",
                       ok, detail))

    passed = all(ok for _, ok, _ in checks)

    # 报告
    lines = ["# P0 报告", "",
             f"**结论：{'通过 ✅' if passed else '未通过 ❌'}**", "",
             "| 检查项 | 结果 | 说明 |", "|---|---|---|"]
    for name, ok, detail in checks:
        lines.append(f"| {name} | {'✅' if ok else '❌'} | {detail} |")
    (p0 / "reports").mkdir(parents=True, exist_ok=True)
    (p0 / "reports" / "p0_report.md").write_text(
        "\n".join(lines), encoding="utf-8")
    _log.console_line(f"[porter] T5: 门禁 {'通过' if passed else '未通过'}"
          f"（详见 {p0/'reports'/'p0_report.md'}）")
    return passed
