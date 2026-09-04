"""extract.py — T3 环境信息提取（agent 多轮循环 + 真实探测交织 + 人工升级）。

流程（用户定稿）：
    R1 提取 → 探测 → R2 修正 → 探测 → R3 修正 → 探测
    全绿即成（missing 若非空 → 生成非阻塞确认问题，不暂停）
    3 轮未成 → P0/reports/human_questions.md（阻塞），exit 3
    人填写 answers.md 后重跑 → R4 答案整合 → 终测 → 成败定局

探测为金标准：三项（build/boot/boot_with_device）双信号全 PASS 即通过。
runner 最小契约校验失败视同该轮失败，缺陷作为反馈进下一轮。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..common import agent
from . import probe as probe_mod
from .. import log as _log

MAX_AUTO_ROUNDS = 3


# ---------- runner 最小契约校验（字段级，4 条） ----------

def validate_runner(r: dict) -> list[str]:
    defects = []
    for section in ("build", "boot", "inject_device"):
        if not isinstance(r.get(section), dict):
            defects.append(f"缺 {section} 节")
            r[section] = {}
    b, bo, inj = r["build"], r["boot"], r["inject_device"]
    if not b.get("cmd"):
        defects.append("build.cmd 为空")
    for f in ("timeout_full_sec", "timeout_inc_sec"):
        v = b.get(f)
        if not isinstance(v, (int, float)) or v <= 0:
            defects.append(f"build.{f} 缺失或非法")
    if isinstance(b.get("timeout_full_sec"), (int, float)) and \
       isinstance(b.get("timeout_inc_sec"), (int, float)) and \
       b["timeout_full_sec"] < b["timeout_inc_sec"]:
        defects.append("build.timeout_full_sec 应 ≥ timeout_inc_sec")
    if not bo.get("cmd"):
        defects.append("boot.cmd 为空")
    if not isinstance(bo.get("timeout_sec"), (int, float)) or \
            bo.get("timeout_sec", 0) <= 0:
        defects.append("boot.timeout_sec 缺失或非法"
                       "（probe_boot 强依赖——缺失会裸 KeyError，H10）")
    if not bo.get("log_is_stdout") and not bo.get("log_file"):
        defects.append("boot.log_file 为空（且 log_is_stdout 非 true）")
    for f in ("success_pattern", "panic_pattern"):
        if not bo.get(f):
            defects.append(f"boot.{f} 为空")
    mech = inj.get("mechanism")
    if mech not in ("env", "cmd"):
        defects.append("inject_device.mechanism 必须为 env|cmd")
    else:
        carrier = (inj.get("env") or {}) if mech == "env" \
            else {"cmd_suffix": inj.get("cmd_suffix")}
        if not carrier or not any("<DEVICE_ARGS>" in str(v) for v in carrier.values()):
            defects.append(f"inject_device（mechanism={mech}）载体缺 <DEVICE_ARGS> 占位符")
    if not inj.get("example_args"):
        defects.append("inject_device.example_args 为空")
    for f in ("driver_success_pattern", "driver_fail_pattern"):
        v = inj.get(f)
        if v is not None and not (isinstance(v, str) and v.strip()):
            defects.append(f"inject_device.{f} 须为非空字符串或 null"
                           "（null=目标 OS 无该类别内置驱动，不做驱动级判定）")
    return defects


# ---------- prompt 组装 ----------

def _prompt_r1(skill: str, materials: list[Path], target_os: Path,
               categories: list[str], catalog: str = "") -> str:
    mat_lines = "\n".join(f"  - {m.resolve()}" for m in materials) or "  （无——仅凭源码树）"
    return (f"{skill}\n\n---\n\n## 任务数据（首轮提取）\n\n"
            f"资料列表（自己去读，文件或目录均可）：\n{mat_lines}\n"
            f"目标 OS 源码树：`{target_os.resolve()}`（树内 README/构建文件/CI 配置同样是你可用的资料）\n"
            f"设备类别标签：{categories or '未知'}\n"
            + (f"\n{catalog}\n" if catalog else "")
            + "\n按 SKILL 提取并只输出一个 JSON 块。")


def _prompt_fix(skill: str, round_no: int, prev_output: dict,
                probe_results: list[dict], defects: list[str]) -> str:
    return (f"{skill}\n\n---\n\n## 修正轮（R{round_no}）\n\n"
            f"你上一轮（R{round_no - 1}）的输出：\n```json\n"
            f"{json.dumps(prev_output, ensure_ascii=False, indent=2)}\n```\n\n"
            f"真实探测结果：\n```json\n"
            f"{json.dumps(probe_results, ensure_ascii=False, indent=2)}\n```\n\n"
            f"契约校验缺陷（若有）：{defects or '无'}\n\n"
            f"按 SKILL「修正轮」指令修正，输出完整 JSON 块（全量输出，不要只给 diff）。")


def _prompt_answers(skill: str, rounds: list[dict], probes: list[list[dict]],
                    answers: str) -> str:
    history = ""
    for i, (out, pr) in enumerate(zip(rounds, probes), 1):
        history += (f"### R{i} 输出\n```json\n{json.dumps(out, ensure_ascii=False, indent=2)}\n```\n"
                    f"### R{i} 探测\n```json\n{json.dumps(pr, ensure_ascii=False, indent=2)}\n```\n")
    return (f"{skill}\n\n---\n\n## 答案整合轮（R4）\n\n"
            f"历史轮次：\n{history}\n"
            f"开发人员的书面回答（answers.md）：\n{answers}\n\n"
            f"按 SKILL「答案整合轮」指令整合，输出完整 JSON 块。")


# ---------- 人工问题生成 ----------

def _write_questions(ws: Path, rounds: list[dict], probes: list[list[dict]]) -> None:
    p0 = ws / "P0"
    last = rounds[-1]
    lines = ["# T3 人工介入问题（自动生成）", "",
             f"agent 已尝试 {len(rounds)} 轮自动提取/修正，以下问题仍无法解决。",
             "请把答案写在 `answers.md`（工作区根目录，逐题编号作答），然后重跑工具。", ""]
    n = 0
    for m in last.get("missing", []):
        n += 1
        lines += [f"## Q{n}（缺失：{m.get('field')}）",
                  f"- 为什么难：{m.get('why_hard')}",
                  f"- 已尝试：{'; '.join(m.get('tried', []))}", ""]
    for pr in probes[-1]:
        if not pr.get("ok"):
            n += 1
            lines += [f"## Q{n}（探测失败：{pr.get('item')}）",
                      f"- 详情：{pr.get('detail')}",
                      "- 完整日志见 P0/logs/ 下对应文件", ""]
    (p0 / "reports").mkdir(parents=True, exist_ok=True)
    (p0 / "reports" / "human_questions.md").write_text("\n".join(lines), encoding="utf-8")
    _log.console_line(f"[porter] T3: 人工问题已生成 {p0/'reports'/'human_questions.md'}（{n} 问）")


# ---------- 探测（顺序依赖，全绿为过） ----------

def _run_probes(ws: Path, target_os: Path, runner: dict,
                categories: list[str], round_no: int) -> list[dict]:
    p0 = ws / "P0"
    (p0 / "logs").mkdir(parents=True, exist_ok=True)
    results = [probe_mod.probe_build(p0, target_os, runner)]
    if results[-1]["ok"]:
        results.append(probe_mod.probe_boot(p0, target_os, runner,
                                            label="boot"))
        if results[-1]["ok"]:
            results.append(probe_mod.probe_boot_with_device(
                p0, target_os, runner, categories, label="boot_with_device",
                check_driver=True))
    (p0 / "reports").mkdir(exist_ok=True)
    (p0 / "reports" / f"T3_probes_R{round_no}.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


# ---------- 主循环 ----------

def extract_env(ws: Path, target_os: Path, materials: list[Path],
                categories: list[str]) -> int:
    """返回 0=成功（runner.json 已写）；3=需人工（问题已生成）；其他=失败。"""
    try:                                # 观测扩全（H12）：P0 相位埋桩
        from ..loop import events as _ev
        _ev.bind(ws, "p0")
    except Exception:
        pass
    p0 = ws / "P0"
    (p0 / "logs").mkdir(parents=True, exist_ok=True)
    (p0 / "reports").mkdir(exist_ok=True)
    runner_path = ws / "runner.json"
    if runner_path.exists():
        _log.console_line(f"[porter] T3: 复用 {runner_path}")
        return 0

    skill = agent.load_skill("P0-env-extract")
    answers_path = ws / "answers.md"
    rounds_out: list[dict] = []
    rounds_probes: list[list[dict]] = []

    # ---- R4：答案整合（双协议：gates 账本答案优先，legacy 全文兼容） ----
    gate_text = ""
    try:
        from ..loop import gates as gates_mod
        ledger = gates_mod.GateLedger(ws).load()
        g = ledger.find("p0.t3.extract")
        if g and g.get("answer") and (g["answer"].get("answers") or "").strip():
            gate_text = g["answer"]["answers"]
    except Exception:
        pass
    legacy_text = ""
    if answers_path.exists():
        raw = answers_path.read_text(encoding="utf-8")
        # 剔除 @ 关口节（已由账本管理）后仍有内容 → legacy 自由文本
        keep: list[str] = []
        skip = False
        for ln in raw.splitlines():
            if ln.startswith("## @"):
                skip = True
                continue
            if ln.startswith("## "):
                skip = False
            if not skip:
                keep.append(ln)
        legacy_text = "\n".join(keep).strip()
    answer_text = gate_text or legacy_text
    if answer_text:
        for i in range(1, MAX_AUTO_ROUNDS + 1):
            p_out = p0 / "reports" / f"T3_R{i}.json"
            p_pr = p0 / "reports" / f"T3_probes_R{i}.json"
            if p_out.exists():
                rounds_out.append(json.loads(p_out.read_text(encoding="utf-8")))
                rounds_probes.append(json.loads(
                    p_pr.read_text(encoding="utf-8")) if p_pr.exists() else [])
        rc, out = agent.run_agent(
            _prompt_answers(skill, rounds_out, rounds_probes, answer_text),
            workdir=p0, log_stem=str(p0 / "logs" / "T3_R4"), timeout_sec=900)
        parsed = agent.extract_json(out) if rc == 0 else None
        if not parsed or not parsed.get("runner"):
            _log.console_line("[porter] T3: R4 输出无法解析——请检查 answers.md 后重跑")
            return 1
        defects = validate_runner(parsed["runner"])
        if defects:
            _log.console_line(f"[porter] T3: R4 runner 仍有契约缺陷: {defects}")
            return 1
        probes = _run_probes(ws, target_os, parsed["runner"], categories, 4)
        if all(p["ok"] for p in probes):
            _finish(ws, parsed, probes)
            return 0
        _log.console_line(f"[porter] T3: R4 终测仍未全绿——见 P0/reports/T3_probes_R4.json")
        return 1

    # ---- R1..R3：自动循环 ----
    # runbook 目录注入（起点假设——环境会漂移，命令/特征仍须本轮实测复核）
    try:
        from ..bootstrap import kb as _kb
        runbook_cat = _kb.kb_face(ws, ["runbook"])
    except Exception:
        runbook_cat = ""
    for round_no in range(1, MAX_AUTO_ROUNDS + 1):
        if round_no == 1:
            prompt = _prompt_r1(skill, materials, target_os, categories,
                                runbook_cat)
        else:
            prompt = _prompt_fix(skill, round_no, rounds_out[-1],
                                rounds_probes[-1], prev_defects)
        rc, out = agent.run_agent(
            prompt, workdir=p0,
            log_stem=str(p0 / "logs" / f"T3_R{round_no}"), timeout_sec=900)
        parsed = agent.extract_json(out) if rc == 0 else None
        if not parsed or not parsed.get("runner"):
            parsed = {"runner": {}, "missing": [{"field": "（输出无法解析）",
                       "why_hard": f"R{round_no} agent 输出不含 runner",
                       "tried": []}]}
            prev_defects = ["agent 输出无法解析为 JSON"]
            rounds_out.append(parsed)
            rounds_probes.append([])
            continue
        prev_defects = validate_runner(parsed["runner"])
        rounds_out.append(parsed)
        (p0 / "reports").mkdir(exist_ok=True)
        (p0 / "reports" / f"T3_R{round_no}.json").write_text(
            json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")

        if prev_defects:
            _log.console_line(f"[porter] T3: R{round_no} 契约缺陷（作为反馈进下一轮）: "
                  f"{prev_defects}")
            rounds_probes.append([])
            continue
        probes = _run_probes(ws, target_os, parsed["runner"], categories, round_no)
        rounds_probes.append(probes)
        all_ok = len(probes) == 3 and all(p["ok"] for p in probes)
        _log.console_line(f"[porter] T3: R{round_no} 探测 "
              f"{'/'.join(p['item'] + '=' + ('P' if p['ok'] else 'F') for p in probes)}")
        if all_ok:
            _finish(ws, parsed, probes)
            return 0

    # ---- 3 轮未成 → 人工升级（统一 panic 关口） ----
    _write_questions(ws, rounds_out, rounds_probes)
    from ..loop import gates as gates_mod
    gates_mod.panic(ws, {
        "id": "p0.t3.extract", "kind": "fact", "gate_type": "decision",
        "phase": "P0",
        "question": (
            "3 轮自动环境提取未完成——剩余环境事实在 agent 可猜测空间外，"
            "需开发者书面补课（题面见 P0/reports/human_questions.md，"
            "逐题编号作答）。答案全文将注入 R4 轮 agent。"),
        "context_files": ["P0/reports/human_questions.md",
                          "P0/reports/T3_R1.json"],
        "answer_form": [
            {"field": "answers", "type": "text", "required": True,
             "hint": "逐题编号作答（自由文本，全文注入 R4 agent prompt）"}],
    })
    _log.console_line("[porter] T3: 3 轮自动提取未完成 → 请按表单作答后重跑（exit 3）")
    return 3


def _finish(ws: Path, parsed: dict, probes: list[dict]) -> None:
    p0 = ws / "P0"
    runner = parsed["runner"]
    runner.setdefault("meta", {"generated_by": "porter/P0-env-extract",
                               "reviewed": False})
    (ws / "runner.json").write_text(
        json.dumps(runner, ensure_ascii=False, indent=2), encoding="utf-8")
    (p0 / "reports").mkdir(exist_ok=True)
    (p0 / "reports" / "T3_development.json").write_text(
        json.dumps({"kind": "development", "results": probes,
                    "hard_gate_pass": True}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    _log.console_line(f"[porter] T3: runner.json 就绪（reviewed=false）+ 探测全绿")
    if parsed.get("missing"):
        # 探测金标准已过：剩余 missing 为非阻塞确认项。
        # 备忘降级（审计 H20/设计 memo 类）：只进独立 memo 文件 + events，
        # 永不写 questions 文件（防覆盖阻塞问题）。
        _log.console_line(f"[porter] T3: ⚠️ 通过，但 agent 声明 {len(parsed['missing'])} 项"
              f"非阻塞不确定项（已记入 P0/reports/memo.md 供有空确认）")
        lines = ["# 非阻塞确认项（探测已全绿，仅备忘）", ""]
        for m in parsed["missing"]:
            lines.append(f"- {m.get('field')}: {m.get('why_hard')}")
        (p0 / "reports" / "memo.md").write_text(
            "\n".join(lines), encoding="utf-8")
        try:
            from ..loop import events as _ev
            _ev.append_event("memo", subject="p0.t3.missing",
                             summary=f"{len(parsed['missing'])} 项非阻塞"
                                     "不确定项（P0/reports/memo.md）")
        except Exception:
            pass
