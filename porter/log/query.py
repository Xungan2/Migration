"""query.py — 查询 / run 登记 / 上下文接续 API（log 子系统的消费面）。

- 全部为 events.jsonl 的派生读（无独立账本，可随时重建）；
- run 登记 = agent_start/agent_end 按配对键合并（intent=log stem；
  run_id 为 v1.1 附加字段，旧事件无之同样可查）；
- context_block() 是 agent 上下文接续的正式 API（收编 p4 的手工
  err_info 尾 40 行 / ut_verify.feedback_block 尾 25 行实践）；
- 永不抛异常（查询面不能打断调用方；坏数据返回空结果）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def events(ws: Path, *, kind_prefix: str | None = None,
           subject: str | None = None, phase: str | None = None,
           module: str | None = None, run_id: str | None = None,
           limit: int | None = None) -> list[dict]:
    """结构化过滤（kind/subject 为前缀语义，同 tail_events）。"""
    from . import store
    evs = _safe(lambda: store.read_events(ws), [])
    sel = []
    for e in evs:
        if kind_prefix is not None and not str(e.get("kind") or "") \
                .startswith(kind_prefix):
            continue
        if subject is not None:
            es = e.get("subject") or ""
            if es != subject and not es.startswith(subject + ".") \
                    and not es.startswith(subject + "/"):
                continue
        if phase is not None and e.get("phase", e.get("mount")) != phase:
            continue
        if module is not None and e.get("module") != module:
            continue
        if run_id is not None and e.get("run_id", e.get("intent")) \
                != run_id:
            continue
        sel.append(e)
    return sel[-limit:] if limit is not None else sel


def _parse_time(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None


def runs(ws: Path, *, subject: str | None = None,
         last_n: int = 10) -> list[dict]:
    """agent 运行登记（start/end 配对 → 单条 run 记录，未闭合挂 rc=None）。

    每条：{run_id, intent, phase, module, attempt, rc, duration_sec,
    summary, log, prompt, time_start, time_end}。log/prompt 取自 ref
    （v1.1）或 intent 兜底（旧事件：log = <intent>.log）。
    """
    evs = _safe(lambda: events(ws, kind_prefix="agent_"), [])
    starts: dict[str, dict] = {}
    out: list[dict] = []
    for e in evs:
        key = str(e.get("run_id") or e.get("intent") or "")
        if not key:
            continue
        if e.get("kind") == "agent_start":
            starts[key] = e
            out.append({
                "run_id": key,
                "intent": e.get("intent") or key,
                "phase": e.get("phase") or e.get("mount"),
                "module": e.get("module"),
                "attempt": e.get("attempt"),
                "rc": None, "duration_sec": None,
                "summary": None,
                "log": (e.get("ref") or {}).get("log")
                or f"{key}.log",
                "prompt": (e.get("ref") or {}).get("prompt"),
                "time_start": e.get("time"), "time_end": None,
                "_open": True})
        elif e.get("kind") == "agent_end" and starts.pop(key, None) \
                is not None:
            rec = next(r for r in reversed(out)
                       if r["run_id"] == key and r["_open"])
            rec.update({"rc": e.get("rc"), "summary": e.get("summary"),
                        "time_end": e.get("time"), "_open": False})
            t0, t1 = _parse_time(rec["time_start"]), _parse_time(
                rec["time_end"])
            if t0 and t1:
                rec["duration_sec"] = round((t1 - t0).total_seconds(), 1)
    for r in out:
        r.pop("_open", None)
    if subject is not None:
        out = [r for r in out if r["module"] == subject
               or str(r["intent"]).startswith(str(subject))
               or subject in str(r["intent"])]
    return out[-last_n:]


def _tail_file(path: Path, lines: int) -> str:
    def _read():
        if path and path.is_file():
            return "\n".join(path.read_text(
                encoding="utf-8", errors="replace")
                .splitlines()[-lines:])
        return ""
    return _safe(_read, "")


def tail_text(text: str, lines: int) -> str:
    """字符串尾部 N 行（共享格式器——err_info/feedback_block 的统一切口）。

    lines ≤0 返回 ""。永不抛异常。
    """
    if not text or lines <= 0:
        return ""
    return "\n".join(text.splitlines()[-lines:])


def tail_block(ws: Path, log_path, lines: int = 40,
               title: str = "上一次输出尾部",
               note: str = "") -> str:
    """日志文件尾部块（prompt 注入用；docs/log.md §6 上下文接续族）。

    log_path 相对 ws 解析；文件缺失/为空返回 ""。产出形如：
    "\\n\\n---\\n\\n## {title}\\n{note}```\\n{tail}\\n```"
    """
    tail = _tail_file(Path(ws) / Path(log_path), lines)
    if not tail:
        return ""
    return f"\n\n---\n\n## {title}\n{note}```\n{tail}\n```"


def context_block(ws: Path, subject: str, *, includes: tuple = (
        "outcome", "log_tail"), tail_lines: int = 40) -> str:
    """取 subject 最近一次（或未闭合）agent run 的上下文块，可直接拼 prompt。

    includes 项：outcome（rc/结局摘要）、log_tail（输出尾 N 行）、
    prompt_head（输入开头 20 行）。旧事件（无 ref）按 <stem>.log 兜底。
    无匹配 run 返回 ""。
    """
    inc = set(includes or ())
    rs = runs(ws, subject=subject, last_n=5)
    if not rs:
        return ""
    r = rs[-1]
    parts = [f"## 上一次 agent 运行（{r['run_id']}）"]
    if "outcome" in inc:
        rc = "运行中" if r["rc"] is None else f"rc={r['rc']}"
        parts.append(f"- 结局：{rc}"
                     + (f"；{r['summary']}" if r.get("summary") else ""))
    log_path = Path(ws) / str(r.get("log") or f"{r['run_id']}.log")
    if "log_tail" in inc:
        tail = _tail_file(log_path, tail_lines)
        if tail:
            parts.append(f"- 输出尾 {tail_lines} 行：\n```\n{tail}\n```")
    if "prompt_head" in inc and r.get("prompt"):
        ph = _safe(lambda: "\n".join(
            (Path(ws) / str(r["prompt"])).read_text(
                encoding="utf-8", errors="replace").splitlines()[:20]),
            "")
        if ph:
            parts.append(f"- 输入开头：\n```\n{ph}\n```")
    return "\n".join(parts)


def timeline(ws: Path, *, module: str | None = None,
             limit: int = 200) -> list[dict]:
    """浓缩时间线（debug/resume 视图）：每事件一行摘要。"""
    evs = _safe(lambda: events(ws, module=module), [])
    return [{"time": e.get("time"), "kind": e.get("kind"),
             "subject": e.get("subject"),
             "phase": e.get("phase", e.get("mount")),
             "summary": e.get("summary")} for e in evs][-limit:]
