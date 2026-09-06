"""pregen.py — P2c 探针预生成（2026-08-30 定案：贵且可复用的验证前置）。

动机（用户会话定案）：探针生成贵（每批 agent 调用 + 编译 + 启动）、产出
长寿命（装进骨架后每次启动重跑 = 回归哨网）、P2 末流程稳定（映射表与骨架
已定，不受后续模块轮次的流程波动连累）。因此在 P2 末一次性生成全部
"会被某模块真实使用的风险主张"的探针，P3(M) 退化为补新（通常接近空）。

流程：
  1. 预计算全部模块使用面（纯脚本幂等；顺带把 P3/<M>/reports/surface.json
     全部缓存——后续每轮 P3 直接复用）
  2. 目标集 = 全模块使用面并集 ∩ (risk∈{med,high} ∪ confidence=low)
     − 已探 claim（P2/各模块 P3/P4 fill 注册表跨表去重）
  3. 共享探针生命周期（probes.run_probe_lifecycle）：
     注册表 = P2/reports/probes.json（节标记 P2(pregen)），日志 = P2/logs/
  4. 残余 FAIL 降级 gap 后**不做四策略处置**——留给消费该符号的模块
     P3(M) 带使用位置上下文处理（2026-08-30 用户定案）
  5. 报告 P2/reports/pregen_report.md

入口：run_p2 自动在 2b 骨架后调用；存量工作区用 `p2-probes` 子命令补跑
（幂等：已探 claim 自动跳过，断点重跑只补缺口）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..loop import probes as probe_lib
from ..loop import surface as surface_mod
from .. import log as _log


def _targets(ws: Path, driver_root: Path, order: list[str],
             max_batches: int | None = None) -> tuple[list[dict], dict]:
    """(待生成条目, 使用位置聚合)。条目按符号名排序保证断点重跑稳定。"""
    for m in order:
        _s, rc = surface_mod.extract_surface(ws, driver_root, m)
        if rc != 0:
            raise RuntimeError(f"使用面提取失败：{m}")

    mapping = json.loads((ws / "P2" / "mapping.json").read_text(
        encoding="utf-8"))
    entries = {e["linux_api"]: e for e in mapping.get("entries", [])}

    union: set[str] = set()
    locs: dict[str, list[str]] = {}
    for m in order:
        s = json.loads((ws / "P3" / m / "reports" / "surface.json")
                       .read_text(encoding="utf-8"))
        for verdict, syms in (s.get("mapped_by_verdict") or {}).items():
            if verdict in ("direct", "adapt"):
                union.update(syms)
        for sym, ll in (s.get("usage_locations") or {}).items():
            bucket = locs.setdefault(sym, [])
            for one in ll:
                if one not in bucket and len(bucket) < 5:
                    bucket.append(one)

    risky = sorted(s for s in union
                   if s in entries
                   and (entries[s].get("risk") in ("med", "high")
                        or entries[s].get("confidence") == "low"))
    claimed = probe_lib.known_claims(ws, order, None)
    todo = [entries[s] for s in risky if s not in claimed]
    if max_batches is not None:
        todo = todo[:max_batches * probe_lib.GEN_BATCH]
    return todo, locs


def run_pregen(ws: Path, target_os: Path,
               max_batches: int | None = None) -> int:
    """返回 0 成功 / 1 失败 / 2 前置缺失。幂等：已探 claim 跳过。"""
    for need in (ws / "project.json", ws / "runner.json",
                 ws / "P1" / "modules" / "deps.json",
                 ws / "P2" / "mapping.json"):
        if not need.exists():
            _log.console_line(f"[porter] P2c: 缺少 {need}（先跑 p0/p1/p2-map）")
            return 2
    proj = json.loads((ws / "project.json").read_text(encoding="utf-8"))
    driver_root = Path(proj["linux_driver"])
    if not driver_root.is_dir():
        _log.console_line(f"[porter] P2c: linux_driver 路径无效 {driver_root}")
        return 2
    deps = json.loads((ws / "P1" / "modules" / "deps.json").read_text(
        encoding="utf-8"))
    order = list(deps.get("order") or [])
    from ..common import scope as _scope
    driver = _scope.driver_name_of(proj)
    # 骨架须已落地（探针要住进宿舍；路径唯一真值源 = scaffold manifest）
    from . import scaffold as _scaffold
    dorm = _scaffold.dormitory_abs(ws, target_os)
    if dorm is None or not dorm.exists():
        _log.console_line("[porter] P2c: 无 scaffold_manifest 或宿舍未建"
                          "（先跑 p2-scaffold）")
        return 2
    (ws / "P2" / "logs").mkdir(parents=True, exist_ok=True)

    try:
        todo, locs = _targets(ws, driver_root, order, max_batches=max_batches)
    except (RuntimeError, json.JSONDecodeError, OSError) as e:
        _log.console_line(f"[porter] P2c: 目标计算失败——{e}")
        return 1
    known = probe_lib.known_claims(ws, order, None)
    print(f"[porter] P2c: 预生成目标 {len(todo)} 条"
          f"（已探 {len(known)} 条去重后；"
          + (f"限 {max_batches} 批）" if max_batches else "全量）"))

    registry_path = ws / "P2" / "reports" / "probes.json"
    t0 = len(probe_lib.load_registry(registry_path).get("probes", []))
    rc = probe_lib.run_probe_lifecycle(
        ws, target_os, proj, order, registry_path,
        label="P2PREG", todo_entries=todo,
        logs_dir=ws / "P2" / "logs", boot_ws=ws / "P2",
        usage_locs=locs, after_downgrade=None)
    _write_report(ws, todo, registry_path, t0)
    return rc


def _write_report(ws: Path, todo: list[dict],
                  registry_path: Path, pre_count: int) -> None:
    reg = probe_lib.load_registry(registry_path)
    probes = reg.get("probes", [])
    active = [p for p in probes if p.get("status") == "active"]
    downgraded = [p for p in probes if p.get("status") == "downgraded"]
    lines = [
        "# P2c 探针预生成报告", "",
        f"- 时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 本轮目标：{len(todo)} 条（≤{probe_lib.GEN_BATCH} 条/批）",
        f"- 注册表累计：{len(probes)} 个探针"
        f"（active {len(active)} / downgraded {len(downgraded)}；"
        f"本轮新增 {len(probes) - pre_count}）",
        f"- 验证轮次历史："
        f"{json.dumps(reg.get('history', []), ensure_ascii=False)}",
        f"- 降级 gap：{', '.join(p['claim'] for p in downgraded) or '无'}"
        "（四策略处置留给消费者模块的 P3——带使用位置上下文）",
        "",
        "## 断点续跑", "",
        "幂等：重跑 `p2-probes` 只补未生成/未判定的缺口（已探 claim 跨"
        "注册表去重）。",
    ]
    (ws / "P2" / "reports" / "pregen_report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    _log.console_line(f"[porter] P2c: 报告 → {ws / 'P2' / 'reports' / 'pregen_report.md'}")
