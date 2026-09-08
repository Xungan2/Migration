"""P2 复用 P0 骨架并预生成探针；P2a 默认暂停，手动 p2-map 保留。"""

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

    from ..divide import pruning
    if pruning.require_ready(ws, driver_root):
        return 2

    from ..handoff import current_execution as _handoff_current
    from ..handoff.integration import execute_phase as _handoff_phase

    def _phase(task_id, deps, fn, artifacts):
        if _handoff_current() is None:
            return fn()
        return _handoff_phase(
            ws, task_id, deps, fn, materials=(ws / "project.json",),
            artifacts=artifacts, description="P2 composite child task")

    predecessor = "p1.prune" if (ws / "P1/scope.json").exists() else "p1.resolve"
    manifest = scaffold.load_manifest(ws)
    if not manifest or manifest.get("status") != "verified" or not manifest.get("source_sha256"):
        _log.console_line("[porter] P2: 缺少 P0 已验证骨架（先跑 p0）")
        return 2
    # P2a 暂停；保留已有映射，空表让 P3 按实际模块使用面增量补齐。
    p2 = ws / "P2"
    p2.mkdir(exist_ok=True)
    if not (p2 / "mapping.json").exists():
        mapping._save(mapping._load_mapping(p2), p2)
    _log.console_line("[porter] P2: 默认跳过 P2a；复用 P0 骨架")

    # 2c 探针预生成（贵且可复用的验证前置；失败不阻塞——缺口可
    # p2-probes 幂等补跑）
    from . import pregen
    rc = _phase("p2.probes", ("p0", predecessor),
                lambda: pregen.run_pregen(ws, target_os),
                (ws / "P2" / "reports" / "pregen_report.md",))
    if rc != 0:
        return rc

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
