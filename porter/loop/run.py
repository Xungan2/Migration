"""run.py — loop 命令：垂直循环推进（plan §2/§10 定案 5）。

按 deps.json 拓扑序逐模块走 P3(M) → P4(M)，断点重入（loop_state.json）。
人工关口 = 连续直通 + 异常介入（§10.5），仅以下三种情况 exit 3 暂停：
  1. gap 决策 human（agent 两级尝试均无解）——P3 侧已写 human_questions.md
  2. 模块验收 FAIL 超界（attempts ≥ MAX_ATTEMPTS）
  3. deferred 无法清偿（消费者全 done 仍 FAIL）——P4 侧已写 human_questions.md

重跑即恢复：人工把答案写入 answers.md（`## <linux_api>` 或
`## retry <module>`）后再次运行 loop。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import p3, p4
from .state import MAX_ATTEMPTS, LoopState, parse_answers


def _handle_retry_answers(ws: Path, state: LoopState, module: str) -> None:
    """消费 `## retry <module>[-p3|-p4]` 答案：清零对应 attempts。"""
    answers = parse_answers(ws)
    if not answers:
        return
    from .state import consume_answers
    keys = []
    for k in answers:
        if k == f"retry {module}" or k == f"retry {module}-p3":
            keys.append(k)
        if k == f"retry {module}-p4":
            keys.append(k)
    if not keys:
        return
    consume_answers(ws, keys)
    for k in keys:
        step = "p3" if k.endswith("-p3") else ("p4" if k.endswith("-p4")
                                               else None)
        if step is None:
            state.reset_attempts(module, "p3")
            state.reset_attempts(module, "p4")
        else:
            state.reset_attempts(module, step)
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
        phase = state.phase_of(target)
        _handle_retry_answers(ws, state, target)
        if phase in ("pending", "p3"):
            if state.attempts(target, "p3") >= MAX_ATTEMPTS:
                _write_attempts_questions(ws, target, "p3", MAX_ATTEMPTS)
                _write_loop_report(ws, state)
                return 3
            state.set_phase(target, "p3")
            rc = p3.run_p3(ws, target, order)
            if rc == 3:
                _write_loop_report(ws, state)
                return 3
            if rc != 0:
                n = state.bump(target, "p3")
                print(f"[porter] loop: P3({target}) 失败 rc={rc}"
                      f"（attempts {n}/{MAX_ATTEMPTS}）")
                if rc == 2 or n >= MAX_ATTEMPTS:
                    _write_attempts_questions(ws, target, "p3", n)
                    _write_loop_report(ws, state)
                    return 3 if n >= MAX_ATTEMPTS else 1
                continue          # 幂等重试（下一轮循环）
            state.set_phase(target, "p4")
            phase = "p4"
        if phase == "p4":
            if state.attempts(target, "p4") >= MAX_ATTEMPTS:
                _write_attempts_questions(ws, target, "p4", MAX_ATTEMPTS)
                _write_loop_report(ws, state)
                return 3
            rc = p4.run_p4(ws, target, order)
            if rc == 3:
                _write_loop_report(ws, state)
                return 3
            if rc != 0:
                n = state.bump(target, "p4")
                print(f"[porter] loop: P4({target}) 失败 rc={rc}"
                      f"（attempts {n}/{MAX_ATTEMPTS}）")
                _reset_slice_progress(ws, target)
                if rc == 2 or n >= MAX_ATTEMPTS:
                    _write_attempts_questions(ws, target, "p4", n)
                    _write_loop_report(ws, state)
                    return 3 if n >= MAX_ATTEMPTS else 1
                continue
            state.set_phase(target, "done")
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
             "", "| 模块 | phase | p3 | p4 |", "|---|---|---|---|"]
    for m in state.order:
        mod = state.modules.get(m, {})
        att = mod.get("attempts", {})
        lines.append(f"| {m} | {mod.get('phase', '?')} "
                     f"| {att.get('p3', 0)} | {att.get('p4', 0)} |")
    lines += ["", f"- deferred：{d['open']} open / {d['cleared']} cleared"
              f"（残余归全局 P5）",
              f"- 平台补丁登记：{d['patches']} 条"
              f"（platform_patches.json，P6 上游补丁素材）"]
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
