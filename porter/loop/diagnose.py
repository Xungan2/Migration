"""diagnose.py — 升级报告生成（错误处理模块的报告面）。

- generate_escalation_report()：升级报告由**编排器**（非 agent）从
  events.jsonl（log.query）/ 快照 manifest / 求解轮次生成——六字段
  schema：symptom / env_snapshot / excluded[{hypothesis,evidence,ref}] /
  experiments / remaining / reproduce；evidence_files **全指不可变快照**。
  消费方：errorloop（求解耗尽终态）自动调用。

- 签名候选回流 knowledge 子系统（2026-09-03 定案）：报告的
  signature_candidates 记入 kb 候选账（temp/candidates，CP5 审核晋升
  failures 域），不再写 failures.md 候选区（该文件已随 §15 重设计退役）。

历史：本模块原有 run_diagnosis（2 轮×≤10 有界深诊）与
build_context_pack，已由求解循环（errorloop.py）吸收替代。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from . import events
from ..log import query as lq


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


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


def _rounds_to_excluded(verdicts: list[dict]) -> list[dict]:
    """求解轮次的归责判定 = 已倾向/已排除的假设。"""
    out = []
    names = {"infra": "基础设施/环境层", "criteria": "判据/测试/文档错",
             "migration": "迁移 bug", "attribution": "归属错",
             "platform": "平台缺口", "unknown": "未知"}
    for v in verdicts:
        circuit = v.get("circuit")
        if circuit in (None, "unknown"):
            if circuit == "unknown" and v.get("notes"):
                out.append({"hypothesis": "求解轮未能归责（unknown）",
                            "evidence": str(v.get("notes"))[:300],
                            "ref": "events.jsonl(errorloop_round)"})
            continue
        out.append({
            "hypothesis": f"倾向 {names.get(circuit, circuit)} 类"
                          f"（第 {v.get('_round', '?')} 轮判定）",
            "evidence": "; ".join(
                f"{e.get('file')}:{e.get('line')} {e.get('quote', '')[:60]}"
                for e in v.get("evidence") or [])[:300]
            or str(v.get("notes") or "")[:300],
            "ref": "events.jsonl(errorloop_round)"})
    return out


def _merge_events(ws: Path, subject: str, source: str,
                  cap: int = 150) -> list[dict]:
    """subject 维度 ∪ mount 维度（cmd 级事件不携带 subject），按时间序。"""
    def key(e: dict):
        return (e.get("time"), e.get("kind"), str(e.get("cmd")),
                str(e.get("summary")))
    by_key: dict = {}
    for e in lq.events(ws, subject=subject, limit=cap):
        by_key[key(e)] = e
    for e in lq.events(ws, kind_prefix=None, limit=cap):
        if e.get("phase", e.get("mount")) == source:
            by_key.setdefault(key(e), e)
    if not by_key:
        for e in lq.events(ws, limit=cap):
            by_key[key(e)] = e
    return sorted(by_key.values(), key=lambda e: e.get("time") or "")


def generate_escalation_report(ws: Path, source: str, subject: str,
                               symptom: str,
                               triage_verdicts: list[dict] | None = None,
                               diagnosis: dict | None = None,
                               cfg: dict | None = None) -> tuple[dict, bool]:
    """生成升级报告（json + md）。返回 (report, human_stop)。

    human_stop 恒 False（diagnosis_escalation 门已随 §15 重设计退役——
    耗尽终态必停 unsolved 关口，由挂载点开）。参数保留兼容旧签名。
    """
    ws = Path(ws)
    triage_verdicts = triage_verdicts or []
    diagnosis = diagnosis or {}
    evs = _merge_events(ws, subject, source)
    snaps = _snapshots_for(ws, subject)

    excluded = _rounds_to_excluded(triage_verdicts) \
        + (diagnosis.get("excluded") or [])
    experiments = _events_to_experiments(evs) \
        + (diagnosis.get("experiments") or [])
    remaining = (diagnosis.get("remaining") or []) or [
        {"hypothesis": "（求解循环耗尽——remaining 待人工/下一轮）",
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
              "solve_rounds": triage_verdicts}

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
    return report, False


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
    lines += ["", "## excluded（已排除/已倾向假设，勿重查）", ""]
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
        lines += ["", "## 新签名候选（已记 kb 候选账，待 CP5 晋升）", ""]
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
    """签名候选 → kb 候选账（CP5 审核；suggested=failures 域）。"""
    if not candidates:
        return
    try:
        from ..bootstrap import candidates as _cand
        for c in candidates:
            _cand.record_candidate(
                ws, hook="escalation", ref=str(c),
                draft=f"失败签名候选：{c}（升级报告自动记录——症状/判别/"
                      "归责/动作四节待人工改写入册）",
                suggested="failures")
    except Exception:
        pass
