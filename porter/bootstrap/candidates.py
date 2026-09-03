"""candidates.py — 随机知识探查（事件钩子 → 候选记录 → 新颖性去重闸）。

四类钩子（设计定案——porter 自有场所的闭集，覆盖是结构性的：
任何影响流水线的知识必经 porter 的三个 I/O 面：它写的文件 / 它解析的
表单与 CLI / 它解析的 agent 输出）：
  类 1  gate 应答收口     process_answered_gates——note/rationale 非空即
                          候选（B6/B7/B8/B9 一族）
  类 2  CLI 台账动作      --defect-close/park、finalize-l4 park、
                          patch-register（B11/B12）
  类 3  产物状态翻转      切片 FAIL→PASS 翻转（B3 原始留痕）、探针降级
                          （B5）；fill fell-back 与 runner 回填已被
                          gaps/runbook 固定收成覆盖，不重复设钩
  类 4  agent 自报        结构化输出的 lessons 字段（fill 的 reason 是
                          特例，随固定收成走）

候选记录（未分类、不进任何注入面——分类进子目录后才可被检索）：
  temp/candidates/<驱动>@<目标>.json（按命名空间一本账）

去重闸：draft 规范化（去空白）后 sha1 前 16 位为签名，同账内签名
重复 → 跳过；过短草稿（< min_draft_len）视为无知识跳过。
开关与阈值：porter/config.json 的 kb 节。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

from . import kb
from .. import log as _log

CONFIG_PATH = kb.TOOL_ROOT / "porter" / "config.json"

# 关口 id 前缀 → 建议类（子目录）。查表免费、审核参考、非定案。
_GATE_CLASS = [
    ("p3.gap.", "gaps"),
    ("p4.blocked.", "gaps"),
    ("p0.t3.", "runbook"),
    ("infra.", "runbook"),
    ("loop.attempts.", "pitfalls"),
    ("loop.budget.", "pitfalls"),
    ("p0.t5.", "pitfalls"),
    ("p5.deferred.", "pitfalls"),
    ("p1.resolve.", "pitfalls"),
    ("p0.category.", "pitfalls"),
]


def _config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8")) \
            .get("kb", {})
    except (OSError, json.JSONDecodeError, AttributeError):
        return {}


def suggest_class(gate_id: str) -> str:
    gid = str(gate_id or "")
    for prefix, cls in _GATE_CLASS:
        if gid.startswith(prefix):
            return cls
    return "pitfalls"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _signature(draft: str) -> str:
    norm = re.sub(r"\s+", " ", str(draft)).strip().lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _ledger_path(ws: Path) -> Path | None:
    try:
        proj = json.loads((Path(ws) / "project.json").read_text(
            encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    ns = f"{Path(proj['linux_driver']).name}@{Path(proj['target_os']).name}"
    return kb.temp_root(ws=ws) / "candidates" / f"{ns}.json"


def _load_doc(p: Path) -> dict:
    """账本文档 {seq, items}。兼容旧裸数组格式（seq 取最大 id）。"""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data
    items = data if isinstance(data, list) else []
    return {"seq": _max_id(items), "items": items}


def _max_id(items: list[dict]) -> int:
    ns = []
    for c in items:
        m = re.match(r"^cand-(\d+)$", str(c.get("id", "")))
        if m:
            ns.append(int(m.group(1)))
    return max(ns) if ns else 0


def _save_doc(p: Path, doc: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")


def record_candidate(ws: Path, hook: str, ref: str, draft: str,
                     evidence: list[str] | None = None,
                     suggested: str = "pitfalls",
                     scope_extra: dict | None = None) -> str | None:
    """探查记录：事件 → 候选（去过重闸）。返回候选 id；跳过返回 None。

    ws 用于命名空间与 scope（读 project.json）；evidence 是工作区内
    相对路径列表（审核者按图索骥）。
    """
    cfg = _config()
    if not cfg.get("candidates", True):
        return None
    draft = str(draft or "").strip()
    if len(draft) < int(cfg.get("min_draft_len", 24)):
        return None
    p = _ledger_path(ws)
    if p is None:
        return None
    sig = _signature(draft)
    doc = _load_doc(p)
    ledger = doc["items"]
    if cfg.get("dedup", True) and any(
            c.get("signature") == sig for c in ledger):
        return None
    try:
        proj = json.loads((Path(ws) / "project.json").read_text(
            encoding="utf-8"))
        scope = {"driver": Path(proj["linux_driver"]).name,
                 "target_os": Path(proj["target_os"]).name}
    except (OSError, json.JSONDecodeError, KeyError):
        scope = {}
    if scope_extra:
        scope.update(scope_extra)
    doc["seq"] = int(doc.get("seq", 0)) + 1
    cid = f"cand-{doc['seq']:04d}"
    ledger.append({
        "id": cid,
        "source": {"hook": hook, "ref": str(ref), "time": _now()},
        "scope": scope,
        "draft": draft,
        "evidence": [str(e) for e in (evidence or [])],
        "suggested_class": suggested,
        "signature": sig,
        "status": "pending",
        "history": [{"time": _now(),
                     "event": f"created（建议类 {suggested}）"}],
    })
    _save_doc(p, doc)
    _log.console_line(f"[porter] kb 探查: 新候选 {cid}（{hook}，建议类 {suggested}）")
    try:
        from ..loop import events as _ev
        _ev.append_event("kb-candidate", subject=str(ref),
                         summary=f"{cid} -> {suggested}")
    except Exception:
        pass
    return cid


def load_candidates(ws: Path) -> list[dict]:
    """本工作区命名空间的候选账（审核面/分类用）。"""
    p = _ledger_path(ws)
    return _load_doc(p)["items"] if p else []


def _save_candidates(ws: Path, items: list[dict]) -> bool:
    p = _ledger_path(ws)
    if p is None:
        return False
    doc = _load_doc(p)
    doc["items"] = items
    _save_doc(p, doc)
    return True


def remove_candidate(ws: Path, cid: str) -> bool:
    """晋升/拒绝后出账（seq 保留——id 单调不复用）。"""
    p = _ledger_path(ws)
    if p is None:
        return False
    doc = _load_doc(p)
    rest = [c for c in doc["items"] if c.get("id") != cid]
    if len(rest) == len(doc["items"]):
        return False
    doc["items"] = rest
    _save_doc(p, doc)
    return True


# ---------- 钩子侧助手（供各挂载点调用，保持一行接线） ----------

def record_from_gate(ws: Path, gate: dict, answer: dict) -> str | None:
    """类 1：gate 应答收口——note/rationale 非空即候选。"""
    draft = (answer.get("rationale") or answer.get("note")
             or answer.get("instruction") or "").strip()
    if not draft:
        return None
    return record_candidate(
        ws, hook="gate-answer", ref=gate.get("id", "?"), draft=draft,
        evidence=[f"gates.json#{gate.get('id', '')}"],
        suggested=suggest_class(gate.get("id", "")),
        scope_extra={"module": gate.get("module")})


def record_lessons(ws: Path, parsed: dict, ref: str,
                   evidence: list[str] | None = None) -> list[str]:
    """类 4：agent 结构化输出的 lessons 字段（字符串数组）。"""
    lessons = parsed.get("lessons") if isinstance(parsed, dict) else None
    if not isinstance(lessons, list):
        return []
    out = []
    for s in lessons:
        if isinstance(s, str):
            cid = record_candidate(ws, hook="agent-lesson", ref=ref,
                                   draft=s, evidence=evidence,
                                   suggested="pitfalls")
            if cid:
                out.append(cid)
    return out
