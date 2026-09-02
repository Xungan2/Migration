"""run.py — loop 命令：垂直循环推进（plan §2/§10 定案 5；方案 A 重构）。

按 deps.json 拓扑序逐模块走 P3(M) → P4(M) → P5(M)，断点重入
（loop_state.json）。人工关口 = 连续直通 + 异常介入（§10.5），仅以下
四种情况 exit 3 暂停：
  1. gap 决策 human（agent 两级尝试均无解）——P3 侧已写 human_questions.md
  2. 模块阶段 FAIL 超界（某桶 attempts ≥ MAX_ATTEMPTS）
  3. deferred 无法清偿（消费者全 done 仍 FAIL）——P5 侧已写 human_questions.md
  4. 切片迁移被 agent 报 blocked（映射不可用）——停车走人工

泊车 + 绕过：模块 attempts 烧穿后 loop 以 exit 3 泊车（该模块保持卡点
相位）；人工可用 `--module <后续独立模块>` 绕行——须其 deps 全部 done，
否则拒绝（rc 2）。绕行只推进该模块走完剩余相位即 exit 0，不回落指针
循环（指针仍指向泊车模块）。

重跑即恢复（resume = 幂等重入）：人工把答案写入 answers.md 后再次运行
loop。新协议：`## @loop.attempts.<module>-<step>` 节 + note 字段（照
human_questions.md 表单）；旧键 `## retry <module>[-pN]` 兼容仍可用。
所有 exit-3 统一走 gates.panic()（§15 快照 + 账本 + 渲染）。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from .. import log as _log
from . import gates, p3, p4, p5
from .state import MAX_ATTEMPTS, LoopState, parse_answers

_PHASE_STEPS = ("p3", "p4", "p5")
# 墙钟预算（panic 信号：单模块全相位推进超此秒数 = 疑似死循环/空转）
MODULE_BUDGET_SEC = 3600


def _attempts_panic(ws: Path, module: str, step: str, attempts: int) -> int:
    """attempts 烧穿 → 统一 panic 关口（retry 类：note 可选诊断笔记）。"""
    return gates.panic(ws, {
        "id": f"loop.attempts.{module}-{step}", "kind": "retry",
        "gate_type": "failure", "phase": f"P{step[1]}", "module": module,
        "step": step,
        "question": (f"{step.upper()}({module}) 失败 {attempts} 次（超界）。"
                     "同一自动策略连续失败 = 系统性问题，请查看对应阶段 "
                     "reports/logs 与 gates 账本 history 诊断真因；"
                     "修复后作答将清零 attempts 并把诊断笔记带给下一轮。"),
        "context_files": [f"P{step[1]}/{module}/reports/migration.json",
                          f"P5/{module}/reports/acceptance.json"],
        "answer_form": [
            {"field": "note", "type": "text", "required": False,
             "hint": "诊断笔记（修了什么/绕过了什么；进下一轮 prompt 并留档）"}],
        "applies_to": {"modules": [module]},
    })


def _handle_retry_answers(ws: Path, state: LoopState, module: str) -> None:
    """消费 `## retry <module>[-p3|-p4|-p5]` 答案：清零对应 attempts。"""
    answers = parse_answers(ws)
    if not answers:
        return
    from .state import consume_answers
    keys = []
    suffixes = [""] + [f"-{s}" for s in _PHASE_STEPS]
    for sfx in suffixes:
        k = f"retry {module}{sfx}"
        if k in answers:
            keys.append(k)
    if not keys:
        return
    consume_answers(ws, keys)
    for k in keys:
        if k == f"retry {module}":
            for step in _PHASE_STEPS:
                state.reset_attempts(module, step)
        else:
            state.reset_attempts(module, k.rsplit("-", 1)[-1])
    _log.record("retry_reset", module=module, scope="loop",
                summary=f"人工重试指令已消费（{module} attempts 清零）")


def _reset_slice_progress(ws: Path, module: str) -> None:
    """重试 P4 时清掉失败切片记录（成功片保留，断点续迁）。"""
    mig = ws / "P4" / module / "reports" / "migration.json"
    if not mig.exists():
        return
    data = json.loads(mig.read_text(encoding="utf-8"))
    data["slices"] = [s for s in data.get("slices", []) if s.get("ok")]
    mig.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding="utf-8")


def _bump_and_maybe_park(ws: Path, state: LoopState, module: str, step: str,
                         rc: int) -> tuple[bool, int]:
    """阶段失败统一处理：bump + 判界。返回 (是否终止, 退出码)。"""
    n = state.bump(module, step)
    _log.record("phase_fail", module=module, step=step, rc=rc, scope="loop",
                level="warn",
                summary=f"{step.upper()}({module}) 失败 rc={rc}"
                        f"（attempts {n}/{MAX_ATTEMPTS}）")
    if step == "p4":
        _reset_slice_progress(ws, module)
    if rc == 2 or n >= MAX_ATTEMPTS:
        return True, (_attempts_panic(ws, module, step, n)
                      if n >= MAX_ATTEMPTS else 1)
    return False, 0


def _deps_of(ws: Path, module: str) -> list[str]:
    try:
        deps = json.loads((ws / "P1" / "modules" / "deps.json")
                          .read_text(encoding="utf-8"))
        return list((deps.get("edges") or {}).get(module) or [])
    except (OSError, json.JSONDecodeError):
        return []


def _advance_module(ws: Path, state: LoopState, module: str,
                    order: list[str]) -> int | None:
    """推进单模块剩余相位（p3→p4→p5）。返回 0=done / None=可幂等重试 /
    其他=终止退出码。"""
    phase = state.phase_of(module)
    _handle_retry_answers(ws, state, module)
    t0 = time.monotonic()
    while phase != "done":
        # panic 信号：墙钟预算超限（疑似 agent 空转/死循环）
        if time.monotonic() - t0 > MODULE_BUDGET_SEC:
            return gates.panic(ws, {
                "id": f"loop.budget.{module}", "kind": "retry",
                "gate_type": "failure", "phase": "loop", "module": module,
                "step": phase if phase in _PHASE_STEPS else None,
                "question": (f"模块 {module} 相位推进超墙钟预算 "
                             f"{MODULE_BUDGET_SEC}s（当前相位 {phase}）——"
                             "疑似 agent 空转或死循环，请查 events.jsonl "
                             "尾部与各相位 logs。"),
                "context_files": ["events.jsonl"],
                "answer_form": [
                    {"field": "note", "type": "text", "required": False,
                     "hint": "诊断笔记（超时原因与处置）"}],
                "applies_to": {"modules": [module]},
            })
        if phase in ("pending", "p3"):
            if state.attempts(module, "p3") >= MAX_ATTEMPTS:
                return _attempts_panic(ws, module, "p3", MAX_ATTEMPTS)
            state.set_phase(module, "p3")
            rc = p3.run_p3(ws, module, order)
            if rc == 3:
                if not gates.GateLedger(ws).load().open_blocking():
                    return None           # 路由层已消化（决策债）——幂等重进
                return 3
            if rc != 0:
                stop, code = _bump_and_maybe_park(ws, state, module,
                                                  "p3", rc)
                if stop:
                    return code
                return None           # 幂等重试（外层再进）
            state.set_phase(module, "p4")
            phase = "p4"
        if phase == "p4":
            if state.attempts(module, "p4") >= MAX_ATTEMPTS:
                return _attempts_panic(ws, module, "p4", MAX_ATTEMPTS)
            rc = p4.run_p4(ws, module, order)
            if rc == 3:
                if not gates.GateLedger(ws).load().open_blocking():
                    return None           # blocked 被路由层消化——幂等重进
                return 3
            if rc != 0:
                stop, code = _bump_and_maybe_park(ws, state, module,
                                                  "p4", rc)
                if stop:
                    return code
                return None
            state.set_phase(module, "p5")
            phase = "p5"
        if phase == "p5":
            if state.attempts(module, "p5") >= MAX_ATTEMPTS:
                return _attempts_panic(ws, module, "p5", MAX_ATTEMPTS)
            rc = p5.run_p5(ws, module, order)
            if rc == 3:
                if not gates.GateLedger(ws).load().open_blocking():
                    return None           # deferred 被路由层消化——幂等重进
                return 3
            if rc != 0:
                stop, code = _bump_and_maybe_park(ws, state, module,
                                                  "p5", rc)
                if stop:
                    return code
                return None
            state.set_phase(module, "done")
            phase = "done"
    return 0


def _settle_debt_checkpoint(ws: Path) -> None:
    """债检查点结算：cp.debt.* 已批审 → 批量结清当前决策债。

    - approve：pending 决策债全部 resolved（批量批审）；逐条否决留给
      `## @<gate_id>` verdict: veto（先于本检查点作答即可）。
    - reject：关口重开（保持阻塞——人须逐条处理或改为 approve）。
    """
    ledger = gates.GateLedger(ws).load()
    acted = False
    for g in ledger.gates:
        if not g["id"].startswith("cp.debt."):
            continue
        if g.get("status") != "applied":
            continue
        verdict = (g.get("answer") or {}).get("verdict", "").lower()
        if verdict == "approve":
            for d in list(ledger.pending_review()):
                if not d["id"].startswith("cp.debt."):
                    gates.resolve_applied(ledger, d["id"],
                                          "债检查点批量批审")
            gates.resolve_applied(ledger, g["id"], "债检查点完成")
            acted = True
        elif verdict == "reject":
            g["status"] = "open"          # 重开：债务仍须处置
            g["history"].append({"time": datetime.now().isoformat(
                timespec="seconds"), "event": "reopened",
                "detail": "reject——请逐条 veto/approve 后重新 approve"})
            acted = True
    if acted:
        ledger.save()


def _debt_checkpoint(ws: Path, state: LoopState) -> int:
    """债限额软停：收窄计数（skip/measure/low）≥ 限额 → 批审检查点。"""
    from . import routing as _routing
    ledger = gates.GateLedger(ws).load()
    n = _routing.debt_count(ledger)
    if n < _routing.debt_limit(ws):
        return 0
    seq = sum(1 for g in ledger.gates if g["id"].startswith("cp.debt."))
    return gates.checkpoint_run(ws, "CP-DEBT", register=[{
        "id": f"cp.debt.{seq}", "kind": "approval", "gate_type": "decision",
        "phase": "loop", "checkpoint": "CP-DEBT",
        "question": (f"决策债达限额（{n} ≥ {_routing.debt_limit(ws)}，"
                     "收窄计数：跳过决策/量尺修改/低置信）。digest 列出全部"
                     "债项——逐条否决用 `## @<债项id>` verdict: veto；"
                     "整体放行 approve（批量结清）。"),
        "context_files": ["checkpoints/CP-DEBT_digest.md"],
        "answer_form": [
            {"field": "verdict", "type": "enum",
             "options": ["approve", "reject"], "required": True},
            {"field": "note", "type": "text", "required": False}]}])


def run_loop(ws: Path, module: str | None = None,
             max_modules: int | None = None) -> int:
    state = LoopState(ws)
    if not state.load_or_init():
        return 2
    proj_path = ws / "project.json"
    if not proj_path.exists():
        _log.record("loop_abort", level="error", scope="loop",
                    summary=f"缺少 {proj_path}")
        return 2
    # 消费 answers.md 的 `## @<gate_id>` 节（新协议：校验→记账→应用）
    gates.process_answered_gates(ws)
    _settle_debt_checkpoint(ws)
    order = state.order

    # 指定模块须在序内
    if module and module not in order:
        _log.record("loop_abort", level="error", scope="loop",
                    summary=f"模块 {module} 不在 deps.json order 中")
        return 2

    if module:
        pointer = state.pointer()
        if module != pointer:
            # 泊车绕行：deps 全满足才放行
            deps = _deps_of(ws, module)
            unmet = [d for d in deps if state.phase_of(d) != "done"]
            if unmet:
                _log.record("bypass_rejected", level="error", scope="loop",
                            summary=f"绕行拒绝——{module} 的依赖未全部"
                                    f" done（缺 {', '.join(unmet)}）")
                return 2
            _log.record("bypass_mode", module=module, scope="loop",
                        summary=f"绕行模式——只推进 {module}"
                                f"（断点指针仍为 {pointer or '无'}）")
            rc = _advance_module(ws, state, module, order)
            if rc is None:
                return 1              # 未烧穿但未完成（幂等重试入口）
            if rc == 0:
                _log.record("module_done", module=module, scope="loop",
                            summary=f"✔ {module} 绕行完成"
                                    f"（{len(state.done_set())}/"
                                    f"{len(order)}）")
                parked = [m for m in order
                          if state.phase_of(m) != "done"]
                if parked:
                    _log.record("parked_remaining", scope="loop",
                                level="warn",
                                summary=f"泊车模块仍在卡点："
                                        f"{', '.join(parked)}"
                                        "（attempts 烧穿走 answers.md）")
            return rc

    target = module or state.pointer()
    if target is None:
        _log.record("all_done", scope="loop",
                    summary="全部模块已完成")
        _write_loop_report(ws, state)
        return 0

    n_done = 0
    while target is not None:
        if max_modules is not None and n_done >= max_modules:
            _log.record("max_modules_reached", scope="loop",
                        summary=f"达到 --max-modules {max_modules}——暂停")
            break
        rc = _advance_module(ws, state, target, order)
        if rc is None:
            target = state.pointer()   # 幂等重试同一模块
            continue
        if rc != 0:
            _write_loop_report(ws, state)
            return rc
        n_done += 1
        _log.record("module_done", module=target, scope="loop",
                    summary=f"✔ {target} 完成"
                            f"（{len(state.done_set())}/{len(order)}）")
        # 债限额软停（收窄计数；夜间自治 = limit 默认 30）
        rc = _debt_checkpoint(ws, state)
        if rc != 0:
            _write_loop_report(ws, state)
            return rc
        # FM 首模块检查点（默认开：用最小沉没成本抓系统性问题，
        # resolved 后永不再停）
        if n_done == 1 and gates.first_module_review_enabled():
            fm = gates.GateLedger(ws).load().find(f"cp.fm.{target}")
            if fm is None or fm.get("status") in ("open", "invalid"):
                rc = gates.checkpoint_run(ws, "FM", register=[{
                    "id": f"cp.fm.{target}", "kind": "approval",
                    "gate_type": "decision", "phase": "loop",
                    "checkpoint": "FM", "module": target,
                    "question": (
                        f"首模块 {target} 已走完 P3→P4→P5。digest 汇集其"
                        "决策债/判据/代码量/attempts——回答一个问题："
                        "这套模式可以放心复制给剩余模块吗？"),
                    "context_files": [
                        f"P3/{target}/reports/gap_decisions.json",
                        f"P5/{target}/reports/acceptance.json"],
                    "answer_form": [
                        {"field": "verdict", "type": "enum",
                         "options": ["approve", "veto"], "required": True},
                        {"field": "note", "type": "text", "required": False,
                         "hint": "调整要求（如改 policy 规则/映射）"}],
                    "applies_to": {"modules": [target]}}])
                if rc != 0:
                    _write_loop_report(ws, state)
                    return rc
        target = state.pointer()
    _write_loop_report(ws, state)
    if state.pointer() is None:
        # CP3 指针：L4 草案生成 + 定稿审（p6.l4.finalize 关口即 CP3 载体）
        _log.record("cp3_next", scope="loop",
                    summary="全部模块完成——CP3 入口：`p6 --draft-l4`"
                            "（草案生成）→ `p6 --finalize-l4`（人审定稿）")
        gates.checkpoint_digest(ws, "CP3")
    return 0


def _write_loop_report(ws: Path, state: LoopState) -> None:
    d = _deferred_summary(ws)
    lines = ["# 垂直循环进度报告", "",
             f"- 时间：{datetime.now():%Y-%m-%d %H:%M}",
             f"- 完成：{len(state.done_set())}/{len(state.order)}",
             "", "| 模块 | phase | p3 | p4 | p5 |", "|---|---|---|---|---|"]
    for m in state.order:
        mod = state.modules.get(m, {})
        att = mod.get("attempts", {})
        lines.append(f"| {m} | {mod.get('phase', '?')} "
                     f"| {att.get('p3', 0)} | {att.get('p4', 0)} "
                     f"| {att.get('p5', 0)} |")
    lines += ["", f"- deferred：{d['open']} open / {d['cleared']} cleared"
              f"（残余归全局 P6 系统验收）",
              f"- 平台补丁登记：{d['patches']} 条"
              f"（platform_patches.json，P7 上游补丁素材）"]
    (ws / "reports" / "loop_report.md").parent.mkdir(parents=True,
                                                     exist_ok=True)
    (ws / "reports" / "loop_report.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")
    _log.record("report_written", scope="loop", ref={"report":
               "reports/loop_report.md"},
                summary=f"报告 → {ws / 'reports' / 'loop_report.md'}")


def _deferred_summary(ws: Path) -> dict:
    out = {"open": 0, "cleared": 0, "patches": 0}
    try:
        d = json.loads((ws / "deferred.json").read_text(encoding="utf-8"))
        out["open"] = sum(1 for e in d.get("entries", [])
                          if e.get("status") == "open")
        out["cleared"] = sum(1 for e in d.get("entries", [])
                             if e.get("status") == "cleared")
    except (OSError, json.JSONDecodeError):
        pass
    try:
        p = json.loads((ws / "platform_patches.json").read_text(
            encoding="utf-8"))
        out["patches"] = len(p.get("patches", []))
    except (OSError, json.JSONDecodeError):
        pass
    return out
