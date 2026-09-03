"""errorloop.py — 错误处理模块核心：知识辅助的 agent 求解循环。

设计（§15 重设计定案，2026-09-03）：
  失败 → 证据包（log.query 组装：judge 史/相关事件/快照指针/日志尾）
    → ≤MAX_ROUNDS 轮 agent 求解：
        每轮检索 failures/pitfalls 知识（轮 1 全量目录注入，后续轮
        自主再检索不重注）；参考知识或自行分析 → 动作词表 verdict
        → 编排器确定性执行 → 双信号复验 → 签名比对（同签名连发
        SAME_SIG_REPEAT 次 = 零进展早退）
    → 解决：知识回流候选（candidates → CP5 审）+ 返回 solved
    → 耗尽/早退/escalate/no-agent：升级报告（六字段）+ 返回，
      由挂载点开 unsolved 关口（attempts 在挂载点退役）

动作词表（判定/执行分离：agent 只判定与改码，正本写盘归编排器）：
  fix-code      agent 已在目标树修码（编排器只复验）
  fix-runner    fix.runner_patch 合入 runner.json
  fix-criteria  改判据正则——强制证据（源码 file:line 或日志原文
                对照 quote）+ 决策债登记（阶段末 CP 审计，无人工闸）
  rerun         环境瞬时问题，幂等重跑即愈（复验即重跑）
  rehang        deferred 改挂真实消费者
  park          平台缺口泊车 + platform_patches/defects 登记
  escalate      放弃自动路径

verify 契约：verify() -> (ok: bool, new_failure: dict | None)。
new_failure 携带复验后的失败现场（detail/boot_log/ut_out/build_out）
供签名与下轮 prompt；解决时可为 None。

降级：PORTER_NO_AGENT=1 → 不跑 agent 轮，直接出报告（no-agent）；
self_diagnosis 熔断关 → bypass（挂载点亦拦，双保险）。
"""

from __future__ import annotations

import json
import os
import re
import hashlib
from datetime import datetime
from pathlib import Path

from ..common import agent
from . import events
from ..log import query as lq

MAX_ROUNDS = 3                    # 求解轮硬上限（定案：3）
SAME_SIG_REPEAT = 2               # 同签名连发次数 = 零进展早退
AGENT_TIMEOUT_SEC = 1200          # 单轮 agent 上限（同 --defect-fix 会话）
ACTIONS = ("fix-code", "fix-runner", "fix-criteria", "rerun",
           "rehang", "park", "escalate")

# 需要升级报告的终态（其余为已解决/已处置）
_UNSOLVED_STATUSES = ("unsolved", "early-exit", "escalated", "no-agent")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                 encoding="utf-8")


def _safe_name(subject) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", str(subject or "X"))[:60]


def _ev_str(verdict: dict) -> str:
    evs = [f"{e.get('file')}:{e.get('line')} {e.get('quote', '')[:80]}"
           for e in verdict.get("evidence") or []]
    return "; ".join(evs) or (verdict.get("notes")
                              or verdict.get("summary") or "")


# ---------- 签名（规范化组合哈希；碎改动不翻转） ----------

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _norm_text(text: str, tail_lines: int = 40) -> str:
    """签名规范化：去 ANSI、路径→basename、独立数字→N（标识符/
    错误码内的数字保留）、压空白、去空行。"""
    if not text:
        return ""
    text = _ANSI_RE.sub("", text)
    out = []
    for ln in text.splitlines()[-tail_lines:]:
        ln = re.sub(r"/?(?:[\w.\-]+/)+([\w.\-]+)", r"\1", ln)
        ln = re.sub(r"\d{4}-\d{2}-\d{2}[T ][\d:]+(\.\d+)?", "TS", ln)
        ln = re.sub(r"\b\d{1,2}:\d{2}:\d{2}\b", "TS", ln)
        ln = re.sub(r"(?<![A-Za-z\d])\d+", "N", ln)
        ln = re.sub(r"\s+", " ", ln).strip()
        if ln:
            out.append(ln)
    return "\n".join(out)


def failure_signature(subject, detail, log_text) -> str:
    """组合签名 = (subject, 规范化 detail, 规范化日志尾) 哈希。

    detail 取尾 6 行、日志取尾 40 行——签名变化 = 现场真实变化
    （行号漂移/时间戳/路径前缀/色码等碎改动不翻转）。
    """
    blob = "\x00".join([str(subject or ""),
                        _norm_text(detail or "", 6),
                        _norm_text(log_text or "", 40)])
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()[:12]


