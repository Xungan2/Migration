"""run.py — P2 入口编排：2a 引导映射 → 2b 框架引导（发现式骨架）→ 2c
探针预生成 → vcs commit + CP2 映射审。

P2b 自带三信号验收（build / boot_with_device + 验收特征 / 单测 smoke，
scaffold.py 内化——独立 _acceptance 步骤已退役）；P2c 预生成自带
build+boot 判定生命周期。本模块只做编排与阶段末收尾。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import mapping, scaffold
from .. import log as _log


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

    rc = scaffold.run_scaffold(ws, target_os, device_ids)
    if rc != 0:
        return rc

    # 2c 探针预生成（贵且可复用的验证前置；失败不阻塞——缺口可
    # p2-probes 幂等补跑）
    from . import pregen
    rc = pregen.run_pregen(ws, target_os)
    if rc == 2:
        return 2
    if rc != 0:
        _log.console_line("[porter] P2: ⚠ 探针预生成存在失败（详见 "
              "P2/reports/pregen_report.md）——可 p2-probes 断点补跑")

    # vcs：P2 阶段末——commit 面来自 scaffold manifest（骨架 + 接线 +
    # P2c 探针同步触碰面），工作区 commit（best-effort）
    try:
        from ..common import vcs as _vcs
        paths: list[str] = []
        m = scaffold.load_manifest(ws)
        if m:
            paths = list(m.get("commit_paths") or [])
            dorm = str(m.get("dormitory") or "")
            if dorm and dorm not in paths:
                paths.append(dorm)
        _vcs.commit_target(ws, "P2: scaffold + probes + wiring", paths=paths,
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
