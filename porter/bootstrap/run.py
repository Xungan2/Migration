"""run.py — P2 入口编排：2a 引导映射 → 2b 骨架 → 验收（runner 双信号）。

验收（复用 P0 probe 机器，判据双信号）：
  1. build：runner build.cmd，退出码 + success_pattern
  2. boot（设备注入）：退出码 + "Successfully booted" + 无 panic
  3. 组件日志：boot 日志（qemu.log）中出现骨架日志特征
     （manifest.acceptance_log_patterns，每条 ≥1 次）
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..env import probe as probe_mod
from . import mapping, skeleton


def _acceptance(ws: Path, target_os: Path) -> bool:
    """build + boot + 组件日志三重验收。"""
    runner = json.loads((ws / "runner.json").read_text(encoding="utf-8"))
    p2 = ws / "P2"
    (p2 / "logs").mkdir(parents=True, exist_ok=True)

    results = [probe_mod.probe_build(p2, target_os, runner,
                                     label="P2_build")]
    if not results[-1]["ok"]:
        print("[porter] P2: 验收 FAIL（build）")
        return False
    results.append(probe_mod.probe_boot_with_device(
        p2, target_os, runner,
        json.loads((ws / "project.json").read_text(encoding="utf-8"))
        .get("category") or [], label="P2_boot"))
    boot_ok = results[-1]["ok"]

    # 组件日志特征（boot 日志 = runner.boot.log_file，相对目标树根）
    patterns_hit = {}
    if boot_ok:
        bo = runner["boot"]
        log_file = Path(bo["log_file"]) if bo.get("log_file") else None
        log_path = (log_file if log_file and log_file.is_absolute()
                    else target_os / log_file) if log_file else None
        boot_log = ""
        if log_path and log_path.exists():
            boot_log = log_path.read_text(encoding="utf-8",
                                          errors="replace")
        manifest_path = p2 / "reports" / "skeleton_manifest.json"
        patterns = (json.loads(manifest_path.read_text(encoding="utf-8"))
                    .get("acceptance_log_patterns", [])) \
            if manifest_path.exists() else []
        for pat in patterns:
            n = boot_log.count(pat)
            patterns_hit[pat] = n
            if n == 0:
                boot_ok = False
    report = {
        "phase": "P2",
        "time": datetime.now().isoformat(),
        "results": results,
        "skeleton_log_patterns": patterns_hit,
        "pass": all(r["ok"] for r in results) and boot_ok,
    }
    (p2 / "reports" / "acceptance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in results:
        print(f"[porter] P2: {r['item']:<14} {'PASS' if r['ok'] else 'FAIL'}  "
              f"{r['detail']}")
    for pat, n in patterns_hit.items():
        print(f"[porter] P2: 日志特征 `{pat}` ×{n}")
    print(f"[porter] P2: 验收 {'PASS' if report['pass'] else 'FAIL'}")
    return report["pass"]


def run_p2(ws: Path, driver_root: Path, target_os: Path,
           device_ids: list[str] | None = None) -> int:
    """返回 0=成功；1=失败；2=前置缺失；3=需人工。幂等：各步产物跳过。"""
    for need in (ws / "project.json", ws / "runner.json",
                 ws / "P1" / "modules" / "deps.json"):
        if not need.exists():
            print(f"[porter] P2: 缺少 {need}（先跑 p0/p1）")
            return 2

    rc = mapping.run_map(ws, driver_root, target_os)
    if rc == 2:
        return 2
    if rc == 1:
        print("[porter] P2: ⚠ 映射存在失败批（详见 mapping_report.md）——"
              "骨架继续，失败批可断点重跑（幂等）")

    rc = skeleton.run_skeleton(ws, target_os, device_ids)
    if rc != 0:
        return rc

    return 0 if _acceptance(ws, target_os) else 1