def _log_blob(d: dict) -> str:
    for k in ("boot_log", "ut_out", "build_out"):
        if d.get(k):
            return str(d[k])
    return ""


# ---------- 证据包与 prompt ----------

def _fenced(title: str, text: str, lines: int = 40) -> str:
    tail = lq.tail_text(text or "", lines)
    if not tail:
        return ""
    return f"\n### {title}\n```\n{tail}\n```"


def _evidence_block(ws: Path, failure: dict) -> str:
    parts = [f"- 对象：{failure.get('subject')}"
             f"（source={failure.get('source')}）",
             f"- 判据：kind={failure.get('kind')} "
             f"layer={failure.get('layer')} expr={failure.get('expr')}"]
    if failure.get("criterion"):
        parts.append("- criterion："
                     + json.dumps(failure["criterion"],
                                  ensure_ascii=False)[:400])
    if failure.get("defect"):
        parts.append("- defect："
                     + json.dumps(failure["defect"],
                                  ensure_ascii=False)[:600])
    if failure.get("deferred_uncleared"):
        parts.append(f"- deferred 未清偿：{failure['deferred_uncleared']}")
    subj = failure.get("subject")
    try:
        judges = lq.events(ws, kind_prefix="judge", subject=subj,
                           limit=20)
        if judges:
            parts.append("### 双信号判定历史（judge 事件）\n"
                         + "\n".join(f"- {e.get('time')} "
                                     f"{(e.get('summary') or '')[:100]}"
                                     for e in judges))
        evs = lq.events(ws, subject=subj, limit=30)
        if evs:
            parts.append("### 相关事件轨迹\n"
                         + "\n".join(f"- {e.get('time')} {e.get('kind')} "
                                     f"{(e.get('summary') or '')[:80]}"
                                     for e in evs))
    except Exception:
        pass
    if failure.get("snapshot"):
        parts.append(f"### 失败快照\n- 目录：{failure['snapshot']}"
                     "（含 manifest.json 与日志原件）")
    body = (_fenced("失败说明（detail）", failure.get("detail") or "", 10)
            + _fenced("boot 日志尾", failure.get("boot_log") or "")
            + _fenced("unit test 输出尾", failure.get("ut_out") or "")
            + _fenced("build 输出尾", failure.get("build_out") or ""))
    return "## 失败证据包\n" + "\n".join(parts) + "\n" + body


def _kb_hint(ws: Path) -> str:
    """后续轮的知识库目录提示（不重注目录，允许自主再检索）。"""
    try:
        from ..bootstrap import kb as _kb
        kb_dir = _kb.kb_dir_for(ws)
        dirs = []
        for dom in ("failures", "pitfalls"):
            if kb_dir is not None:
                dirs.append(str(_kb.domain_kb(dom, kb_dir).resolve()))
            dirs.append(str(_kb.domain_base(dom).resolve()))
            dirs.append(str(_kb.domain_temp(dom, ws=ws).resolve()))
        return "目录：" + "；".join(dirs)
    except Exception:
        return "目录：（不可用）"


def _round_prompt(ws: Path, round_no: int, failure: dict,
                  prev_ctx: str, kb_face_text: str) -> str:
    skill = agent.load_skill("solve")
    lines = [skill, "", "---", "", _evidence_block(ws, failure)]
    if round_no == 1:
        if kb_face_text:
            lines += ["", "---", "", kb_face_text]
        task = (f"\n## 任务\n第 {round_no}/{MAX_ROUNDS} 轮。先检索知识库，"
                "再判定归责并解决；输出紧凑 JSON verdict（格式见 skill）。")
    else:
        lines += ["", "---", "",
                  "## 上一轮上下文（勿重查已排除路线）", prev_ctx,
                  "", "## 知识库（如需按新假设再检索）", _kb_hint(ws)]
        task = (f"\n## 任务\n第 {round_no}/{MAX_ROUNDS} 轮。总结上轮"
                "成败，换思路继续；输出紧凑 JSON verdict。")
    lines.append(task)
    return "\n".join(lines)


