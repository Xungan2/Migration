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
from ..common import scope as scope_mod
from . import fragments as frag_mod
from . import index
from .. import log as _log

MAX_TRIES = 2          # 每文件：首发 + 催补重试 1 次
AGENT_TIMEOUT_SEC = 900


def _assign_one_file_impl(skill: str, strategy: str, fname: str,
                          entries: list, p1: Path,
                          task_id: str = "") -> tuple[dict | None, dict]:
    """单文件 agent 分配。返回 (决定, module_desc)；失败返回 (None, {})。"""
    total = entries[-1].end
    base = (f"{skill}\n\n---\n\n## 拆分策略（已人工审阅，按它执行）\n\n"
            f"{strategy}\n\n---\n\n## 任务数据\n\n"
            f"本次调用只处理一个源文件：`{fname}`。\n\n"
            f"{index.render_slice(fname, entries, total)}\n\n"
            f"请按 SKILL 输出该文件的分配结果（只输出一个 JSON 块）。")
    proj_path = p1.parent / "project.json"
    proj = json.loads(proj_path.read_text(encoding="utf-8")) if proj_path.exists() else {}
    scope_path = p1 / "scope.json"
    if scope_path.exists():
        features = json.loads(scope_path.read_text(encoding="utf-8")).get("features")
        if features is not None:
            base += ("\n\n## 已审功能范围（scope.json，逐项遵循）\n"
                     + json.dumps(features, ensure_ascii=False, indent=2))
    if proj.get("linux_driver"):
        base += (f"\n源码位置：{Path(proj['linux_driver']) / fname}"
                 f"\n驱动类别：{proj.get('category') or []}"
                 "\n符号用途不明时只读核实函数体、调用者与回调注册；按已审策略分配。")
    feedback = ""
    for attempt in range(1, MAX_TRIES + 1):
        def _attempt():
            rc, out = agent.run_agent(
                base + feedback, workdir=p1,
                log_stem=str(p1 / "logs" /
                             f"P1D_F_{fname.replace('/', '__')}_R{attempt}"),
                timeout_sec=AGENT_TIMEOUT_SEC,
                task={"phase": "p1", "step": "divide-file",
                      "attempt": attempt, "task_id": task_id})
            parsed = agent.extract_json(out) if rc == 0 else None
            return rc, parsed, index.validate_decision(entries, parsed)

        from ..handoff import current_execution, run_task, TaskSpec
        if current_execution() is not None:
            rc, parsed, err = run_task(
                p1.parent,
                TaskSpec(task_id, ("p1.strategy",), (p1 / "strategy.md",),
                         "one P1 source-file assignment attempt"),
                _attempt,
                success=lambda value: value[0] == 0 and value[2] is None,
                summary=lambda value: (
                    f"Assignment for {fname}: rc={value[0]}, "
                    f"validation={value[2] or 'accepted'}."),
                verification=lambda value: (
                    "index.validate_decision accepted output" if value[2] is None
                    else f"index.validate_decision rejected output: {value[2]}",))
        else:
            rc, parsed, err = _attempt()
        if err is None:
            desc = parsed.get("module_desc")
            return parsed, (desc if isinstance(desc, dict) else {})
        head = err.splitlines()[0]
        _log.console_line(f"[porter] P1D: {fname} 第 {attempt} 次输出不合规：{head}")
        feedback = (f"\n\n---\n\n## 上一次输出的问题（修正后重新输出"
                    f"完整 JSON）\n\n{err}")
    return None, {}


def _assign_one_file(skill: str, strategy: str, fname: str,
                     entries: list, p1: Path) -> tuple[dict | None, dict]:
    """Assign one source file; every provider attempt is a fresh execution."""
    safe = __import__("re").sub(r"[^A-Za-z0-9_.-]+", "_", fname)
    return _assign_one_file_impl(
        skill, strategy, fname, entries, p1, f"p1.divide.file.{safe}")


