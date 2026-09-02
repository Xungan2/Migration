"""store.py — events.jsonl 存储（log 子系统的机读 sink，真值源）。

自 loop/events.py 平移（行为不变）；schema v1.1 的附加字段见 append_event。

设计：
- append_event() → 工作区 events.jsonl（append-only，永不改写）；
- 进程级 bind(ws, mount) 后自动记录（未绑定 = no-op，向后兼容）；
- 附加字段（phase/module/step/attempt/level/run_id/ref）只增不改，
  旧文件/旧调用永久兼容；
- 观测纪律：记录永不抛异常（观测面不能打断流水线）；字符串字段
  截断到 _MAX_FIELD 字符防 jsonl 膨胀。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

_MAX_FIELD = 400            # 单字段截断上限（字符）

# 进程级记录器：{ws: Path, mount: str} | None
_RECORDER: dict | None = None


# ---------- 绑定 ----------

def bind(ws: Path, mount: str) -> None:
    """绑定当前工作区与挂载点（各相位入口调用；兼容面，新代码用 core）。"""
    global _RECORDER
    _RECORDER = {"ws": Path(ws), "mount": mount}


def unbind() -> None:
    global _RECORDER
    _RECORDER = None


def bound() -> dict | None:
    return _RECORDER


# ---------- events.jsonl ----------

def _clip(v) -> str | None:
    if v is None:
        return None
    s = str(v)
    return s if len(s) <= _MAX_FIELD else s[:_MAX_FIELD] + "…"


# schema v1.1 附加字段（只增不改；None = 不写入，保持旧行兼容）
_EXTRA_FIELDS = ("phase", "module", "step", "attempt", "level",
                 "run_id", "ref")


def append_event(kind: str, subject: str | None = None,
                 intent: str | None = None, cmd: str | None = None,
                 rc: int | None = None, summary: str | None = None,
                 mount: str | None = None, ws: Path | None = None,
                 **extra) -> bool:
    """追加一条事件（未绑定且未显式给 ws 时 no-op）。永不抛异常。"""
    try:
        rec_ws = Path(ws) if ws is not None else \
            (_RECORDER or {}).get("ws")
        if rec_ws is None:
            return False
        rec_mount = mount or (_RECORDER or {}).get("mount")
        ev = {"time": datetime.now().isoformat(timespec="milliseconds"),
              "kind": kind,
              "mount": rec_mount,
              "subject": subject,
              "intent": _clip(intent),
              "cmd": _clip(cmd),
              "rc": rc,
              "summary": _clip(summary)}
        for k in _EXTRA_FIELDS:
            if k in extra:
                v = extra.pop(k)
                if v is not None:
                    ev[k] = _clip(v) if isinstance(v, str) else v
        for k, v in extra.items():
            if v is not None:
                ev[k] = _clip(v) if isinstance(v, str) else v
        # phase 缺省回落 bind（与 mount 同源）——兼容面事件无需改调用点
        # 即可按相位查询（显式 phase/record 的 ctx 优先级不受影响）
        if "phase" not in ev and rec_mount is not None:
            ev["phase"] = rec_mount
        path = Path(rec_ws) / "events.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def read_events(ws: Path) -> list[dict]:
    """读全部事件（坏行跳过）。"""
    path = Path(ws) / "events.jsonl"
    out: list[dict] = []
    if not path.exists():
        return out
    for ln in path.read_text(encoding="utf-8", errors="replace") \
            .splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def tail_events(ws: Path, subject: str | None = None,
                mount: str | None = None, kind_prefix: str | None = None,
                limit: int = 200) -> list[dict]:
    """过滤取尾部（升级报告/考古用）。subject 支持前缀过滤。"""
    evs = read_events(ws)
    sel = []
    for e in evs:
        if subject is not None:
            es = e.get("subject") or ""
            if es != subject and not es.startswith(subject + ".") \
                    and not es.startswith(subject + "/"):
                continue
        if mount is not None and e.get("mount") != mount:
            continue
        if kind_prefix is not None and not str(e.get("kind") or "") \
                .startswith(kind_prefix):
            continue
        sel.append(e)
    return sel[-limit:]


# ---------- 埋桩助手（agent.run_agent / env/probe._run 调用） ----------

def note_agent_start(log_stem: str, prompt: str) -> None:
    append_event("agent_start", intent=log_stem, cmd=prompt)


def note_agent_end(log_stem: str, rc: int, out: str) -> None:
    tail = (out or "")[-300:].strip().replace("\n", " ⏎ ")
    append_event("agent_end", intent=log_stem, rc=rc, summary=tail)


def note_cmd_start(cmd: str, log_path: Path | str) -> None:
    append_event("cmd_start", cmd=cmd, summary=str(log_path))


def note_cmd_end(cmd: str, rc: int, out: str, elapsed_sec: float | None,
                 log_path: Path | str) -> None:
    append_event("cmd_end", cmd=cmd, rc=rc,
                 summary=(f"{elapsed_sec:.0f}s" if elapsed_sec is not None
                          else "") + f" log={log_path}",
                 extra_out_tail=(out or "")[-200:].strip())