def _prev_context(ws: Path, stem, out: str, verdict: dict | None,
                  verify_fail_text: str,
                  consulted: list[str]) -> str:
    """轮间上下文接续：经 log 子系统取上轮 run 的结局与输出尾。"""
    parts = [f"### 上一轮运行（{stem}）"]
    got_tail = False
    try:
        rs = [r for r in lq.runs(ws, last_n=30)
              if r.get("run_id") == str(stem)]
        if rs:
            r = rs[-1]
            rc = "运行中" if r.get("rc") is None else f"rc={r['rc']}"
            parts.append(f"- 结局：{rc}"
                         + (f"；{(r.get('summary') or '')[:200]}"
                            if r.get("summary") else ""))
            tb = lq.tail_block(ws, r.get("log"), 40, "上轮输出尾部")
            if tb:
                parts.append(tb)
                got_tail = True
    except Exception:
        pass
    if not got_tail:
        tail = lq.tail_text(out or "", 40)
        if tail:
            parts.append(f"- 输出尾 40 行：\n```\n{tail}\n```")
    if verdict:
        parts.append(f"- 上轮判定：action={verdict.get('action')} "
                     f"circuit={verdict.get('circuit')}")
        parts.append(f"- 上轮总结：{(verdict.get('summary') or '')[:400]}")
    if consulted:
        parts.append(f"- 已检索且不匹配的条目：{', '.join(consulted)}"
                     "（勿原样重试）")
    if verify_fail_text:
        parts.append("### 复验后失败现场（新）\n```\n"
                     + lq.tail_text(verify_fail_text, 30) + "\n```")
    return "\n".join(parts)


# ---------- verdict 解析 ----------

def _parse_verdict(out: str) -> dict | None:
    """```json 块中找含合法 action 的 verdict；找不到返回 None。"""
    if not out:
        return None
    blocks = re.findall(r"```json\s*(.*?)```", out, re.DOTALL) \
        or re.findall(r"```\s*(\{.*?\})\s*```", out, re.DOTALL)
    for b in blocks:
        try:
            obj = json.loads(b.strip())
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("action") in ACTIONS:
            return obj
    return None


# ---------- 动作执行（确定性；判定/执行分离） ----------

def _patch_runner(ws: Path, patch: dict) -> list[str]:
    rp = ws / "runner.json"
    runner = _load(rp, {})
    applied = []
    for key, val in patch.items():
        if isinstance(val, dict) and isinstance(runner.get(key), dict):
            runner[key].update(val)
        else:
            runner[key] = val
        applied.append(f"runner.{key} 更新（auto-fixed）")
    _save(rp, runner)
    return applied


def _criteria_evidence_ok(verdict: dict) -> bool:
    """证据门槛：源码 file:line（line>0）或日志原文对照（quote ≥16）。"""
    for e in verdict.get("evidence") or []:
        if e.get("file") and int(e.get("line") or 0) > 0:
            return True
        if len(str(e.get("quote") or "")) >= 16:
            return True
    return False


def _apply_criteria_fix(ws: Path, failure: dict,
                        verdict: dict) -> str:
    fix = verdict.get("fix") or {}
    subject = failure.get("subject")
    if not _criteria_evidence_ok(verdict):
        return ("criteria 修正被拒：缺证据（源码 file:line 或日志原文"
                "对照 quote≥16 字符）——防无证据改量尺")
    target = fix.get("target") or "criteria"
    if target == "close_stale":
        from . import p6 as p6_mod
        did = failure.get("subject")
        try:
            p6_mod.close_defect(
                ws, did,
                root_cause=f"文档过期（假缺陷）：{_ev_str(verdict)[:300]}",
                fix="无代码修复（闭账 stale；solve）",
                regression_evidence="代码实测调用点已在（见 root_cause "
                                    "file:line）")
            return f"defect {did} 闭账 stale（auto-fixed）"
        except ValueError as ex:
            return f"闭账未执行：{ex}"
    if target == "l4":
        cp = ws / "P6" / "reports" / "l4_criteria.json"
    else:
        module = failure.get("module") or str(subject or "X").split(".")[0]
        cp = ws / "P3" / module / "reports" / "criteria.json"
    doc = _load(cp, None)
    if doc is None:
        return f"criteria 修正未执行：缺 {cp}"
    hit = False
    old = None
    for c in doc.get("criteria", []):
        if c.get("id") == subject:
            old = c.get("expr")
            if fix.get("expr"):
                c["expr"] = fix["expr"]
            c["auto_fixed"] = {"time": _now(), "was": old,
                               "evidence": _ev_str(verdict)[:300],
                               "by": "solve"}
            hit = True
            break
    if not hit:
        return f"criteria 条目未找到：{subject}"
    _save(cp, doc)
    _criteria_debt(ws, failure, verdict, old, fix.get("expr"))
    return (f"criteria {subject} 修正（auto-fixed；证据 "
            f"{_ev_str(verdict)[:100]}）")


