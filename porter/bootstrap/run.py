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
from .. import log as _log


def _acceptance(ws: Path, target_os: Path) -> bool:
    """build + boot + 组件日志三重验收。"""
    runner = json.loads((ws / "runner.json").read_text(encoding="utf-8"))
    p2 = ws / "P2"
    (p2 / "logs").mkdir(parents=True, exist_ok=True)

    results = [probe_mod.probe_build(p2, target_os, runner,
                                     label="P2_build")]
    if not results[-1]["ok"]:
        _log.console_line("[porter] P2: 验收 FAIL（build）")
        return False
    results.append(probe_mod.probe_boot_with_device(
        p2, target_os, runner,
        json.loads((ws / "project.json").read_text(encoding="utf-8"))
        .get("category") or [], label="P2_boot"))
    boot_ok = results[-1]["ok"]

    # 组件日志特征（boot 日志 = runner.boot.log_file，相对目标树根）
    patterns_hit = {}
    probe_fails = 0
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
        # P2c 预生成探针：boot 日志不得出现任何 FAIL 行（有 active 探针时）
        import re as _re
        probe_fails = len(_re.findall(r"PROBE_\S+ FAIL", boot_log))
        if probe_fails:
            boot_ok = False
    report = {
        "phase": "P2",
        "time": datetime.now().isoformat(),
        "results": results,
        "skeleton_log_patterns": patterns_hit,
        "probe_fail_lines": probe_fails,
        "pass": all(r["ok"] for r in results) and boot_ok,
    }
    (p2 / "reports" / "acceptance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in results:
        _log.console_line(f"[porter] P2: {r['item']:<14} {'PASS' if r['ok'] else 'FAIL'}  "
              f"{r['detail']}")
    for pat, n in patterns_hit.items():
        _log.console_line(f"[porter] P2: 日志特征 `{pat}` ×{n}")
    _log.console_line(f"[porter] P2: 验收 {'PASS' if report['pass'] else 'FAIL'}")
    return report["pass"]


def run_p2(ws: Path, driver_root: Path, target_os: Path,
           device_ids: list[str] | None = None) -> int:
    """返回 0=成功；1=失败；2=前置缺失；3=需人工。幂等：各步产物跳过。"""
    try:                                # 观测扩全（H12）：P2 相位埋桩
        from ..loop import events as _ev
        _ev.bind(ws, "p2")
    except Exception:
        pass
    for need in (ws / "project.json", ws / "runner.json",
                 ws / "P1" / "modules" / "deps.json"):
        if not need.exists():
            _log.console_line(f"[porter] P2: 缺少 {need}（先跑 p0/p1）")
            return 2

    rc = mapping.run_map(ws, driver_root, target_os)
    if rc == 2:
        return 2
    if rc == 1:
        _log.console_line("[porter] P2: ⚠ 映射存在失败批（详见 mapping_report.md）——"
              "骨架继续，失败批可断点重跑（幂等）")

    rc = skeleton.run_skeleton(ws, target_os, device_ids)
    if rc != 0:
        return rc

    # 2c 探针预生成（贵且可复用的验证前置；失败不阻塞验收——缺口可
    # p2-probes 幂等补跑）
    from . import pregen
    rc = pregen.run_pregen(ws, target_os)
    if rc == 2:
        return 2
    if rc != 0:
        _log.console_line("[porter] P2: ⚠ 探针预生成存在失败（详见 "
              "P2/reports/pregen_report.md）——可 p2-probes 断点补跑")

    if not _acceptance(ws, target_os):
        return 1

    # vcs：P2 阶段末——目标树骨架+wiring commit、工作区 commit（best-effort）
    try:
        from ..common import vcs as _vcs
        paths: list[str] = list(_vcs.TARGET_WIRING_FILES)
        try:
            _m = json.loads((ws / "P2" / "reports" / "skeleton_manifest.json")
                            .read_text(encoding="utf-8"))
            paths = list(_m.get("created") or []) + paths
        except (OSError, json.JSONDecodeError):
            pass
        _vcs.commit_target(ws, "P2: skeleton + wiring", paths=paths,
                           phase="P2")
        _vcs.commit_workspace(ws, "P2: done", phase="P2")
    except Exception:
        pass

    # CP2 映射审（默认关：e2e 实证无它也跑通，下游机器验证兜底；
    # checkpoints.CP2_enabled=true 开启——高保障迁移的映射抽审 + 债批审）
    from ..loop import gates as _gates
    if _gates.checkpoint_enabled("CP2"):
        return _gates.checkpoint_run(ws, "CP2", register=[{
            "id": "cp2.mapping_review", "kind": "approval",
            "gate_type": "decision", "phase": "P2", "checkpoint": "CP2",
            "question": ("P2 引导映射抽审：mapping.json 的 verdict/证据/"
                         "redesigns 抽样核对（CP2 显式开启时）。"),
            "context_files": ["P2/mapping.md", "P2/reports/probes.json"],
            "answer_form": [{"field": "verdict", "type": "enum",
                             "options": ["approve", "reject"],
                             "required": True}]}])
    return 0
