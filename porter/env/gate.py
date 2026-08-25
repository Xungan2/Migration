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


def run_gate(ws: Path) -> bool:
    checks: list[tuple[str, bool, str]] = []

    # 1. project.json 完整性
    proj_path = ws / "project.json"
    if not proj_path.exists():
        print("[porter] gate: project.json 缺失")
        return False
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    checks.append(("project.json 完整",
                   bool(proj.get("linux_driver") and proj.get("target_os")),
                   "身份字段"))
    cat = proj.get("category")
    checks.append(("类别已定（含人工/回落）", cat is not None, f"category={cat}"))

    # 2. runner.json
    runner_path = ws / "runner.json"
    if runner_path.exists():
        runner = json.loads(runner_path.read_text(encoding="utf-8"))
        defects = validate_runner(runner)
        checks.append(("runner.json 机器校验", not defects,
                       "通过" if not defects else "; ".join(defects)))
    else:
        checks.append(("runner.json 存在", False, "缺失"))

    # 3. T3 三项显式结果（硬门禁）
    dev_path = ws / "reports" / "T3_development.json"
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
        checks.append(("T3 探测执行", False, "T3_development.json 缺失"))

    passed = all(ok for _, ok, _ in checks)

    # 报告
    lines = ["# P0 报告", "",
             f"**结论：{'通过 ✅' if passed else '未通过 ❌'}**", "",
             "| 检查项 | 结果 | 说明 |", "|---|---|---|"]
    for name, ok, detail in checks:
        lines.append(f"| {name} | {'✅' if ok else '❌'} | {detail} |")
    (ws / "reports").mkdir(exist_ok=True)
    (ws / "reports" / "p0_report.md").write_text(
        "\n".join(lines), encoding="utf-8")
    print(f"[porter] T5: 门禁 {'通过' if passed else '未通过'}"
          f"（详见 {ws/'reports'/'p0_report.md'}）")
    return passed