def _criteria_debt(ws: Path, failure: dict, verdict: dict,
                   old, new) -> None:
    """criteria 修正的审计债（无人工闸；阶段末 CP digest 批审）。"""
    from . import gates as gates_mod
    gid = f"{failure.get('source') or 'solve'}.criteria-fix.{failure.get('subject')}"
    led = gates_mod.GateLedger(ws).load()
    if led.find(gid) is None:
        led.add(id=gid, kind="decision", lane="checkpoint",
                gate_type="decision",
                phase=str(failure.get("source") or "solve").upper(),
                subject=failure.get("subject"), blocking=False,
                question=(f"判据 {failure.get('subject')} 已由求解循环自动"
                          f"修正（expr {old!r} → {new!r}）。阶段末审计：量尺"
                          "修改是否有据（证据见 resolution）。"),
                answer_form=[{"field": "verdict", "type": "enum",
                              "options": ["approve", "reject"],
                              "required": True}])
    led.mark(gid, "applied", answer={"verdict": "auto"},
             answered_by="agent", answered_at=_now(),
             resolution=f"expr {old!r}→{new!r}；证据："
                        f"{_ev_str(verdict)[:200]}")


def _rehang(ws: Path, failure: dict, fix: dict) -> str:
    dp = ws / "deferred.json"
    d = _load(dp, {"entries": []})
    did = fix.get("deferred_id") or failure.get("subject")
    to = fix.get("to") or []
    if not to:
        return "rehang 缺 fix.to——未执行"
    for e in d.get("entries", []):
        if e.get("id") == did:
            e["deferred_by"] = list(to)
            e["history"] = e.get("history") or []
            e["history"].append({"time": _now(), "ok": None,
                                 "detail": f"solve 改挂 {to}"})
            _save(dp, d)
            return f"deferred {did} 改挂 {to}"
    return f"deferred 条目未找到：{did}"


def _register_platform_patch(ws: Path, failure: dict,
                             verdict: dict) -> str:
    pp = ws / "platform_patches.json"
    doc = _load(pp, {"patches": []})
    gap = (verdict.get("fix") or {}).get("gap") \
        or failure.get("subject") or "UNKNOWN-GAP"
    if not any(p.get("gap") == gap for p in doc.get("patches", [])):
        doc["patches"].append({
            "gap": gap, "module": failure.get("module"),
            "status": "proposed", "strategy": "platform-gap",
            "instruction": f"求解循环泊车登记（{_now()}）："
                           f"{verdict.get('summary') or ''}"[:400],
            "evidence": _ev_str(verdict)[:500],
            "registered": _now()})
        _save(pp, doc)
    from . import p6 as p6_mod
    try:
        p6_mod.add_defect(ws, gap,
                          f"平台缺口（求解循环自动登记）：{gap}",
                          _ev_str(verdict)[:300])
    except ValueError:
        pass
    try:
        p6_mod.park_defect(ws, gap, "平台缺口泊车（solve park）——"
                                     "P7 上游补丁素材")
    except ValueError:
        pass
    return f"platform_patches 登记 + defect 泊车：{gap}"