def run_divide(ws: Path, driver_root: Path) -> int:
    """返回 0=成功；2=前置缺失；1=失败。"""
    p1 = ws / "P1"
    plan_path = p1 / "reports" / "P1D_plan.json"
    try:
        scope_set = scope_mod.load_scope(ws, driver_root)
        fingerprint = scope_mod.input_fingerprint(ws, driver_root)
    except (scope_mod.ScopeError, OSError, ValueError) as e:
        _log.console_line(f"[porter] P1D: {e}")
        return 2
    stamp_path = p1 / "reports" / "P1D_inputs.json"
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            scope_mod.validate_plan_scope(ws, driver_root, plan)
            if scope_set is not None and not stamp_path.exists():
                raise scope_mod.ScopeError(
                    "旧范围计划缺少源码指纹，不能确认与当前源码一致；"
                    "请在新工作区重新运行 P1，或通过 p1-import 重新校验导入")
            if stamp_path.exists() and json.loads(stamp_path.read_text(
                    encoding="utf-8")).get("fingerprint") != fingerprint:
                raise scope_mod.ScopeError(
                    "意图、scope、策略或源码已变化；请在新工作区重新运行 P1，"
                    "保留当前产物供对照，不能复用旧 plan")
        except (ValueError, OSError, KeyError) as e:
            _log.console_line(f"[porter] P1D: {e}")
            return 2
        expected = {m["name"] for m in plan["modules"]}
        actual = {p.parent.name for p in (p1 / "modules").glob("*/module.json")}
        missing_files = any(not (p1 / "modules" / m["name"] / f["dest"]).is_file()
                            for m in plan["modules"] for f in m["files"])
        if expected != actual or missing_files:
            try:
                plan = json.loads(plan_path.read_text(encoding="utf-8"))
                summary = frag_mod.extract_modules(ws, driver_root, plan)
            except (OSError, json.JSONDecodeError) as e:
                _log.console_line(f"[porter] P1D: 复用 {plan_path} 读取失败：{e}")
                return 1
            except frag_mod.DivideError as e:
                _log.console_line(f"[porter] P1D: 复用时按现存 plan 重建抽取失败：\n{e}")
                return 1
            total = sum(n for fm in summary.values() for n in fm.values())
            _log.console_line(f"[porter] P1D: modules/ 缺失——按现存 plan 重建抽取"
                  f"（{len(summary)} 模块 {total} 行）")
        _log.console_line(f"[porter] P1D: 复用 {plan_path}（如需重做请删除该文件）")
        return 0
    strategy_path = p1 / "strategy.md"
    if not strategy_path.exists():
        _log.console_line(f"[porter] P1D: 缺少 {strategy_path}（divide 执行已审阅的策略——"
              f"先跑 p1-strategy 并人工放行）")
        return 2
    strategy = strategy_path.read_text(encoding="utf-8")

    file_index = index.build_index(driver_root, scope_set)
    files = index.call_order(file_index)
    if not files:
        _log.console_line(f"[porter] P1D: {driver_root} 下未发现含定义的 *.c/*.h——失败")
        return 2
    # scope 白名单（范围声明层）：意图工作区只分配闭包内文件
    if scope_set is not None:
        total = len(files)
        files = [f for f in files if f in scope_set]
        file_index = {k: v for k, v in file_index.items() if k in scope_set}
        if not files:
            _log.console_line("[porter] P1D: scope 白名单（P1/scope.json）内"
                  "无含定义的可分配文件——失败")
            return 2
        _log.console_line(f"[porter] P1D: scope 白名单生效——{len(files)}/{total}"
              " 个文件进入分配")
    (p1 / "logs").mkdir(parents=True, exist_ok=True)
    (p1 / "reports").mkdir(parents=True, exist_ok=True)
    _log.console_line(f"[porter] P1D: 索引预建完成——{len(files)} 个文件待分配："
          f"{' '.join(files)}")

    skill = agent.load_skill("P1-divide")
    cache_path = p1 / "reports" / "P1D_assignments.json"
    cached = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    if cached.get("fingerprint") != fingerprint:
        cached = {}
    decisions: dict[str, dict] = cached.get("decisions", {})
    module_desc: dict[str, str] = cached.get("module_desc", {})
    for fname in files:
        dec = decisions.get(fname)
        desc = {}
        cached_error = index.validate_decision(file_index[fname], dec)
        if cached_error is not None:
            dec, desc = _assign_one_file(skill, strategy, fname,
                                         file_index[fname], p1)
        else:
            # Revalidate an existing cache entry and create a current explicit
            # handoff; old artifacts alone never bypass the protocol.
            from ..handoff import current_execution, run_task, TaskSpec
            if current_execution() is not None:
                safe = __import__("re").sub(r"[^A-Za-z0-9_.-]+", "_", fname)
                run_task(
                    ws, TaskSpec(f"p1.divide.file.{safe}", ("p1.strategy",),
                                 (strategy_path, cache_path),
                                 "revalidated cached source-file assignment"),
                    lambda: 0,
                    summary=f"Revalidated cached assignment for {fname}.",
                    artifacts=(cache_path,),
                    verification=("index.validate_decision accepted cached entry",))
        if dec is None:
            _log.console_line(f"[porter] P1D: {fname} {MAX_TRIES} 次尝试均失败——退出"
                  f"（见 P1/logs/P1D_F_{fname}_R*.log）")
            return 1
        decisions[fname] = dec
        module_desc.update(desc)
        cache_path.write_text(json.dumps({
            "fingerprint": fingerprint, "decisions": decisions,
            "module_desc": module_desc}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
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
    stamp_path.write_text(json.dumps({"fingerprint": fingerprint}, indent=2) + "\n",
                          encoding="utf-8")
    (p1 / "reports" / "P1D_decisions.json").write_text(
        json.dumps(decisions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0
