"""events.py — 观测地基（plan §15 子系统 B：events.jsonl + 失败即快照）。

设计（§15.2，三处挂载共用）：
- append_event() → 工作区 events.jsonl（append-only，永不改写）；
  agent/探针调用前后写：意图 + 命令 + 退出码 + 关键摘要 + 时间戳。
- 埋桩方式 = 包装底层收敛点（agent.run_agent 与 env/probe._run），
  进程级 bind(ws, mount) 后自动记录（未绑定 = no-op，向后兼容）。
- take_failure_snapshot()：判定 FAIL 的第一时间、任何重跑之前抢救现场 →
  ws/failure-snapshot-<n>/（qemu.log/串口/判定输入/内核哈希 best-effort/
  QEMU 命令行/criteria+mapping 状态 + manifest.json）。快照不可变——
  升级报告的 evidence_files 只指向它。

观测纪律：记录永不抛异常（观测面不能打断流水线）；cmd/intent/summary
截断到 _MAX_FIELD 字符防 jsonl 膨胀。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

_MAX_FIELD = 400            # 单字段截断上限（字符）
_MAPPING_COPY_LIMIT = 2 * 1024 * 1024   # mapping.json 超此尺寸只记 sha256
# 内核镜像常见落点（best-effort；找不到记 not-found，不阻塞快照）
_KERNEL_GLOBS = ("osdk/build/*/kernel*", "osdk/build/kernel*",
                 "target/*/debug/kernel*", "target/*/release/kernel*")

# 进程级记录器：{ws: Path, mount: str} | None
_RECORDER: dict | None = None


# ---------- 绑定 ----------

def bind(ws: Path, mount: str) -> None:
    """绑定当前工作区与挂载点（p5/p6/D1 入口调用）。"""
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
        ev = {"time": datetime.now().isoformat(timespec="milliseconds"),
              "kind": kind,
              "mount": mount or (_RECORDER or {}).get("mount"),
              "subject": subject,
              "intent": _clip(intent),
              "cmd": _clip(cmd),
              "rc": rc,
              "summary": _clip(summary)}
        for k, v in extra.items():
            if v is not None:
                ev[k] = _clip(v) if isinstance(v, str) else v
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


# ---------- 失败即快照 ----------

def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _next_snapshot_idx(ws: Path) -> int:
    n = 0
    for d in ws.glob("failure-snapshot-*"):
        m = re.match(r"failure-snapshot-(\d+)$", d.name)
        if m and d.is_dir():
            n = max(n, int(m.group(1)))
    return n + 1


def _find_kernel(target_os: Path | None) -> dict:
    if target_os is None:
        return {"found": False, "reason": "no-target-os"}
    for pat in _KERNEL_GLOBS:
        for p in sorted(target_os.glob(pat)):
            if p.is_file():
                try:
                    return {"found": True, "path": str(p),
                            "size": p.stat().st_size, "sha256": _sha256(p)}
                except OSError:
                    continue
    return {"found": False, "reason": "no-kernel-glob-hit"}


def _qemu_cmdline(runner: dict | None, extra_env: dict | None) -> str:
    if not runner:
        return ""
    boot = (runner.get("boot") or {}).get("cmd") or ""
    inj = runner.get("inject_device") or {}
    parts = [boot]
    if inj.get("mechanism") == "env" and extra_env:
        for k, v in extra_env.items():
            parts.append(f"({k}={v})")
    elif inj.get("cmd_suffix"):
        parts.append(f"(suffix={inj['cmd_suffix']})")
    return " ".join(x for x in parts if x)


def _copy_in(src: Path, dst: Path, files: dict, key: str) -> None:
    """把 src 复制进快照；不存在记 missing。"""
    if src and src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
        files[key] = {"copied": str(dst.name), "size": dst.stat().st_size}
    else:
        files[key] = {"missing": str(src) if src else "unset"}


def take_failure_snapshot(ws: Path, source: str, subject: str,
                          reason: str, runner: dict | None = None,
                          target_os: Path | None = None,
                          extra_files: list[tuple[Path, str]] | None = None,
                          extra_env: dict | None = None) -> Path | None:
    """失败现场抢救（重跑前调用）。返回快照目录；失败返回 None（不抛）。

    source ∈ {"p5","p6","d1",...}（挂载点）；subject = 判据 id / 红项 /
    defect id；extra_files = [(源路径, 快照内文件名), ...]（判定输入等）。
    """
    try:
        ws = Path(ws)
        n = _next_snapshot_idx(ws)
        snap = ws / f"failure-snapshot-{n}"
        snap.mkdir(parents=True, exist_ok=False)
        files: dict[str, dict] = {}

        # qemu.log / 串口日志（boot 判定的真值源）
        log_file = ((runner or {}).get("boot") or {}).get("log_file")
        if log_file:
            p = Path(log_file) if Path(log_file).is_absolute() \
                else (target_os / log_file if target_os else Path(log_file))
            _copy_in(p, snap / "qemu.log", files, "qemu_log")
        else:
            files["qemu_log"] = {"missing": "runner.boot.log_file unset"}
        serial = (target_os / "qemu-serial.log") if target_os else None
        _copy_in(serial, snap / "qemu-serial.log", files, "serial_log")

        # 调用方指定判定输入（acceptance / 红项 / criteria 等）
        for i, (src, name) in enumerate(extra_files or []):
            _copy_in(Path(src), snap / name, files, f"extra_{i}_{name}")

        # criteria + mapping 状态（缺省捎带 P2/mapping.json 的指纹/副本）
        mapping = ws / "P2" / "mapping.json"
        if mapping.is_file():
            if mapping.stat().st_size <= _MAPPING_COPY_LIMIT:
                _copy_in(mapping, snap / "mapping.json", files, "mapping")
            else:
                files["mapping"] = {"sha256": _sha256(mapping),
                                    "size": mapping.stat().st_size,
                                    "mode": "hash-only"}

        manifest = {"n": n, "time": datetime.now().isoformat(
                        timespec="seconds"),
                    "source": source, "subject": subject, "reason": reason,
                    "files": files,
                    "kernel": _find_kernel(target_os),
                    "qemu_cmdline": _qemu_cmdline(runner, extra_env)}
        (snap / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8")
        append_event("snapshot", subject=subject, intent=source,
                     summary=f"failure-snapshot-{n}: {reason}", ws=ws,
                     mount=source)
        return snap
    except OSError:
        return None