def _apply_action(ws: Path, failure: dict,
                  verdict: dict) -> tuple[list[str], str | None]:
    """执行动作。返回 (applied 说明, 终态提示|None)。

    终态提示 parked/rehung/escalate 时循环立即结束（已处置/转人工）。
    """
    action = verdict.get("action")
    fix = verdict.get("fix") or {}
    applied: list[str] = []
    if action == "fix-runner":
        patch = fix.get("runner_patch")
        if isinstance(patch, dict) and patch:
            applied += _patch_runner(ws, patch)
        else:
            applied.append("fix-runner 缺 runner_patch——未执行")
    elif action == "fix-criteria":
        applied.append(_apply_criteria_fix(ws, failure, verdict))
    elif action == "park":
        applied.append(_register_platform_patch(ws, failure, verdict))
        return applied, "parked"
    elif action == "rehang":
        applied.append(_rehang(ws, failure, fix))
        return applied, "rehung"
    elif action == "escalate":
        return applied, "escalate"
    # fix-code：agent 已在工作目录改码；rerun：直接复验。无正本写盘。
    return applied, None


# ---------- 报告与回流 ----------

def _report(ws: Path, failure: dict, outcome: dict,
            cfg: dict | None) -> tuple[dict | None, str | None]:
    """耗尽终态 → 六字段升级报告（复用 diagnose 的编排器生成）。"""
    try:
        from . import diagnose
        verdicts = [{"circuit": r.get("circuit"), "rule_id": None,
                     "confidence": None, "evidence": [],
                     "signature_candidates":
                         r.get("signature_candidates") or [],
                     "notes": (r.get("summary") or "")[:200]}
                    for r in outcome["rounds"] if r.get("action")]
        rep, _stop = diagnose.generate_escalation_report(
            ws, failure.get("source") or "d1",
            failure.get("subject") or "?",
            symptom=outcome.get("symptom") or failure.get("detail") or "",
            triage_verdicts=verdicts, cfg=cfg)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_",
                      str(failure.get("subject") or "?"))[:60]
        esc = sorted((ws / "escalations").glob(f"{safe}-*.json"),
                     key=lambda p: p.stat().st_mtime)
        return rep, (str(esc[-1].relative_to(ws)) if esc else None)
    except Exception:
        return None, None


def _sediment_case(ws: Path, failure: dict, verdict: dict) -> None:
    """解决案例回流：签名+解法 → kb 候选账（CP5 审核晋升 failures 域）。"""
    try:
        from ..bootstrap import candidates as _cand
        draft = (f"求解循环解决：{failure.get('subject')} → "
                 f"{verdict.get('action')}（{verdict.get('circuit')}）。"
                 f"{(verdict.get('summary') or '')[:200]}")
        _cand.record_candidate(ws, hook="solve-loop",
                               ref=str(failure.get("subject")),
                               draft=draft, evidence=[],
                               suggested="failures")
    except Exception:
        pass


def _end_event(ws: Path, failure: dict, outcome: dict) -> None:
    events.append_event(
        "errorloop_end", subject=failure.get("subject"),
        intent=failure.get("source"),
        summary=(f"{outcome['status']} rounds={len(outcome['rounds'])} "
                 f"actions={[r.get('action') for r in outcome['rounds']]}")
        [:380],
        ws=ws, mount=failure.get("source"))


# ---------- 主入口 ----------

