"""run.py — P1 divide 编排（索引预建 + 按文件分配 + 机械展开版）。

流程（plan: divide-refactor §2，重构起因：单次大调用撞思考上限零产出）：
  1. 前置：strategy.md 必须存在（divide 执行已审阅的策略）
  2. index.build_index 预建定义索引（纯脚本，秒级）
  3. 按文件循环（先 .c 后 .h）：每次 agent 调用 = SKILL + strategy 全文
     + 该文件索引切片 → whole_file 或 assignments JSON；
     校验失败带反馈重试 1 次（缺符号催补/解析失败），仍败则该文件报错退出
  4. index.expand 机械展开 → P1D_plan.json（schema 不变）+ P1D_audit.md
  5. fragments.extract_modules 物理抽取（零改动复用）
"""

from __future__ import annotations

import json
from pathlib import Path

from ..common import agent
from . import fragments as frag_mod
from . import index
from .. import log as _log

MAX_TRIES = 2          # 每文件：首发 + 催补重试 1 次
AGENT_TIMEOUT_SEC = 900


def _assign_one_file(skill: str, strategy: str, fname: str,
                     entries: list, p1: Path) -> tuple[dict | None, dict]:
    """单文件 agent 分配。返回 (决定, module_desc)；失败返回 (None, {})。"""
    total = entries[-1].end
    base = (f"{skill}\n\n---\n\n## 拆分策略（已人工审阅，按它执行）\n\n"
            f"{strategy}\n\n---\n\n## 任务数据\n\n"
            f"本次调用只处理一个源文件：`{fname}`。\n\n"
            f"{index.render_slice(fname, entries, total)}\n\n"
            f"请按 SKILL 输出该文件的分配结果（只输出一个 JSON 块）。")
    feedback = ""
    for attempt in range(1, MAX_TRIES + 1):
        rc, out = agent.run_agent(
            base + feedback, workdir=p1,
            log_stem=str(p1 / "logs" / f"P1D_F_{fname}_R{attempt}"),
            timeout_sec=AGENT_TIMEOUT_SEC)
        parsed = agent.extract_json(out) if rc == 0 else None
        err = index.validate_decision(entries, parsed)
        if err is None:
            desc = parsed.get("module_desc")
            return parsed, (desc if isinstance(desc, dict) else {})
        head = err.splitlines()[0]
        _log.console_line(f"[porter] P1D: {fname} 第 {attempt} 次输出不合规：{head}")
        feedback = (f"\n\n---\n\n## 上一次输出的问题（修正后重新输出"
                    f"完整 JSON）\n\n{err}")
    return None, {}


def run_divide(ws: Path, driver_root: Path) -> int:
    """返回 0=成功；2=前置缺失；1=失败。"""
    p1 = ws / "P1"
    plan_path = p1 / "reports" / "P1D_plan.json"
    if plan_path.exists():
        _log.console_line(f"[porter] P1D: 复用 {plan_path}（如需重做请删除该文件）")
        return 0
    strategy_path = p1 / "strategy.md"
    if not strategy_path.exists():
        _log.console_line(f"[porter] P1D: 缺少 {strategy_path}（divide 执行已审阅的策略——"
              f"先跑 p1-strategy 并人工放行）")
        return 2
    strategy = strategy_path.read_text(encoding="utf-8")

    file_index = index.build_index(driver_root)
    files = index.call_order(file_index)
    if not files:
        _log.console_line(f"[porter] P1D: {driver_root} 下未发现含定义的 *.c/*.h——失败")
        return 2
    (p1 / "logs").mkdir(parents=True, exist_ok=True)
    (p1 / "reports").mkdir(parents=True, exist_ok=True)
    _log.console_line(f"[porter] P1D: 索引预建完成——{len(files)} 个文件待分配："
          f"{' '.join(files)}")

    skill = agent.load_skill("P1-divide")
    decisions: dict[str, dict] = {}
    module_desc: dict[str, str] = {}
    for fname in files:
        dec, desc = _assign_one_file(skill, strategy, fname,
                                     file_index[fname], p1)
        if dec is None:
            _log.console_line(f"[porter] P1D: {fname} {MAX_TRIES} 次尝试均失败——退出"
                  f"（见 P1/logs/P1D_F_{fname}_R*.log）")
            return 1
        decisions[fname] = dec
        module_desc.update(desc)
        if "whole_file" in dec:
            _log.console_line(f"[porter] P1D: {fname} → 整文件 → {dec['whole_file']}")
        else:
            asg = dec.get("assignments") or {}
            kept = sum(1 for v in asg.values() if v)
            _log.console_line(f"[porter] P1D: {fname} → {len(asg)} 符号分配"
                  f"（{kept} 保留 / {len(asg) - kept} 裁剪）")

    plan, audit_md = index.expand(file_index, decisions, module_desc, strategy)
    (p1 / "reports" / "P1D_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    (p1 / "reports" / "P1D_audit.md").write_text(audit_md, encoding="utf-8")
    _log.console_line(f"[porter] P1D: plan 落盘 P1/reports/P1D_plan.json"
          f"（{len(plan['modules'])} 个模块）；审计 → P1D_audit.md")

    try:
        summary = frag_mod.extract_modules(ws, driver_root, plan)
    except frag_mod.DivideError as e:
        _log.console_line(f"[porter] P1D: 方案致命缺陷（本轮不做自动修正）：\n{e}")
        return 1

    _log.console_line(f"[porter] P1D: 抽取完成——{len(summary)} 个模块：")
    total = 0
    for mname, files_map in summary.items():
        n = sum(files_map.values())
        total += n
        _log.console_line(f"[porter] P1D:   {mname}（{len(files_map)} 文件，{n} 行）")
    _log.console_line(f"[porter] P1D: 合计抽取 {total} 行 → P1/modules/")
    return 0
