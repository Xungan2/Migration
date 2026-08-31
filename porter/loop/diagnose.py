"""diagnose.py — 诊断与升级（plan §15 子系统 D）。

组成：
- generate_escalation_report()：升级报告由**编排器**（非 agent）从
  events.jsonl / 快照 manifest / triage verdict 生成——六字段 schema：
  symptom / env_snapshot / excluded[{hypothesis,evidence,ref}] /
  experiments / remaining / reproduce；evidence_files **全指不可变快照**。
- run_diagnosis()：有界诊断编排——2 轮 × ≤10 工具调用（预算写进
  prompt，agent 超时/零产出也留痕：每步增量落 events + 专用日志）。
- build_context_pack()：为 context-extract 考古流程组装输入包
  （events 切片 + 快照清单 + 报告路径）。

审核门（porter/config.json review_gates）：
- diagnosis_escalation = human → 报告生成后停车（写 human_questions.md，
  answers.md `diagnosis_escalation: approve` 放行续跑）。
- b_class_autofix 门在 triage.apply_verdict 消费。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from ..common import agent
from . import events

MAX_ROUNDS = 2
MAX_TOOL_CALLS_PER_ROUND = 10        # 写进 prompt 的预算（软约束）

_RELEASE_RE = re.compile(
    r"diagnosis_escalation\s*[:：]\s*(approve|release|放行|通过)",
    re.IGNORECASE)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def gate_mode(name: str, cfg: dict | None = None) -> str:
    """复用 p6 的 review_gates 读取语义（缺键即 agent）。"""
    if cfg is None:
        from . import p6 as p6_mod
        cfg = p6_mod.load_config()
    mode = ((cfg.get("review_gates") or {}).get(name)) or "agent"
    return "human" if mode == "human" else "agent"


def released(ws: Path) -> bool:
    """answers.md 是否已放行 diagnosis_escalation（human 门续跑）。"""
    p = ws / "answers.md"
    if not p.exists():
        return False
    return any(_RELEASE_RE.search(ln)
               for ln in p.read_text(encoding="utf-8").splitlines())


# ---------- 升级报告（编排器侧生成，零 agent） ----------

def _snapshots_for(ws: Path, subject: str) -> list[dict]:
    out = []
    for d in sorted(ws.glob("failure-snapshot-*")):
        man = _load_json(d / "manifest.json", None)
        if man and (man.get("subject") == subject
                    or str(man.get("subject") or "").startswith(
                        subject + ".")):
            out.append({"dir": d.name, "manifest": man})
    return out


def _events_to_experiments(evs: list[dict]) -> list[dict]:
    """events 里的重跑/命令即受控实验记录。"""
    out = []
    for e in evs:
        if e.get("kind") == "cmd_end":
            out.append({"name": str(e.get("cmd") or "")[:120],
                        "result": f"rc={e.get('rc')} {e.get('summary') or ''}"
                                  [:200],
                        "conclusion": "（命令级记录，见 events.jsonl）"})
    return out


def _triage_to_excluded(verdicts: list[dict]) -> list[dict]:
    """triage 已判明的回路 = 被排除的其他假设。"""
    out = []
    if not verdicts:
        return out
    names = {"infra": "基础设施/环境层", "criteria": "判据/测试/文档错",
             "migration": "迁移 bug", "attribution": "归属错",
             "platform": "平台缺口", "unknown": "未知"}
    seen_final = verdicts[-1]
    for v in verdicts:
        if v.get("circuit") in (None, "unknown"):
            continue
        out.append({
            "hypothesis": f"非 {names[v['circuit']]} 类以外的回路（triage "
                          f"rule={v.get('rule_id')} 判定该类成立/排除）",
            "evidence": "; ".join(
                f"{e.get('file')}:{e.get('line')} {e.get('quote', '')[:60]}"
                for e in v.get("evidence") or [])[:300]
            or (v.get("notes") or "")[:300],
            "ref": "events.jsonl(triage)"})
    if seen_final.get("circuit") == "unknown":
        out.append({"hypothesis": "规则与分诊 agent 均未能判定（unknown）",
                    "evidence": seen_final.get("notes") or "",
                    "ref": "events.jsonl(triage)"})
    return out


def _merge_events(ws: Path, subject: str, source: str,
                  cap: int = 150) -> list[dict]:
    """subject 维度 ∪ mount 维度（cmd 级事件不携带 subject），按时间序。"""
    def key(e: dict):
        return (e.get("time"), e.get("kind"), str(e.get("cmd")),
                str(e.get("summary")))
    by_key: dict = {}
    for e in events.tail_events(ws, subject=subject, limit=cap):
        by_key[key(e)] = e
    for e in events.tail_events(ws, mount=source, limit=cap):
        by_key.setdefault(key(e), e)
    if not by_key:
        for e in events.tail_events(ws, limit=cap):
            by_key[key(e)] = e
    return sorted(by_key.values(), key=lambda e: e.get("time") or "")


def generate_escalation_report(ws: Path, source: str, subject: str,
                               symptom: str,
                               triage_verdicts: list[dict] | None = None,
                               diagnosis: dict | None = None,
                               cfg: dict | None = None) -> tuple[dict, bool]:
    """生成升级报告（json + md）。返回 (report, human_stop)。

    diagnosis = run_diagnosis 的合并产物（可 None——MVP 形态：规则判不了
    即升级，报告仍完整）。
    """
    ws = Path(ws)
    triage_verdicts = triage_verdicts or []
    diagnosis = diagnosis or {}
    evs = _merge_events(ws, subject, source)
    snaps = _snapshots_for(ws, subject)

    excluded = _triage_to_excluded(triage_verdicts) \
        + (diagnosis.get("excluded") or [])
    experiments = _events_to_experiments(evs) \
        + (diagnosis.get("experiments") or [])
    remaining = (diagnosis.get("remaining") or []) or [
        {"hypothesis": "（无诊断轮——remaining 待人工/下一轮）",
         "evidence": ""}]
    reproduce = diagnosis.get("reproduce") or _reproduce_from_runner(ws)
    evidence_files = []
    for s in snaps:
        m = s["manifest"]
        for key, f in (m.get("files") or {}).items():
            if "copied" in f:
                evidence_files.append(f"{s['dir']}/{f['copied']}")
        evidence_files.append(f"{s['dir']}/manifest.json")

    report = {"time": _now(), "source": source, "subject": subject,
              "symptom": symptom,
              "env_snapshot": {
                  "snapshots": [{"dir": s["dir"],
                                 "kernel": s["manifest"].get("kernel"),
                                 "qemu_cmdline": s["manifest"].get(
                                     "qemu_cmdline")}
                                for s in snaps],
                  "events_span": (evs[0]["time"], evs[-1]["time"])
                  if evs else None},
              "excluded": excluded,
              "experiments": experiments,
              "remaining": remaining,
              "reproduce": reproduce,
              "evidence_files": evidence_files,
              "signature_candidates": sorted({
                  c for v in triage_verdicts
                  for c in (v.get("signature_candidates") or [])}
                  | set(diagnosis.get("signature_candidates") or [])),
              "triage_verdicts": triage_verdicts,
              "diagnosis": {k: diagnosis.get(k) for k in
                            ("excluded", "experiments", "remaining",
                             "reproduce", "verdict") if k in diagnosis}}

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", subject)[:60]
    rp = ws / "escalations"
    rp.mkdir(parents=True, exist_ok=True)
    jp = rp / f"{safe}-{datetime.now():%Y%m%d-%H%M%S}.json"
    jp.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    _write_md(rp / f"{safe}.md", report)
    _attach_signature_candidates(ws, report["signature_candidates"])
    events.append_event("escalation", subject=subject, intent=source,
                        summary=f"升级报告 {jp.name}（excluded="
                                f"{len(excluded)} remaining="
                                f"{len(remaining)} evidence="
                                f"{len(evidence_files)}）",
                        ws=ws, mount=source)

    human_stop = False
    if gate_mode("diagnosis_escalation", cfg) == "human":
        human_stop = True
        with (ws / "human_questions.md").open("a", encoding="utf-8") as f:
            f.write(f"\n\n---\n\n## 诊断升级审核门（diagnosis_escalation="
                    f"human）\n- 时间：{_now()}；对象：{subject}\n"
                    f"- 报告：{jp}\n"
                    f"- 放行：answers.md 写 `diagnosis_escalation: approve`"
                    f" 后重跑/续跑。\n")
    return report, human_stop


def _write_md(path: Path, r: dict) -> None:
    lines = [f"# 升级报告：{r['subject']}", "",
             f"- 时间：{r['time']}；挂载点：{r['source']}",
             f"- 症状：{r['symptom']}", "",
             "## env_snapshot", ""]
    for s in r["env_snapshot"]["snapshots"]:
        k = s.get("kernel") or {}
        lines.append(f"- {s['dir']}：kernel="
                     f"{(k.get('sha256') or 'not-found')[:12]}"
                     f" cmdline=`{(s.get('qemu_cmdline') or '')[:120]}`")
    if not r["env_snapshot"]["snapshots"]:
        lines.append("-（无快照——现场未抢救或已丢失）")
    lines += ["", "## excluded（已排除假设，勿重查）", ""]
    for e in r["excluded"]:
        lines.append(f"- **{e['hypothesis']}**：{e['evidence'][:200]}"
                     f"（ref: {e.get('ref', '')}）")
    lines += ["", "## experiments（受控实验）", ""]
    for e in r["experiments"]:
        lines.append(f"- {e['name']} → {e['result']}：{e.get('conclusion','')}")
    lines += ["", "## remaining（剩余假设，按可能性排序）", ""]
    for e in r["remaining"]:
        lines.append(f"- {e['hypothesis']}"
                     + (f"（证据：{e['evidence'][:160]}）"
                        if e.get("evidence") else ""))
    lines += ["", "## reproduce", "", "```", r["reproduce"] or "（无）",
              "```", "", "## evidence_files（全指不可变快照）", ""]
    lines += [f"- {f}" for f in r["evidence_files"]] or ["-（无）"]
    if r["signature_candidates"]:
        lines += ["", "## 新签名候选（已自动附 failures.md 候选区）", ""]
        lines += [f"- {c}" for c in r["signature_candidates"]]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _reproduce_from_runner(ws: Path) -> str:
    runner = _load_json(ws / "runner.json", {})
    parts = []
    for k in ("build", "boot"):
        c = (runner.get(k) or {}).get("cmd")
        if c:
            parts.append(f"# {k}\n{c}")
    ut = (runner.get("unit_test") or {}).get("cmd")
    if ut:
        parts.append(f"# unit_test\n{ut}")
    return "\n\n".join(parts)


def _attach_signature_candidates(ws: Path, candidates: list[str]) -> None:
    """自动附候选到 knowledge/failures.md 候选区（人工晋升，非直写库）。"""
    if not candidates:
        return
    from ..common.agent import TOOL_ROOT
    p = TOOL_ROOT / "knowledge" / "failures.md"
    if not p.exists():
        return
    text = p.read_text(encoding="utf-8")
    add = [f"- {c}（{datetime.now():%Y-%m-%d} 由升级报告自动附上，"
           f"待人工晋升）" for c in candidates
           if c not in text]
    if not add:
        return
    marker = "## 候选区（agent 自动附上来的，待人工晋升）"
    block = marker + "\n\n" + "\n".join(add) + "\n"
    if marker in text:
        text = text.replace(marker, block, 1)
    else:
        text += "\n" + block
    p.write_text(text, encoding="utf-8")


# ---------- 有界诊断编排（2 轮 × ≤10 工具调用） ----------

def _diagnosis_prompt(evidence: dict, round_no: int,
                      prior: dict | None) -> str:
    skill = agent.load_skill("diagnose")
    slim = {k: (v if not isinstance(v, str) or len(v) <= 2000
                else v[:2000] + "…")
            for k, v in evidence.items()
            if k in ("source", "subject", "module", "kind", "expr",
                     "detail", "boot_log", "ut_out", "build_out",
                     "criterion", "defect", "deferred", "snapshot")}
    lines = [skill, "", "---", "",
             f"## 失败证据包（JSON）", "```json",
             json.dumps(slim, ensure_ascii=False, indent=1), "```", ""]
    if round_no == 1:
        lines.append(f"## 任务\n第 {round_no}/{MAX_ROUNDS} 轮。按 skill "
                     f"方法论做有界诊断（本轮 ≤{MAX_TOOL_CALLS_PER_ROUND} "
                     "次工具调用），输出紧凑 JSON。")
    else:
        lines.append(f"## 任务\n第 {round_no}/{MAX_ROUNDS} 轮。上一轮结论"
                     "如下——**继续收敛 remaining（复核/补实验），勿重查 "
                     "excluded**：\n```json\n"
                     + json.dumps(prior or {}, ensure_ascii=False)
                     + "\n```")
    return "\n".join(lines)


def _parse_diag(out: str) -> dict | None:
    blocks = re.findall(r"```json\s*(.*?)```", out, re.DOTALL) \
        or re.findall(r"```\s*(\{.*?\})\s*```", out, re.DOTALL)
    for b in blocks:
        try:
            obj = json.loads(b.strip())
            if isinstance(obj, dict) and ("remaining" in obj
                                          or "excluded" in obj):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def run_diagnosis(ws: Path, evidence: dict,
                  cfg: dict | None = None) -> tuple[dict, dict]:
    """有界诊断：2 轮 × ≤10 调用；每步增量落盘（超时也留痕）。
    返回 (merged_diagnosis, escalation_report)。PORTER_NO_AGENT=1 时跳过
    agent 轮（直接出报告——MVP 形态）。
    """
    ws = Path(ws)
    merged: dict = {"excluded": [], "experiments": [], "remaining": [],
                    "signature_candidates": [], "rounds": []}
    if os.environ.get("PORTER_NO_AGENT"):
        merged["rounds"].append({"round": 0, "rc": None, "parsed": False,
                                 "log": "skipped（PORTER_NO_AGENT）"})
    else:
        log_dir = ws / "diagnose" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        prior = None
        for rnd in range(1, MAX_ROUNDS + 1):
            prompt = _diagnosis_prompt(evidence, rnd, prior)
            events.append_event("diagnose_round", subject=evidence
                                .get("subject"), intent=f"R{rnd}",
                                summary=f"诊断第 {rnd}/{MAX_ROUNDS} 轮启动",
                                ws=ws, mount=evidence.get("source"))
            rc, out = agent.run_agent(
                prompt, workdir=evidence.get("_workdir") or Path.cwd(),
                log_stem=str(log_dir / f"diag_{evidence.get('subject')}"
                                       f"_R{rnd}"), timeout_sec=900)
            parsed = _parse_diag(out) if rc == 0 else None
            merged["rounds"].append({"round": rnd, "rc": rc,
                                     "parsed": bool(parsed),
                                     "log": f"diag_{evidence.get('subject')}"
                                            f"_R{rnd}.log"})
            events.append_event("diagnose_round_end",
                                subject=evidence.get("subject"),
                                intent=f"R{rnd}", rc=rc,
                                summary=("有产出" if parsed else
                                         ("TIMEOUT/零产出（已留痕）"
                                          if rc != 0 else "输出不可解析")),
                                ws=ws, mount=evidence.get("source"))
            if parsed:
                merged["excluded"] += parsed.get("excluded") or []
                merged["experiments"] += parsed.get("experiments") or []
                merged["remaining"] = parsed.get("remaining") \
                    or merged["remaining"]
                if parsed.get("reproduce"):
                    merged["reproduce"] = parsed["reproduce"]
                merged["signature_candidates"] += \
                    parsed.get("signature_candidates") or []
                prior = {k: parsed.get(k) for k in
                         ("excluded", "experiments", "remaining")}
            if parsed and not parsed.get("remaining"):
                break                        # 已收敛
    report, human_stop = generate_escalation_report(
        ws, evidence.get("source") or "d1", evidence.get("subject") or "?",
        symptom=evidence.get("detail") or "",
        triage_verdicts=evidence.get("_triage_verdicts") or [],
        diagnosis=merged, cfg=cfg)
    report["human_stop"] = human_stop
    return merged, report


# ---------- context-extract 考古输入包 ----------

def build_context_pack(ws: Path, source: str, subject: str) -> Path:
    """组装考古输入包（context-extract skill 的任务数据）。"""
    ws = Path(ws)
    evs = events.tail_events(ws, subject=subject, limit=200)
    snaps = [s["dir"] for s in _snapshots_for(ws, subject)]
    escs = sorted((ws / "escalations").glob("*.md")) \
        if (ws / "escalations").exists() else []
    defect = None
    d = _load_json(ws / "defects.json", {"defects": []})
    for e in d.get("defects", []):
        if e.get("id") == subject:
            defect = e
    pack = {"time": _now(), "source": source, "subject": subject,
            "events_file": str(ws / "events.jsonl"),
            "events_lines": len(events.read_events(ws)),
            "events_for_subject": len(evs),
            "snapshots": snaps,
            "escalation_reports": [str(p) for p in escs],
            "defect": defect}
    out = ws / "escalations" / f"context-pack-{re.sub(
        r'[^A-Za-z0-9._-]', '_', subject)[:60]}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pack, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return out
