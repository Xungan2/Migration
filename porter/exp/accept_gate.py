"""accept_gate.py — accept 本地关口原语（feat/prepare 形态，零 loop 依赖）。

关口 = 人审决策点。与 test 分支 loop/gates 的关系：语义子集（decision
类关口的登记/作答/指纹绑定），实现独立——accept 不引入 loop 子系统。

约定：
- 关口台账：`exp-accept/gates.json`（机器账本；人读视图渲染到
  `exp-accept/GATES.md`）；
- 作答文件：工作区 `answers.md`（人写），节格式：

      ## @exp-accept.inject
      verdict: approve
      note: 可选意见行

  一个关口多节时以最后一节为准；
- 指纹 belts-and-braces：登记时记录 artifact 的 sha16；作答 approve 后
  若 artifact 被改动（指纹不符），关口自动重置为 open，人须重审。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

_APPROVE_WORDS = ("approve", "release", "放行", "通过")
_REJECT_WORDS = ("reject", "拒绝", "否决")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sha16_file(path: Path) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def combined_sha16(paths: list[Path]) -> str:
    """多文件联合指纹：sha16 over 逐文件 `name:sha16` 行（按路径排序）。"""
    parts = [f"{Path(p).name}:{sha16_file(p)}"
             for p in sorted(paths, key=str)]
    if not parts:
        return ""
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def _load(ws: Path) -> dict:
    p = Path(ws) / "exp-accept" / "gates.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"gates": []}


def _save(ws: Path, doc: dict) -> None:
    p = Path(ws) / "exp-accept" / "gates.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")


def _find(doc: dict, gate_id: str) -> dict | None:
    for g in doc.get("gates") or []:
        if g.get("id") == gate_id:
            return g
    return None


def register_gate(ws: Path, gate_id: str, question: str,
                  context_files: list[str],
                  artifact_path: Path | list[Path]) -> dict:
    """登记（或重开重置）一个 decision 关口。返回关口记录。

    artifact_path 支持单文件或多文件列表（联合指纹）。
    """
    ws = Path(ws)
    doc = _load(ws)
    arts = [Path(p) for p in
            ([artifact_path] if isinstance(artifact_path, (str, Path))
             else artifact_path)]
    sha = combined_sha16(arts)
    g = _find(doc, gate_id)
    if g is None:
        g = {"id": gate_id, "status": "open", "history": []}
        doc.setdefault("gates", []).append(g)
    else:
        g.setdefault("history", []).append(
            {"time": _now(), "event": "re-registered",
             "detail": f"重新登记，指纹刷新 {g.get('artifact_sha')} → {sha}"})
    g.update({"status": "open", "question": question,
              "context_files": list(context_files),
              "artifact_path": str(arts[0]) if arts else "",
              "artifact_paths": [str(p) for p in arts],
              "artifact_sha": sha,
              "answer": None, "answered_at": None})
    # 注意：consumed_verdict/consumed_note 跨登记保留——重开后
    # answers.md 里的旧节继续失效，人须更新该节内容重新表态。
    _save(ws, doc)
    render_gates(ws)
    return g


def _parse_answers(ws: Path) -> dict[str, dict]:
    """解析 answers.md 的 `## @<gate-id>` 节 → {gate_id: {verdict, note}}。"""
    p = Path(ws) / "answers.md"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict[str, dict] = {}
    cur: str | None = None
    for ln in text.splitlines():
        m = re.match(r"^##\s*@([\w.-]+)\s*$", ln.strip())
        if m:
            cur = m.group(1)
            out[cur] = {"verdict": "", "note": ""}
            continue
        if cur is None:
            continue
        vm = re.match(r"^verdict:\s*(.+)$", ln.strip(), re.IGNORECASE)
        if vm:
            out[cur]["verdict"] = vm.group(1).strip().lower()
            continue
        nm = re.match(r"^note:\s*(.+)$", ln.strip(), re.IGNORECASE)
        if nm:
            out[cur]["note"] = nm.group(1).strip()
    return out


def _fingerprint_reset(ws: Path, doc: dict, g: dict) -> bool:
    """approved 态指纹复查：artifact 与登记指纹不符 → 重置 open。

    消费态早退路径同样复查（防"放行后篡改 artifact 仍 approved"）。
    返回 True = 已重置。
    """
    if g.get("status") != "approved":
        return False
    arts = [Path(p) for p in (g.get("artifact_paths")
                              or ([g.get("artifact_path")]
                                  if g.get("artifact_path") else []))]
    cur = combined_sha16(arts)
    if cur and g.get("artifact_sha") and cur != g["artifact_sha"]:
        g.update({"status": "open", "answer": None})
        g.setdefault("history", []).append(
            {"time": _now(), "event": "reset",
             "detail": "作答后 artifact 已变更（指纹不符）——重置待人审"})
        _save(ws, doc)
        render_gates(ws)
        return True
    return False


def gate_state(ws: Path, gate_id: str) -> tuple[str, str]:
    """关口状态：(state, note)。state ∈ open|approved|rejected。

    已消费语义：一次作答（verdict+note 对）只对一次登记生效——重开
    （re-register/redraft）后，answers.md 里残留的旧节不再触发状态
    迁移，人须更新该节内容重新表态（措辞/注记变化即可）。
    approved 另需 artifact 当前指纹与登记指纹一致（不一致 → 重置
    open，人重审；消费态早退路径同样复查）。
    """
    ws = Path(ws)
    doc = _load(ws)
    g = _find(doc, gate_id)
    if g is None:
        return "open", "关口未登记"
    ans = _parse_answers(ws).get(gate_id) or {}
    verdict = ans.get("verdict") or ""
    note = ans.get("note", "")
    consumed = {"verdict": g.get("consumed_verdict"),
                "note": g.get("consumed_note")}
    fresh = verdict and consumed != {"verdict": verdict, "note": note}
    if verdict and not fresh:
        if g.get("status") == "approved":
            if _fingerprint_reset(ws, doc, g):
                return ("open",
                        "作答指向的文件在作答后被改动——关口已重置，"
                        "请审阅当前内容后重新表态")
            return "approved", note
        if g.get("status") == "rejected":
            return g["status"], g.get("reject_note", "")
        return "open", ("该作答已被消费——关口重开后须更新 answers.md "
                        "该节内容重新表态")
    if verdict in _APPROVE_WORDS:
        if _fingerprint_reset(ws, doc, g):
            return ("open",
                    "作答指向的文件在作答后被改动——关口已重置，"
                    "请审阅当前内容后重新表态")
        g.update({"status": "approved", "answer": verdict,
                  "answered_at": _now(),
                  "consumed_verdict": verdict, "consumed_note": note})
        _save(ws, doc)
        return "approved", note
    if verdict in _REJECT_WORDS:
        g.update({"status": "rejected", "answer": verdict,
                  "answered_at": _now(), "reject_note": note,
                  "consumed_verdict": verdict, "consumed_note": note})
        g.setdefault("history", []).append(
            {"time": _now(), "event": "rejected", "detail": note[:200]})
        _save(ws, doc)
        render_gates(ws)
        return "rejected", note
    g["status"] = "open"
    _save(ws, doc)
    return "open", note


def reject_note(ws: Path, gate_id: str) -> str:
    """最近一次 reject 的意见（供 --tier 重跑注入 prompt；消费不清除）。"""
    g = _find(_load(Path(ws)), gate_id) or {}
    return str(g.get("reject_note") or "")


def render_gates(ws: Path) -> Path:
    """人读视图：exp-accept/GATES.md。"""
    ws = Path(ws)
    doc = _load(ws)
    lines = ["# accept 关口（人审决策点）", "",
             "> 作答：在工作区 `answers.md` 追加节（每关口一节）：",
             ">", "> ```", "> ## @<关口id>", "> verdict: approve|reject",
             "> note: 可选意见", "> ```", ">",
             "> 作答只对一次登记生效——关口重开（重做/redraft）后，旧节",
             "> 不再触发状态迁移；须**更新该节内容**（改措辞/加注记）重新",
             "> 表态。", ""]
    for g in doc.get("gates") or []:
        arts = g.get("artifact_paths") or \
            ([g.get("artifact_path")] if g.get("artifact_path") else [])
        lines += [f"## @{g.get('id')} —— {g.get('status', 'open')}", "",
                  f"- 问题：{g.get('question', '')}",
                  f"- 评审材料：{g.get('context_files')}",
                  f"- 绑定文件：{[Path(a).name for a in arts]}"
                  f"（联合指纹 `{g.get('artifact_sha')}`）", ""]
    out = ws / "exp-accept" / "GATES.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
