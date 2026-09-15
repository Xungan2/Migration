"""Acceptance design and execution with real checks and human approval.

Each section is an agent-authored JSON contract plus a check script. Delivery and
verification failures return to the same session. Changes to approved criteria
or scope produce a candidate review before waiting for approval; only approved,
verified criteria can produce formal acceptance success.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..artifacts import locate
from ..common import agent
from ..common import vcs as _vcs
from ..exp import accept_exec as _exec
from ..exp import mono as _mono
from ..exp import accept_gate as _gate
from ..handoff import latest_success, publish_handoff, require_success
from .. import log as _log
from ..workspace import read_json

SKILL_T1 = "EXP-accept-t1"     # Tier1 mono-fact extraction skill
SKILL_INJECT = "EXP-accept-inject"
SKILL_E2E = "EXP-accept-e2e"
SKILL_EXECUTE = "EXP-accept-execute"
GATE_T1 = "exp-accept.t1"
GATE_INJECT = "exp-accept.inject"
GATE_PLAN = "exp-accept.plan"
T1_BUDGET_SEC = 3600
INJECT_BUDGET_SEC = 3600      # Tier 2/3 agent 段总预算缺省（CLI 可覆盖）
E2E_BUDGET_SEC = 3600
EXEC_BUDGET_SEC = 3600        # 执行相位 agent 段总预算缺省

# Tier1 derives criteria from mono execution evidence.
_T1_READY = True

_TIER_TITLES = {"t1": "§1/§2/§3/§4（模块编译/单测/镜像编译/自启动）",
                "inject": "§5/§6（设备注入/简单交互）",
                "e2e": "§7（端到端）"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- 范围守卫 ----------

def _scope_offenders(target_os: Path, baseline: set[str],
                     driver_home_rel: str, touched: list[str]) -> list[str]:
    allowed = [driver_home_rel] + [t for t in touched if t]

    def ok(p: str) -> bool:
        return any(p == a or p.startswith(a.rstrip("/") + "/")
                   for a in allowed if a)

    return sorted(p for p in _mono._git_status(target_os) - baseline
                  if not ok(p))


# ---------- 静态段（run_static 请求的执行体） ----------

def _make_static(ws: Path, target_os: Path, driver_home_rel: str,
                 nums: tuple[int, ...], baseline: set[str],
                 prefix: str, *, allow_target_changes: bool = True) -> dict:
    """探索期静态段：结构校验 + 范围守卫 + 逐节真实 invoke + 归档指针。"""

    def _fn() -> tuple[bool, str]:
        probs = _exec.structural_problems(ws, nums)
        if probs:
            return False, ("节文件对结构问题：\n- " + "\n- ".join(probs)
                           + "\n——先补齐/修好文件再请求执行")
        offenders = (sorted(_mono._git_status(target_os) - baseline)
                     if not allow_target_changes else
                     _scope_offenders(target_os, baseline, driver_home_rel,
                                      _exec.touched_paths(ws, nums)))
        records, _p = _exec.invoke_sections(ws, target_os, driver_home_rel,
                                            nums, prefix)
        lines = [f"外部执行完成：{len(records)} 节（输出已归档，"
                 "自行 tail/grep 以下文件）"]
        for n in sorted(records):
            r = records[n]
            lines.append(f"- §{n}: exit={r['rc']} {r['duration_sec']}s"
                         f" 输出={r['output']}"
                         + ("（兜底超时）" if r["timed_out"] else ""))
            if r["status"] != "pass":
                lines.append("  尾部摘录：\n"
                             + "\n".join("  | " + ln for ln in
                                         _exec._tail_of(r["output"]).splitlines()))
        if offenders:
            lines.append("需要说明的目标树改动：" + "、".join(offenders))
        return not _p, "\n".join(lines)

    return {"describe": "按 acceptance/ 节文件对真实执行并归档证据"
                        "（编译/启动/差分试跑）",
            "fn": _fn}


# ---------- prompt ----------

def _kb_face(ws: Path) -> str:
    """知识库注入面——指针形式（2026-09-12 裁定：prompt 知识注入一律
    路径指针，禁止无依据的 head-N 原文截断；与 goals/report/parking/
    negatives 同模式，agent 自读全文）。缺席省略该域。"""
    rows = []
    for rel, what in (
            ("knowledgebase/build/README.md", "构建域（构建命令与坑史）"),
            ("knowledgebase/boot/README.md",
             "启动域（boot 命令、成功/panic 特征与坑史）")):
        if (ws / rel).exists():
            rows.append(f"- 知识库·{what}，**先读**：`{ws / rel}`")
    return "\n".join(rows) + "\n" if rows else ""


def _boot_face(ws: Path) -> str:
    """机器契约来自 runner.json，解释和历史证据来自 runner.md。"""
    return (f"- 基线机器契约（命令、超时、成功/panic 特征——**先读**）："
            f"`{ws / 'runner.json'}`\n"
            f"- 基线启动/编译解释与证据（坑史——**先读**）：`{ws / 'runner.md'}`（三部分手册："
            "镜像编译/设备自启动/设备注入与交互/单元测试+执行记录）\n"
            f"- 执行证据备查（mono gate 实跑输出）："
            f"`{ws / 'exp-mono' / 'logs'}/`\n")


def _mono_knowledge_face(ws: Path) -> str:
    """mono 知识面指针（G4 接入，2026-09-12）：契约登记表 + API 映射
    词典。指针形式（同 session 裁定：禁 head-N 截断）。两文件为 mono
    时代快照、可能落后于树内代码——措辞显式"疑则以树内代码为准"，
    防过时条目误导（这是注入契约知识的固有风险，与不注入的归因效率
    损失相权衡后裁定接入）。缺席省略。"""
    rows = []
    c = ws / "exp-mono" / "contracts.md"
    if c.exists():
        rows.append(f"- 契约登记表（跨模块共享签名与行为纪律——改码前"
                    f"**必查**；系 mono 快照，疑则以树内代码为准）：`{c}`")
    m = ws / "exp-mono" / "mapping-notes.md"
    if m.exists():
        rows.append(f"- API 映射词典（Linux→目标 OS 映射参考，同上以树"
                    f"内代码为准）：`{m}`")
    return "\n".join(rows) + "\n" if rows else ""


def _mono_handoff_face(ws: Path) -> str:
    """Point accept agents at the exact successful mono boundary and record."""
    item = latest_success(ws, "mono")
    if not item:
        return ""
    return (f"- mono 成功 handoff（本阶段事实边界，**先读**）："
            f"`{item.get('handoff')}`\n"
            f"- mono execution record：`{item.get('record')}`\n")


def _t1_prompt(ws: Path, proj: dict,
               manifest: dict, extra: str) -> str:
    skill = agent.load_skill(SKILL_T1)
    acc = _exec.acceptance_dir(ws)
    return (f"{skill}\n\n---\n\n## 背景数据（mono 终态与证据）\n"
            f"- 目标 OS 源码树：`{proj.get('target_os')}`\n"
            f"- driver_home：`{manifest.get('driver_home')}`\n"
            f"- mono ledger：`{ws / 'exp-mono' / 'ledger.json'}`\n"
            f"- runner machine contract：`{ws / 'runner.json'}`\n"
            f"- runner manual/evidence：`{ws / 'runner.md'}`\n"
            f"- mono logs：`{ws / 'exp-mono' / 'logs'}`\n"
            f"- 输出目录：`{acc}`\n" + _mono_knowledge_face(ws) + (extra or "") +
            _mono_handoff_face(ws) +
            "\n先读取上述文件，从已执行且 pass 的事实提取 §1-§4；补充验证与必要修复须留证据并交评审。"
            "每节写 JSON + .check.py 成对文件，并最终输出 done JSON。")


def _inject_prompt(ws: Path, proj: dict,
                   manifest: dict, extra: str) -> str:
    skill = agent.load_skill(SKILL_INJECT)
    acc = _exec.acceptance_dir(ws)
    return (f"{skill}\n\n---\n\n## 背景数据（平台事实）\n"
            f"- 目标 OS 源码树（你的工作目录）：`{proj.get('target_os')}`\n"
            f"- 驱动目录 driver_home：`{manifest.get('driver_home')}`\n"
            f"- runner machine contract：`{ws / 'runner.json'}`\n"
            f"- 迁移终态：exp-mono 报告 `{ws / 'exp-mono' / 'report.md'}`；"
            f"泊车 `{ws / 'exp-mono' / 'parking.md'}`；"
            f"负结论（已排除死路）`{ws / 'exp-mono' / 'negatives.md'}`"
            "（三者先读）\n"
            f"- 迁移意图：`{ws / 'goals.md'}`\n"
            + _boot_face(ws)
            + _kb_face(ws)
            + _mono_knowledge_face(ws)
            + _mono_handoff_face(ws)
            + (extra or "")
            + f"\n## 输出契约\n- 交付物：`{acc}` 下两对文件——"
              "`5-<slug>.json`、`6-<slug>.json` 及各自的 `.check.py`"
              "（§1-§4 由 Tier1 提取，不在你的任务内）。每对的 JSON 须含"
              "按序命令+成功标准，契约见 SKILL。\n"
              "- done JSON：`status` / `deliverable`（= acceptance 目录"
              "绝对路径）/ `notes`。")


def _e2e_prompt(ws: Path, proj: dict,
                manifest: dict, extra: str) -> str:
    skill = agent.load_skill(SKILL_E2E)
    acc = _exec.acceptance_dir(ws)
    inj5 = _exec.pair_json(ws, 5)
    inj6 = _exec.pair_json(ws, 6)
    return (f"{skill}\n\n---\n\n## 背景数据（平台事实）\n"
            f"- 目标 OS 源码树（你的工作目录）：`{proj.get('target_os')}`\n"
            f"- 驱动目录 driver_home：`{manifest.get('driver_home')}`\n"
            f"- runner machine contract：`{ws / 'runner.json'}`\n"
            f"- **已批注入/交互方案（§5/§6 节文件，事实基线，先读）**："
              f"`{inj5}`、`{inj6}`\n"
            f"- 迁移意图：`{ws / 'goals.md'}`；exp-mono 报告 "
            f"`{ws / 'exp-mono' / 'report.md'}`\n"
            + _boot_face(ws)
            + _kb_face(ws)
            + _mono_knowledge_face(ws)
            + _mono_handoff_face(ws)
            + (extra or "")
            + f"\n## 输出契约\n- 交付物：`{acc}` 下 `7-<slug>.json` + "
              "`7-<slug>.check.py` 一对。JSON 须含按序命令+成功标准，"
              "契约见 SKILL。\n"
              "- done JSON：`status` / `deliverable`（= acceptance 目录"
              "绝对路径）/ `notes`。")


# ---------- 评审摘要 ----------

def _write_review(ws: Path, exp_dir: Path, tier: str, nums: tuple[int, ...],
                  records: dict, entry: dict) -> None:
    """关口评审材料：各节 JSON+脚本全文 + invoke 记录 + commit/范围。"""
    lines = [f"# accept {tier} 评审摘要", "",
             f"- 生成时间：{entry.get('time')}",
             f"- 节范围：{_TIER_TITLES[tier]}",
             f"- 验证：{len(records)} 节真实 invoke 全绿（exit=0）",
             f"- 验收期代码改动 commit：{entry.get('commits') or '（无）'}",
             f"- 范围外变更（人判）：{entry.get('offenders') or '（无）'}",
             ""]
    for n in nums:
        j = _exec.pair_json(ws, n)
        if j is None:
            continue
        c = j.with_name(j.stem + ".check.py")
        r = records.get(n) or {}
        lines += [f"## §{n}（{j.stem}）", "",
                  f"- invoke：exit={r.get('rc')}，输出归档 "
                  f"`{r.get('output')}`（{r.get('duration_sec')}s）", "",
                  "### 标准（JSON）", "", "```json",
                  j.read_text(encoding="utf-8", errors="replace").rstrip(),
                  "```", "", "### 消费脚本（check.py）", "", "```python",
                  c.read_text(encoding="utf-8", errors="replace").rstrip(),
                  "```", ""]
    gate_id = _GATE_OF[tier]
    lines += ["## 放行方式", "",
              "answers.md 追加：", "", "```", f"## @{gate_id}",
              "verdict: approve", "```", "",
              "reject 时附 `note:` 行——意见会注入下一轮重做"
              "（`--tier " + tier + "`）。"]
    (exp_dir / f"{tier}-review.md").write_text("\n".join(lines) + "\n",
                                               encoding="utf-8")


# ---------- 关卡执行（一段 tier 的完整编排） ----------

_GATE_OF = {"t1": GATE_T1, "inject": GATE_INJECT, "e2e": GATE_PLAN}
_PREFIX_OF = {"t1": "T1", "inject": "T2", "e2e": "T3"}


def _prompt_fn(tier: str):
    if tier == "t1":
        return _t1_prompt
    return _inject_prompt if tier == "inject" else _e2e_prompt


def _run_tier(ws: Path, exp_dir: Path, proj: dict,
              manifest: dict, ledger: dict, tier: str, budget: int,
              session: str | None, extra: str) -> int:
    """tier ∈ t1|inject|e2e。返回 3=关口待答（已登记）/1=失败或泊车/2=缺 agent。"""
    import os
    if os.environ.get("PORTER_NO_AGENT"):
        _log.console_line("[porter] accept: 需要 agent（PORTER_NO_AGENT=1）"
                          "——rc 2")
        return 2
    target_os = Path(proj["target_os"])
    driver_home_rel = str(manifest["driver_home"])
    nums = _exec.SECTION_TIER[tier]
    prefix = _PREFIX_OF[tier]
    gate_id = _GATE_OF[tier]
    prompt_fn = _prompt_fn(tier)
    baseline = _mono._git_status(target_os)
    static = _make_static(ws, target_os, driver_home_rel, nums,
                          baseline, prefix,
                          allow_target_changes=tier != "t1")
    prompt = prompt_fn(ws, proj, manifest, extra)
    session_id = session
    attempts = 0
    problems: list[str] = []
    outcome: dict = {}
    records = {}

    def validate(_parsed):
        nonlocal problems, attempts, records
        attempts += 1
        problems = _exec.structural_problems(ws, nums)
        if not problems:
            records, problems = _exec.invoke_sections(ws, target_os, driver_home_rel, nums,
                                                       f"{prefix}FV{attempts}")
        return not problems, "\n".join(problems)

    outcome = agent.run_agent_seq(
        prompt, workdir=target_os, log_stem=str(exp_dir / "logs" / prefix),
        static=static, complete_check=validate,
        gen_schema={"status": "str", "deliverable": "str", "notes": "str"},
        agent_budget_sec=int(budget), resume_session=session_id,
        handoff_inputs=(ws, ("mono",)) if latest_success(ws, "mono") else None,
        task={"phase": "accept", "step": tier, "task_id": f"accept.{tier}"})
    session_id = outcome.get("session_id") or session_id
    parsed = outcome.get("parsed") or {}
    blocked = parsed.get("status") == "blocked"
    ok = (not problems and outcome.get("status") == "done" and not blocked)
    entry = {"status": "pass" if ok else ("blocked" if blocked else "invalid"),
             "session_id": session_id,
             "seq_status": outcome.get("status"),
             "agent_sec": outcome.get("total_agent_sec"),
             "validate_attempts": attempts,
             "problems": problems if not ok else [],
             "notes": str(parsed.get("notes", ""))[:400],
             "time": _now()}
    ledger.setdefault("tiers", {})[tier] = entry
    _save_ledger(exp_dir, ledger)
    if not ok:
        if blocked:
            _log.console_line(f"[porter] accept: {tier} 被报 blocked："
                              f"{entry['notes']}——停车 rc 1")
        else:
            _log.console_line(f"[porter] accept: {tier} 未通过"
                              f"（{problems[:2] or outcome.get('status')}）"
                              f"——rc 1；session={session_id}，可 --session "
                              "续接或 --tier 重跑")
        return 1

    entry["verify"] = {str(n): {"rc": r["rc"], "status": r["status"],
                                "output": r["output"],
                                "duration_sec": r["duration_sec"]}
                       for n, r in records.items()}
    # 验证绿 → 目标树改动 commit（agent 的代码/脚手架落地留痕）
    try:
        touched = _exec.touched_paths(ws, nums)
        changed = sorted(_mono._git_status(target_os) - baseline)
        offenders = _scope_offenders(target_os, baseline, driver_home_rel,
                                     touched)
        allowed_changed = ([] if tier == "t1" else
                           [p for p in changed
                            if p == driver_home_rel
                            or p.startswith(driver_home_rel.rstrip("/") + "/")
                            or p in touched])
        commits = ([] if tier == "t1" else
                   _vcs.commit_target(
                       ws, f"accept({tier}): verified — "
                       f"{len(records)} sections green",
                       paths=allowed_changed or None, phase="accept") or [])
        entry["commits"] = commits
        entry["offenders"] = offenders
    except Exception as ex:                     # commit 失败不阻断关口
        entry["commit_error"] = repr(ex)
    _save_ledger(exp_dir, ledger)
    _write_review(ws, exp_dir, tier, nums, records, entry)
    arts = []
    for n in nums:
        j = _exec.pair_json(ws, n)
        if j is not None:
            arts += [j, j.with_name(j.stem + ".check.py")]
    _gate.register_gate(
        ws, gate_id,
        question=(f"验收标准 {_TIER_TITLES[tier]} 审批：逐节过目评审摘要"
                  "——JSON 标准、消费脚本、invoke 验证记录、代码改动 "
                  "commit。批准绑定全部节文件对联合指纹。"),
        context_files=[str((exp_dir / f"{tier}-review.md").relative_to(ws))]
        + [str(a.relative_to(ws)) for a in arts],
        artifact_path=arts)
    _log.console_line(f"[porter] accept: {tier} 节文件就绪（{len(records)} "
                      f"节 invoke 全绿，commit "
                      f"{len(entry.get('commits') or [])}）→ 评审摘要 "
                      f"{exp_dir / (tier + '-review.md')}——人审放行"
                      "（exit 3）")
    return 3


def _save_ledger(exp_dir: Path, ledger: dict) -> None:
    ledger["time"] = _now()
    (exp_dir / "ledger.json").write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


# ---------- 执行相位（--execute：agent session 循环 + 机器阶梯） ----------

def _record_execute(exp_dir: Path, ledger: dict, records: dict,
                    tag: str) -> None:
    """阶梯记录写入 ledger（逐节最新实况；静态段/终验共用）。"""
    ex = ledger.setdefault("execute", {})
    secs = ex.setdefault("sections", {})
    for n, r in records.items():
        secs[str(n)] = {"status": r.get("status"), "rc": r.get("rc"),
                        "output": r.get("output") or "",
                        "duration_sec": r.get("duration_sec"),
                        "tag": tag, "time": _now()}
    _save_ledger(exp_dir, ledger)


def _ladder_lines(records: dict) -> list[str]:
    lines = []
    for n in sorted(records):
        r = records[n]
        if r.get("status") == "skipped":
            lines.append(f"- §{n}: 未跑（{r.get('reason')}）")
            continue
        lines.append(f"- §{n}: exit={r.get('rc')} "
                     f"{r.get('duration_sec', 0)}s 输出={r.get('output')}"
                     + ("（兜底超时）" if r.get("timed_out") else ""))
        if r.get("status") != "pass":
            tail = _exec._tail_of(r.get("output") or "")
            if tail:
                lines.append("  尾部摘录：\n"
                             + "\n".join("  | " + ln
                                         for ln in tail.splitlines()))
    return lines


def _make_exec_static(ws: Path, exp_dir: Path, target_os: Path,
                      driver_home_rel: str, ledger: dict,
                      baseline: set[str]) -> dict:
    """Execute current candidates and return the real ladder verdict."""
    import itertools
    counter = itertools.count(1)

    def _fn() -> tuple[bool, str]:
        tag = f"EXEC{next(counter)}"
        records, ladder_probs = _exec.invoke_ladder(ws, target_os,
                                                    driver_home_rel, tag)
        _record_execute(exp_dir, ledger, records, tag)
        lines = [f"阶梯执行完成（tag={tag}）："] + _ladder_lines(records)
        if ladder_probs:
            lines.append("阶梯未全绿——按归因修复后再次请求执行。")
        else:
            lines.append("阶梯全绿——验收可收敛，请输出 done JSON。")
        ledger["execute"]["problems"] = ladder_probs
        return not ladder_probs, "\n".join(lines)

    return {"describe": "按序执行验收阶梯（§1→§7，首错即停）并归档证据",
            "fn": _fn}


def _execute_prompt(ws: Path, proj: dict, manifest: dict,
                    records: dict) -> str:
    skill = agent.load_skill(SKILL_EXECUTE)
    first_red = next((n for n in sorted(records)
                      if records[n].get("status") not in ("pass",
                                                          "skipped")),
                     None)
    head = (f"当前首红节：§{first_red}（其前各节本轮已过）"
            if first_red else "当前无红节（复验）")
    return (f"{skill}\n\n---\n\n## 背景数据（平台事实）\n"
            f"- 目标 OS 源码树（你的工作目录）：`{proj.get('target_os')}`\n"
            f"- 驱动目录 driver_home：`{manifest.get('driver_home')}`\n"
            f"- runner machine contract：`{ws / 'runner.json'}`\n"
            f"- 验收标准（修订后须重新审批）：`{_exec.acceptance_dir(ws)}`"
            "（七节 JSON+check.py 对；判定语义自读各节文件）\n"
            f"- {head}\n"
            f"- 阶梯实况（基线/最近一轮，输出已归档自行读）：\n"
            + "\n".join("  " + ln for ln in _ladder_lines(records)) + "\n"
            f"- 迁移终态：exp-mono 报告 `{ws / 'exp-mono' / 'report.md'}`；"
            f"泊车 `{ws / 'exp-mono' / 'parking.md'}`；"
            f"负结论 `{ws / 'exp-mono' / 'negatives.md'}`\n"
            f"- 迁移意图：`{ws / 'goals.md'}`\n"
            + _mono_knowledge_face(ws)
            + _mono_handoff_face(ws)
            + f"- 修复知识记录：修完在 `{ws / 'exp-accept' / 'fixes.md'}` "
              "追加一节（归因/改了什么/为何预期转绿/死路教训，见 SKILL"
              " 纪律；供后续沉淀 knowledgebase）\n"
            + f"\n## 输出契约\n- 修复完成后输出 done JSON：`status` / "
              "`notes`（≤200 字：归因、改了什么、为何预期转绿）。\n"
              "- 需要修改标准或批准范围时，完成候选修订，并在 notes 说明理由、"
              "影响及验证结果，输出 done 交付评审。候选验证不代表正式通过。\n"
              "- 无法继续时输出 status: blocked，说明证据和缺失条件；"
              "可选 reason_kind: criteria-defect/platform-gap，分类不依赖措辞。")


def _write_run_report(ws: Path, exp_dir: Path, ledger: dict) -> None:
    """执行相位 run report（机器渲染，判定事实源=invoke 记录）。"""
    ex = ledger.get("execute") or {}
    secs = ex.get("sections") or {}
    lines = ["# accept 执行相位 run report", "",
             f"- 终态：`{ex.get('status')}`（{ex.get('time', '')}）",
             f"- session：`{ex.get('session_id')}`；agent 段时长 "
             f"{ex.get('agent_sec')}s",
             f"- 代码改动 commit：{ex.get('commits') or '（无）'}",
             f"- 范围外变更（人判）：{ex.get('offenders') or '（无）'}",
             "", "## 阶梯实况（各节最新 invoke 记录）", ""]
    for n in _exec.ALL_SECTIONS:
        r = secs.get(str(n)) or {}
        if not r:
            lines.append(f"- §{n}:（无记录）")
            continue
        if r.get("status") == "skipped":
            lines.append(f"- §{n}: 未跑（{r.get('reason')}）")
        else:
            lines.append(f"- §{n}: **{r.get('status')}** exit="
                         f"{r.get('rc')}（tag={r.get('tag')}）输出归档 "
                         f"`{r.get('output')}`")
    if ex.get("problems"):
        lines += ["", "## 未决问题", ""]
        lines += [f"- {p}" for p in ex["problems"][:8]]
    (exp_dir / "run-report.md").write_text("\n".join(lines) + "\n",
                                           encoding="utf-8")


def _publish_execute_handoff(ws: Path, exp_dir: Path) -> bool:
    """Publish the optional execution boundary after a verified 7/7 run."""
    if latest_success(ws, "accept") is None:
        return True
    try:
        publish_handoff(
            ws, "accept.execute",
            summary="Acceptance execution verified all seven sections.",
            artifacts=[exp_dir / "run-report.md"],
            verification=["execution status=pass", "seven-section ladder green"],
            dependencies=("accept",),
            materials=tuple(p for p in (ws / "exp-mono" / "ledger.json",
                                         ws / "runner.md", ws / "runner.json")
                           if p.exists()),
        )
    except (OSError, ValueError) as exc:
        _log.console_line(f"[porter] accept: execute handoff failed: {exc}——rc 1")
        return False
    return True


def _write_panic(exp_dir: Path, ledger: dict, notes: str,
                 kind: str = "standard") -> None:
    """agent 上报争议升级报告（人工介入入口）。kind ∈ standard|platform。

    - standard（criteria-defect）：agent 裁定验收标准本身有错；
    - platform（platform-gap）：agent 裁定断点在目标 OS 侧、且在可改
      范围（driver_home ∪ paths）之外——平台缺口，同样须人工裁决。
    """
    ex = ledger.get("execute") or {}
    secs = ex.get("sections") or {}
    title = {"standard": "标准争议，人工介入",
             "platform": "平台缺口，人工介入"}.get(kind, "争议，人工介入")
    lines = [f"# accept 执行相位 PANIC —— {title}", ""]
    if kind == "standard":
        lines += ["> agent 裁定验收标准本身有错（criteria-defect）。标准在"
                  "修订需形成候选文件、理由和验证材料后交人工审批。", ""]
    else:
        lines += ["> agent 裁定断点在目标 OS 侧且在其可改范围"
                  "（driver_home ∪ 各节 paths）之外（platform-gap）。"
                  "agent 报告尚缺必要条件；已完成诊断及可行修复，待补齐条件后续接。", ""]
    lines += ["## 阶梯实况（各节最新 invoke 记录）", ""]
    for n in _exec.ALL_SECTIONS:
        r = secs.get(str(n)) or {}
        if r:
            lines.append(f"- §{n}: {r.get('status')} exit={r.get('rc')}"
                         f" 输出={r.get('output')}")
    lines += ["", "## agent 论证（" +
              ("criteria-defect" if kind == "standard" else "platform-gap")
              + "）", "", "```",
              notes.strip() or "（空）", "```", ""]
    if kind == "standard":
        lines += ["## 人工选项", "",
                  "1. **认同**：修订标准——`porter accept --tier "
                  "<inject|e2e>` 重做涉事节（reject 意见可写进关口），"
                  "关口重审放行后重新 `--execute`；",
                  "2. **否决**：`porter accept --execute --session "
                  f"{ex.get('session_id') or '<session-id>'}` 续接，令其"
                  "按归因继续修复（最新阶梯实况会随 prompt 重注入）。"]
    else:
        lines += ["## 人工选项", "",
                  "1. **认同缺口**，人工处置（三选）：",
                  "   - 平台侧另行修复（driver_home 之外的目标树改动），"
                  "完成后重新 `--execute`；",
                  "   - 调整验收标准（`--tier` 重做涉事节 + 关口重审，或"
                  "手工修订节文件对后重新登记索引）；",
                  "   - 扩大允许改动范围（修订涉事节 JSON 顶层 paths 后"
                  "重走人审）。",
                  "2. **否决**：`porter accept --execute --session "
                  f"{ex.get('session_id') or '<session-id>'}` 续接，令其"
                  "按归因继续修复（最新阶梯实况会随 prompt 重注入）。"]
    (exp_dir / "execute-panic.md").write_text("\n".join(lines) + "\n",
                                               encoding="utf-8")


def _run_execute(ws: Path, exp_dir: Path, proj: dict, manifest: dict,
                 ledger: dict, budget: int,
                 session: str | None) -> int:
    """执行相位：基线阶梯 → agent session 循环（静态段=阶梯）→ 终验。

    终态 rc：0=pass / 1=interrupted|parked|panic|unverified|failed /
    2=前置（索引缺失、缺 agent）；3=候选修订待审批。
    """
    import os
    if os.environ.get("PORTER_NO_AGENT"):
        _log.console_line("[porter] accept: 需要 agent（PORTER_NO_AGENT=1）"
                          "——rc 2")
        return 2
    target_os = Path(proj["target_os"])
    driver_home_rel = str(manifest["driver_home"])
    idx = ledger.get("acceptance") or {}
    if (not idx.get("sections") or any(s.get("status") != "bound" for s in idx["sections"])
            or {s.get("section") for s in idx["sections"]} != set(range(1, 8))):
        _log.console_line("[porter] accept: 七节索引缺失——先完成标准制定"
                          "（双关口放行后索引自动登记）rc 2")
        return 2
    ex = ledger.setdefault("execute", {})
    revision = ex.get("revision")
    revision_note = ""
    rejected_tiers = []
    if revision:
        states = {tier: _gate.gate_state(ws, _GATE_OF[tier]) for tier in revision["tiers"]}
        candidate_unchanged = revision.get("fingerprint") == _gate.combined_sha16(
            [Path(p) for p in revision.get("artifacts", [])])
        if candidate_unchanged and all(state == "approved" for state, _ in states.values()):
            ex["approved_paths"] = revision.get("paths", [])
            _register_index(ws, exp_dir, ledger)
            ex.setdefault("revision_history", []).append(revision)
            ex.pop("revision", None)
        elif any(state == "rejected" for state, _ in states.values()):
            revision_note = "\n## 修订审批意见\n" + json.dumps(states, ensure_ascii=False)
        else:
            # Rebuild review if the submitted candidate changed while waiting.
            if revision.get("fingerprint") == _gate.combined_sha16(
                    [Path(p) for p in revision.get("artifacts", [])]):
                return 3
            revision_note = "\n待审批候选再次变化，请更新修订说明和验证。"
    if not ex.get("revision"):
        for gate in _gate._load(ws).get("gates", []):
            if gate.get("id") in (GATE_T1, GATE_INJECT, GATE_PLAN):
                state, note = _gate.gate_state(ws, gate["id"])
                if state == "rejected":
                    revision_note += f"\n{gate['id']} 审批意见：{note}"
                    rejected_tiers.extend(tier for tier, gid in _GATE_OF.items() if gid == gate["id"])
                elif state != "approved" and not _exec.fingerprint_problems(ws, ledger.get("acceptance") or {}):
                    return 3
    fprobs = _exec.fingerprint_problems(ws, ledger.get("acceptance") or {})
    ex["status"] = "running"
    _save_ledger(exp_dir, ledger)
    baseline = set(ex.setdefault("baseline", sorted(_mono._git_status(target_os))))
    session = session or ex.get("session_id")

    # 基线阶梯（零 agent 入口：全绿即验收复验通过）
    records0, ladder0 = _exec.invoke_ladder(ws, target_os, driver_home_rel,
                                            "EXEC0")
    _record_execute(exp_dir, ledger, records0, "EXEC0")
    upstream_changes = (require_success(ws, "mono")["changes"]
                        if latest_success(ws, "mono") else [])
    scope_changes = _scope_offenders(target_os, baseline, driver_home_rel,
                                    _exec.touched_paths(ws, range(1, 8)) + ex.get("approved_paths", []))
    if not ladder0 and not fprobs and not revision_note and not upstream_changes and not scope_changes:
        ex.update(status="pass", zero_agent=True, time=_now(),
                  session_id=None, agent_sec=0)
        _save_ledger(exp_dir, ledger)
        _write_run_report(ws, exp_dir, ledger)
        if not _publish_execute_handoff(ws, exp_dir):
            return 1
        _log.console_line("[porter] accept: 基线阶梯全绿（7/7）——验收"
                          "复验通过，零 agent——rc 0")
        return 0
    _log.console_line("[porter] accept: 基线阶梯有红——进入修环循环")

    static = _make_exec_static(ws, exp_dir, target_os, driver_home_rel,
                               ledger, baseline)
    prompt = _execute_prompt(ws, proj, manifest, records0) + revision_note
    if fprobs:
        prompt += "\n标准发生变化，请完成候选修订和影响说明，再交付审批：\n" + "\n".join(fprobs)
    revision_tiers = []
    revision_paths = []

    def validate(parsed):
        nonlocal revision_tiers, revision_paths
        problems = _exec.structural_problems(ws, range(1, 8))
        if problems:
            return False, "\n".join(problems)
        ok, detail = static["fn"]()
        changed = [section["section"] for section in ledger["acceptance"]["sections"]
                   if _exec.fingerprint_problems(ws, {"sections": [section]})]
        revision_paths = _scope_offenders(
            target_os, baseline, driver_home_rel,
            _exec.touched_paths(ws, range(1, 8)) + ex.get("approved_paths", []))
        revision_tiers = [tier for tier, nums in _exec.SECTION_TIER.items()
                          if set(nums) & set(changed) or revision_paths]
        if revision_note and not revision_tiers:
            revision_tiers = list((ex.get("revision") or {}).get("tiers", [])) or rejected_tiers
        if revision_tiers:
            if not str(parsed.get("notes", "")).strip():
                return False, "请补充候选修订理由、影响范围及验证结果，再交付审批。"
            return True, "候选修订已验证，等待人工审批；" + detail
        return ok, detail

    outcome = agent.run_agent_seq(
        prompt, workdir=target_os,
        log_stem=str(exp_dir / "logs" / "EXE"),
        static=static,
        gen_schema={"status": "str", "notes": "str"},
        complete_check=validate,
        handoff_inputs=(ws, ("mono",)) if latest_success(ws, "mono") else None,
        agent_budget_sec=int(budget),
        resume_session=session,
        task={"phase": "accept", "step": "execute", "task_id":
              "accept.execute"})
    sid = outcome.get("session_id") or session
    ex.update(session_id=sid, agent_sec=outcome.get("total_agent_sec"),
              seq_status=outcome.get("status"))
    parsed = outcome.get("parsed") or {}

    if outcome.get("status") != "done":
        st = {"budget-exhausted": "interrupted", "interrupted": "interrupted",
              "stalled": "parked"}.get(outcome.get("status"), "failed")
        ex.update(status=st, time=_now())
        _save_ledger(exp_dir, ledger)
        _write_run_report(ws, exp_dir, ledger)
        hint = (f"--session {sid} 续接" if st == "interrupted"
                else "人工检视后可 --session 续接或调整")
        _log.console_line(f"[porter] accept: 执行相位 {st}"
                          f"（seq={outcome.get('status')}）——{hint} rc 1")
        return 1
    if parsed.get("status") == "blocked":
        notes = str(parsed.get("notes", ""))
        flat = str(parsed.get("reason_kind") or notes).strip().lower()
        if flat.startswith("criteria-defect"):
            ex.update(status="panic", panic_kind="standard",
                      panic_notes=notes[:4000], time=_now())
            _save_ledger(exp_dir, ledger)
            _write_panic(exp_dir, ledger, notes, kind="standard")
            _write_run_report(ws, exp_dir, ledger)
            _log.console_line(f"[porter] accept: 执行相位 PANIC——agent "
                              "裁定标准有错（criteria-defect），升级报告 "
                              f"{exp_dir / 'execute-panic.md'}——人工"
                              "介入 rc 1")
            return 1
        if flat.startswith("platform-gap"):
            ex.update(status="panic", panic_kind="platform",
                      panic_notes=notes[:4000], time=_now())
            _save_ledger(exp_dir, ledger)
            _write_panic(exp_dir, ledger, notes, kind="platform")
            _write_run_report(ws, exp_dir, ledger)
            _log.console_line(f"[porter] accept: 执行相位 PANIC——agent "
                              "裁定平台缺口（platform-gap，可改范围之外），"
                              f"升级报告 {exp_dir / 'execute-panic.md'}——"
                              "人工介入 rc 1")
            return 1
        ex.update(status="parked", blocked_notes=notes[:2000], time=_now())
        _save_ledger(exp_dir, ledger)
        _write_run_report(ws, exp_dir, ledger)
        _log.console_line(f"[porter] accept: 执行相位泊车（blocked："
                          f"{notes[:120]}）——rc 1")
        return 1

    if revision_tiers:
        review = exp_dir / f"execute-revision-{datetime.now():%Y%m%d%H%M%S%f}.md"
        review.write_text("# 验收修订评审\n\n" + str(parsed.get("notes", ""))
                          + "\n\n## 影响与验证\n\n"
                          + json.dumps({"tiers": revision_tiers, "paths": revision_paths,
                                        "changes": _exec.fingerprint_problems(ws, ledger["acceptance"]),
                                        "sections": ex.get("sections", {})},
                                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        artifacts = [review]
        for tier in revision_tiers:
            arts = []
            for n in _exec.SECTION_TIER[tier]:
                j = _exec.pair_json(ws, n)
                arts.extend([j, j.with_name(j.stem + ".check.py")])
            artifacts.extend(arts)
            _gate.register_gate(ws, _GATE_OF[tier],
                                question="验收修订审批：审阅理由、影响及候选验证后批准当前版本。",
                                context_files=[str(review.relative_to(ws))],
                                artifact_path=arts + [review])
            ledger.setdefault("tiers", {}).setdefault(tier, {}).update(status="pending-approval", verify={})
        ex.update(status="pending-approval", revision={
            "tiers": revision_tiers, "paths": sorted(set(ex.get("approved_paths", []) + revision_paths)),
            "artifacts": [str(p) for p in artifacts],
            "fingerprint": _gate.combined_sha16(artifacts), "review": str(review)})
        _save_ledger(exp_dir, ledger)
        _write_run_report(ws, exp_dir, ledger)
        return 3

    # 全绿 → commit + 报告
    try:
        touched = _exec.touched_paths(ws, range(1, 8))
        changed = sorted(_mono._git_status(target_os) - baseline)
        offenders = _scope_offenders(target_os, baseline, driver_home_rel,
                                     touched)
        allowed = [p for p in changed
                   if p == driver_home_rel
                   or p.startswith(driver_home_rel.rstrip("/") + "/")
                   or p in touched]
        commits = _vcs.commit_target(
            ws, "accept(execute): ladder 7/7 green",
            paths=allowed or None, phase="accept") or []
        ex.update(commits=commits, offenders=offenders)
    except Exception as exn:                     # commit 失败不阻断终态
        ex["commit_error"] = repr(exn)
    ex.update(status="pass", time=_now())
    _save_ledger(exp_dir, ledger)
    _write_run_report(ws, exp_dir, ledger)
    if not _publish_execute_handoff(ws, exp_dir):
        return 1
    _log.console_line("[porter] accept: 执行相位 PASS——阶梯 7/7 全绿，"
                      f"报告 {exp_dir / 'run-report.md'}——rc 0")
    return 0


# ---------- 七节索引（双批后） ----------

def _register_index(ws: Path, exp_dir: Path, ledger: dict) -> dict:
    d = _exec.acceptance_dir(ws)
    sections = []
    for n in _exec.ALL_SECTIONS:
        ms = sorted(d.glob(f"{n}-*.json"))
        if len(ms) != 1:
            sections.append({"section": n, "status": "missing"})
            continue
        j = ms[0]
        c = j.with_name(j.stem + ".check.py")
        sections.append({"section": n, "status": "bound",
                         "json": str(j.relative_to(ws)),
                         "check": str(c.relative_to(ws)),
                         "sha16": _gate.sha16_file(j),
                         "check_sha16": _gate.sha16_file(c)})
    idx = {"sections": sections, "time": _now()}
    ledger["acceptance"] = idx
    _save_ledger(exp_dir, ledger)
    return idx


def _tier_rerun_needed(ws: Path, ledger: dict, tier: str) -> bool:
    nums = _exec.SECTION_TIER[tier]
    if _exec.structural_problems(ws, nums):
        return True
    return (ledger.get("tiers", {}).get(tier, {})
            .get("status") in ("invalid", "unverified"))


# ---------- 主入口 ----------

def run_accept(ws: Path, tier: str | None = None,
               budget: int | None = None,
               session: str | None = None,
               execute: bool = False) -> int:
    ws = Path(ws).resolve()
    legacy_workspace = not (ws / "exp-mono" / "report.md").exists()
    mono_handoff = latest_success(ws, "mono")
    mono_task_dir = ws / "handoffs" / "tasks" / "mono"
    # Real mono runs always leave report.md (and the task directory).  Keep
    # the tiny historical fixtures usable when neither exists; they predate
    # the phase handoff and are covered by the legacy compatibility tests.
    if (mono_handoff is not None or mono_task_dir.exists()
            or not legacy_workspace):
        try:
            require_success(ws, "mono")
        except ValueError as exc:
            _log.console_line(f"[porter] accept: mono handoff 无效：{exc}——rc 2")
            return 2
    # Historical workspaces without mono/report are kept usable; every current
    # mono run creates report.md and therefore takes the strict path above.
    mono_ready = mono_handoff is not None
    def read_inputs():
        paths = locate(ws, required=("module-division.json",), record=False)
        manifest = read_json(paths["module-division.json"])
        proj = read_json(ws / "project.json")
        target = proj.get("target_os")
        home = manifest.get("driver_home")
        if not isinstance(target, str) or not Path(target).is_dir():
            raise ValueError("project.json requires an accessible target_os directory")
        if not isinstance(home, str) or not home or not (Path(target) / home).resolve().is_relative_to(Path(target).resolve()):
            raise ValueError("module-division.json requires driver_home inside target_os")
        return proj, manifest, read_json(ws / "exp-mono/ledger.json")

    try:
        if mono_ready:
            proj, manifest, mono_ledger = agent.repair_inputs(
                read_inputs, ws, str(ws / "exp-accept/logs/inputs"),
                budget=budget if budget is not None else EXEC_BUDGET_SEC,
                handoff_inputs=(ws, ("mono",)),
                task={"phase": "accept", "step": "inputs", "task_id": "accept.inputs"})
        else:
            proj, manifest, mono_ledger = read_inputs()
    except (ValueError, FileNotFoundError) as exc:
        _log.console_line(f"[porter] accept: {exc}")
        return 1 if mono_ready else 2
    mods = mono_ledger.get("modules") or {}
    if not mods or not all((e or {}).get("status") == "pass"
                           for e in mods.values()):
        _log.console_line("[porter] accept: exp-mono 未全 pass——先完成迁移"
                          "（本流程只接终态）rc 2")
        return 2
    exp_dir = ws / "exp-accept"
    (exp_dir / "acceptance").mkdir(parents=True, exist_ok=True)
    (exp_dir / "logs").mkdir(parents=True, exist_ok=True)
    ledger = _mono._read_json(exp_dir / "ledger.json") or {}

    # ---- 执行相位（--execute）----
    if execute:
        if tier:
            _log.console_line("[porter] accept: --execute 与 --tier 互斥"
                              "——rc 2")
            return 2
        return _run_execute(ws, exp_dir, proj, manifest, ledger,
                            int(budget or EXEC_BUDGET_SEC), session)


    # ---- Tier 1（§1-§4：自 mono 执行事实提取）----
    if not _T1_READY or not mono_ready:
        if tier == "t1":
            reason = ("Tier1 未启用" if not _T1_READY else
                      "缺少 mono 成功 handoff")
            _log.console_line(f"[porter] accept: {reason}——rc 2")
            return 2
        reason = ("Tier1 未启用" if not _T1_READY else
                  "缺少 mono 成功 handoff")
        _log.console_line(f"[porter] accept: {reason}——§1-§4 留空跳过"
                          "（索引将记 missing；--execute 需七节齐备）")
    elif tier == "t1" or (not tier and _tier_rerun_needed(ws, ledger, "t1")):
        note = ""
        if _exec.pair_json(ws, 1) is not None:
            rnote = _gate.reject_note(ws, GATE_T1)
            if rnote:
                note = f"\n## 关口① reject 意见（重做依据）\n{rnote}\n"
        rc = _run_tier(ws, exp_dir, proj, manifest, ledger,
                       "t1", int(budget or T1_BUDGET_SEC), session, note)
        session = None                     # 会话只续接被指名的 tier
        if rc != 3:
            return rc
        st0, _note0 = _gate.gate_state(ws, GATE_T1)
        if st0 == "rejected":
            _log.console_line("[porter] accept: 关口① reject（note 见 "
                              "GATES.md）——重跑 `--tier t1` 载入意见重做"
                              " rc 3")
            return 3
        if st0 != "approved":
            _log.console_line(f"[porter] accept: 关口① {st0}——人审放行后"
                              "重跑（answers.md `## @exp-accept.t1` + "
                              "`verdict: approve`）rc 3")
            return 3

    if mono_ready:
        state, note = _gate.gate_state(ws, GATE_T1)
        if state != "approved":
            _log.console_line(f"[porter] accept: Tier1 等待审批：{note}——rc 3")
            return 3

    # ---- Tier 2（§5/§6）----
    if tier == "inject" or (not tier
                            and _tier_rerun_needed(ws, ledger, "inject")):
        note = ""
        if _exec.pair_json(ws, 5) is not None:
            rnote = _gate.reject_note(ws, GATE_INJECT)
            if rnote:
                note = f"\n## 关口② reject 意见（重做依据）\n{rnote}\n"
        rc = _run_tier(ws, exp_dir, proj, manifest, ledger,
                       "inject", int(budget or INJECT_BUDGET_SEC),
                       session, note)
        session = None                     # 会话只续接被指名的 tier
        if rc != 3:
            return rc
    st1, _note1 = _gate.gate_state(ws, GATE_INJECT)
    if st1 == "rejected":
        _log.console_line("[porter] accept: 关口② reject（note 见 GATES.md）"
                          "——重跑 `--tier inject` 载入意见重做 rc 3")
        return 3
    if st1 != "approved":
        _log.console_line(f"[porter] accept: 关口② {st1}——人审放行后重跑"
                          "（answers.md `## @exp-accept.inject` + "
                          "`verdict: approve`）rc 3")
        return 3

    # ---- Tier 3（§7）----
    if tier == "e2e" or (not tier and _tier_rerun_needed(ws, ledger, "e2e")):
        note = ""
        if _exec.pair_json(ws, 7) is not None:
            rnote = _gate.reject_note(ws, GATE_PLAN)
            if rnote:
                note = f"\n## 关口③ reject 意见（重做依据）\n{rnote}\n"
        rc = _run_tier(ws, exp_dir, proj, manifest, ledger,
                       "e2e", int(budget or E2E_BUDGET_SEC),
                       session, note)
        if rc != 3:
            return rc
    st2, _note2 = _gate.gate_state(ws, GATE_PLAN)
    if st2 == "rejected":
        _log.console_line("[porter] accept: 关口③ reject——重跑 "
                          "`--tier e2e` 载入意见重做 rc 3")
        return 3
    if st2 != "approved":
        _log.console_line(f"[porter] accept: 关口③ {st2}——人审放行后重跑"
                          "（answers.md `## @exp-accept.plan` + "
                          "`verdict: approve`）rc 3")
        return 3

    # ---- 三 tier → 七节索引 ----
    idx = _register_index(ws, exp_dir, ledger)
    bound = [s["section"] for s in idx["sections"]
             if s.get("status") == "bound"]
    missing = [s["section"] for s in idx["sections"]
               if s.get("status") != "bound"]
    extra = (f"；缺节 §{','.join(map(str, missing))}"
             "——待 Tier1 agent" if missing else "")
    _log.console_line(f"[porter] accept: 验收标准就绪——七节索引已登记"
                      f"（bound {len(bound)}/7{extra}，见 "
                      "exp-accept/ledger.json）；缺节期间 --execute 不可用"
                      "——rc 0")
    if not missing and mono_ready:
        try:
            publish_handoff(
                ws, "accept",
                summary="Acceptance standards for all seven sections are ready.",
                artifacts=[exp_dir / "acceptance"],
                verification=["seven acceptance section pairs bound",
                              "human gates approved"],
                dependencies=("mono",),
                materials=tuple(p for p in (ws / "exp-mono" / "ledger.json",
                                             ws / "runner.md", ws / "runner.json")
                               if p.exists()),
            )
        except (OSError, ValueError) as exc:
            _log.console_line(f"[porter] accept: accept handoff failed: {exc}——rc 1")
            return 1
    return 0
