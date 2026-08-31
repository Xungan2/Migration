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

重跑即恢复：人工把答案写入 answers.md（`## <linux_api>` 或
`## retry <module>[-p3|-p4|-p5]`）后再次运行 loop。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import p3, p4, p5
from .state import MAX_ATTEMPTS, LoopState, parse_answers

_PHASE_STEPS = ("p3", "p4", "p5")


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
    print(f"[porter] loop: 人工重试指令已消费（{module} attempts 清零）")


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
    print(f"[porter] loop: {step.upper()}({module}) 失败 rc={rc}"
          f"（attempts {n}/{MAX_ATTEMPTS}）")
    if step == "p4":
        _reset_slice_progress(ws, module)
    if rc == 2 or n >= MAX_ATTEMPTS:
        _write_attempts_questions(ws, module, step, n)
        return True, (3 if n >= MAX_ATTEMPTS else 1)
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
    while phase != "done":
        if phase in ("pending", "p3"):
            if state.attempts(module, "p3") >= MAX_ATTEMPTS:
                _write_attempts_questions(ws, module, "p3", MAX_ATTEMPTS)
                return 3
            state.set_phase(module, "p3")
            rc = p3.run_p3(ws, module, order)
            if rc == 3:
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
                _write_attempts_questions(ws, module, "p4", MAX_ATTEMPTS)
                return 3
            rc = p4.run_p4(ws, module, order)
            if rc == 3:
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
                _write_attempts_questions(ws, module, "p5", MAX_ATTEMPTS)
                return 3
            rc = p5.run_p5(ws, module, order)
            if rc == 3:
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


def run_loop(ws: Path, module: str | None = None,
             max_modules: int | None = None) -> int:
    state = LoopState(ws)
    if not state.load_or_init():
        return 2
    proj_path = ws / "project.json"
    if not proj_path.exists():
        print(f"[porter] loop: 缺少 {proj_path}")
        return 2
    order = state.order

    # 指定模块须在序内
    if module and module not in order:
        print(f"[porter] loop: 模块 {module} 不在 deps.json order 中")
        return 2

    if module:
        pointer = state.pointer()
        if module != pointer:
            # 泊车绕行：deps 全满足才放行
            deps = _deps_of(ws, module)
            unmet = [d for d in deps if state.phase_of(d) != "done"]
            if unmet:
                print(f"[porter] loop: 绕行拒绝——{module} 的依赖未全部"
                      f" done（缺 {', '.join(unmet)}）")
                return 2
            print(f"[porter] loop: 绕行模式——只推进 {module}"
                  f"（断点指针仍为 {pointer or '无'}）")
            rc = _advance_module(ws, state, module, order)
            if rc is None:
                return 1              # 未烧穿但未完成（幂等重试入口）
            if rc == 0:
                print(f"[porter] loop: ✔ {module} 绕行完成"
                      f"（{len(state.done_set())}/{len(order)}）")
                parked = [m for m in order
                          if state.phase_of(m) != "done"]
                if parked:
                    print(f"[porter] loop: 泊车模块仍在卡点："
                          f"{', '.join(parked)}（attempts 烧穿走 answers.md）")
            return rc

    target = module or state.pointer()
    if target is None:
        print("[porter] loop: 全部模块已完成")
        _write_loop_report(ws, state)
        return 0

    n_done = 0
    while target is not None:
        if max_modules is not None and n_done >= max_modules:
            print(f"[porter] loop: 达到 --max-modules {max_modules}——暂停")
            break
        rc = _advance_module(ws, state, target, order)
        if rc is None:
            target = state.pointer()   # 幂等重试同一模块
            continue
        if rc != 0:
            _write_loop_report(ws, state)
            return rc
        n_done += 1
        print(f"[porter] loop: ✔ {target} 完成"
              f"（{len(state.done_set())}/{len(order)}）")
        target = state.pointer()
    _write_loop_report(ws, state)
    return 0


def _write_attempts_questions(ws: Path, module: str, step: str,
                              attempts: int) -> None:
    path = ws / "human_questions.md"
    lines = ["# loop 人工关口（exit 3）", "",
             f"- 模块：{module}（{step}）；时间："
             f"{datetime.now():%Y-%m-%d %H:%M}",
             f"- 原因：{step} 失败 {attempts} 次（超界）", "",
             "处理：查看对应阶段 reports/logs 定位问题，修复后在 "
             "answers.md 写：", "",
             f"    ## retry {module}-{step}", "",
             "然后重跑 loop（attempts 清零、断点续跑）。", ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
    print(f"[porter] loop: 报告 → {ws / 'reports' / 'loop_report.md'}")


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