def run_solve_loop(ws: Path, failure: dict, verify,
                   cfg: dict | None = None) -> dict:
    """知识辅助求解循环。返回 outcome：

    {"status": solved|unsolved|early-exit|escalated|parked|rehung|
               no-agent|bypass,
     "source", "subject", "rounds": [{round, rc, run_id, action,
     circuit, applied[], summary, verified?, signature?, note?}],
     "report": {...}|None, "report_path": str|None,
     "signature_candidates": [...]}

    solved/parked/rehung = 已处置（挂载点不算失败）；其余走 unsolved
    关口（报告已生成，report_path 指之）。
    """
    ws = Path(ws)
    from . import gates as gates_mod
    outcome: dict = {"source": failure.get("source"),
                     "subject": failure.get("subject"), "rounds": [],
                     "status": None, "report": None, "report_path": None,
                     "symptom": failure.get("detail"),
                     "signature_candidates": []}

    if not gates_mod.self_diagnosis_enabled():
        outcome["status"] = "bypass"
        _end_event(ws, failure, outcome)
        return outcome
    if os.environ.get("PORTER_NO_AGENT"):
        outcome["status"] = "no-agent"
        outcome["report"], outcome["report_path"] = \
            _report(ws, failure, outcome, cfg)
        _end_event(ws, failure, outcome)
        return outcome

    target_os = Path(failure.get("_workdir") or Path.cwd())
    stem_base = ws / "solve" / "logs" / f"SOLVE_{_safe_name(failure.get('subject'))}"
    consulted: list[str] = []
    prev_sig = ""
    sig_repeat = 1
    prev_ctx = ""
    kb_face_text = ""
    try:
        from ..bootstrap import kb as _kb
        kb_face_text = _kb.kb_face(ws, ["failures", "pitfalls"])
    except Exception:
        pass

    for round_no in range(1, MAX_ROUNDS + 1):
        stem = f"{stem_base}_R{round_no}"
        prompt = _round_prompt(ws, round_no, failure, prev_ctx,
                               kb_face_text)
        events.append_event("errorloop_round",
                            subject=failure.get("subject"),
                            intent=f"R{round_no}",
                            summary=f"求解第 {round_no}/{MAX_ROUNDS} 轮",
                            ws=ws, mount=failure.get("source"))
        rc, out = agent.run_agent(
            prompt, workdir=target_os, log_stem=str(stem),
            timeout_sec=AGENT_TIMEOUT_SEC)
        verdict = _parse_verdict(out) if rc == 0 else None
        round_rec: dict = {"round": round_no, "rc": rc,
                           "run_id": str(stem)}

        if not verdict:
            round_rec.update({"action": None,
                              "summary": "agent 输出不可解析或超时"
                                         "（已留痕）"})
            outcome["rounds"].append(round_rec)
            prev_ctx = _prev_context(ws, stem, out, None, "",
                                     consulted)
            continue

        for dom in ("failures", "pitfalls"):
            try:
                from ..bootstrap import kb as _kb
                _kb.record_consulted(_kb.kb_dir_for(ws), dom,
                                     verdict.get("kb_consulted") or [])
            except Exception:
                pass
        consulted += [c for c in verdict.get("kb_consulted") or []
                      if c not in consulted]
        outcome["signature_candidates"] += \
            verdict.get("signature_candidates") or []

        applied, terminal = _apply_action(ws, failure, verdict)
        if verdict.get("action") == "fix-code":
            try:                        # vcs：修码动作落 commit（每次修改）
                from ..common import vcs as _vcs
                _vcs.commit_target(
                    ws, f"solve[{failure.get('source')}]: fix-code "
                        f"{failure.get('subject')}", phase="solve")
            except Exception:
                pass
        round_rec.update({"action": verdict.get("action"),
                          "circuit": verdict.get("circuit"),
                          "applied": applied,
                          "signature_candidates":
                              verdict.get("signature_candidates") or [],
                          "summary": (verdict.get("summary") or "")[:300]})
        if terminal == "escalate":
            round_rec["note"] = "agent 明确升级"
            outcome["rounds"].append(round_rec)
            outcome["status"] = "escalated"
            break
        if terminal in ("parked", "rehung"):
            outcome["rounds"].append(round_rec)
            outcome["status"] = terminal
            break

        try:
            ok, new_fail = verify()
        except Exception as ex:                    # 复验面异常不吞轨迹
            ok, new_fail = False, {"detail": f"verify 异常：{ex}"}
        round_rec["verified"] = bool(ok)
        outcome["rounds"].append(round_rec)

        if ok:
            outcome["status"] = "solved"
            _sediment_case(ws, failure, verdict)
            break

        sig = failure_signature(failure.get("subject"),
                                (new_fail or {}).get("detail"),
                                _log_blob(new_fail or {}))
        round_rec["signature"] = sig
        if sig and sig == prev_sig:
            sig_repeat += 1
        else:
            sig_repeat = 1
            prev_sig = sig
        if sig_repeat >= SAME_SIG_REPEAT:
            outcome["status"] = "early-exit"
            round_rec["note"] = f"同签名 {sig} 连发——零进展提前截断"
            break
        failure = {**failure, **{k: v for k, v in (new_fail or {}).items()
                                 if k in ("detail", "boot_log", "ut_out",
                                          "build_out", "boot_log_raw")}}
        prev_ctx = _prev_context(ws, stem, out, verdict,
                                 _log_blob(new_fail or {}), consulted)

    if outcome["status"] is None:
        outcome["status"] = "unsolved"
    if outcome["status"] in _UNSOLVED_STATUSES:
        outcome["report"], outcome["report_path"] = \
            _report(ws, failure, outcome, cfg)
    _end_event(ws, failure, outcome)
    return outcome
