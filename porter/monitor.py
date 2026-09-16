"""Background task monitor for a Porter workspace.

The monitor is deliberately file based.  ``events.jsonl`` remains the source
of truth; ``taskboard.md`` and ``.monitor-state.json`` are derived state.  A
separate process makes the monitor useful when the Porter host is killed.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# ``start`` launches this module with ``-m``.  Keep direct execution useful for
# operators and tests too.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "porter"

from . import log
from .workspace import write_json


POLL_INTERVAL = 5.0
RECOVERY_BUDGET = 1
RECOVERY_TIMEOUT = 300
STATE_FILE = ".monitor-state.json"
TASKBOARD = "taskboard.md"
HUMAN_FILE = "HUMAN.md"
PID_FILE = ".monitor.pid"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(path: Path, default=None):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value
    except (OSError, json.JSONDecodeError):
        return default


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-")


def _path(ws: Path, value) -> Path | None:
    if not value:
        return None
    path = Path(str(value))
    return path if path.is_absolute() else ws / path


def _tail(path: Path | None, lines: int = 30) -> str:
    if path is None or not path.is_file():
        return ""
    try:
        return "\n".join(path.read_text(encoding="utf-8", errors="replace")
                         .splitlines()[-lines:])
    except OSError:
        return ""


def _status(value) -> str | None:
    """Map phase-specific ledger vocabulary to the monitor's small set."""
    if value is None:
        return None
    value = str(value)
    return {"delivered": "done", "pass": "done", "complete": "done",
            "budget-exhausted": "timeout", "interrupted": "timeout",
            "stalled": "blocked", "invalid": "fail",
            "invalid-deliverable": "fail", "unverified": "fail",
            "parked": "blocked"}.get(value, value)


def analyze_log(path: Path | str | None) -> dict:
    """Return timeout/block/error facts from one raw provider log."""
    path = Path(path) if path else None
    text = ""
    if path and path.is_file():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    lower = text.lower()
    session_id = None
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            value = event.get("sessionID") or event.get("session_id")
            if value:
                session_id = str(value)
    # Only the transport's standalone marker is evidence of a timeout.
    timeout = bool(re.search(r"^TIMEOUT(?: after [0-9.]+s)?$", text, re.MULTILINE))
    blocked = bool(re.search(r'"status"\s*:\s*"blocked"', lower))
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    reason = next((line for line in reversed(lines)
                   if any(word in line.lower() for word in
                          ("error", "failed", "failure", "blocked", "timeout"))),
                  (lines[-1] if lines else "no log output"))
    return {"timeout": timeout, "blocked": blocked, "session_id": session_id,
            "reason": reason[:400], "tail": "\n".join(lines[-30:])}


def _event_runs(ws: Path) -> list[dict]:
    """Pair agent events without requiring the query layer to know new fields."""
    # ponytail: full JSONL scan each poll; rotate/index events only if a large
    # workspace makes the simple durable source too slow.
    events = log.store.read_events(ws)
    starts: dict[str, dict] = {}
    output: list[dict] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("phase") == "monitor":
            continue
        key = str(event.get("run_id") or event.get("intent") or "")
        if not key:
            continue
        if event.get("kind") == "agent_start":
            starts[key] = event
            ref = event.get("ref") or {}
            output.append({
                "run_id": key,
                "task_id": event.get("task_id"),
                "phase": event.get("phase") or event.get("mount"),
                "module": event.get("module"),
                "step": event.get("step"),
                "attempt": event.get("attempt"),
                "session_id": event.get("session_id"),
                "log": ref.get("log") or f"{key}.log",
                "prompt": ref.get("prompt"),
                "time_start": event.get("time"),
                "rc": None,
                "summary": None,
            })
        elif event.get("kind") == "agent_end" and key in starts:
            starts.pop(key, None)
            match = next((r for r in reversed(output)
                          if r["run_id"] == key and r["rc"] is None), None)
            if match:
                match.update(rc=event.get("rc"), summary=event.get("summary"),
                             time_end=event.get("time"),
                             session_id=event.get("session_id") or
                             match.get("session_id"))
    return output


def _log_files(ws: Path) -> list[Path]:
    paths = []
    for path in ws.rglob("*.log"):
        # Command/gate logs are evidence, not sessions.  A provider session
        # always has the paired prompt archive; events cover older logs.
        if (path.is_file() and path.with_suffix(".prompt.md").is_file()
                and not any(part in (".git", "monitor-logs") for part in path.parts)):
            paths.append(path)
    return sorted(paths)


def _guess_log(ws: Path, phase: str, step: str, module: str | None = None) -> str | None:
    if phase == "exp-mono":
        prefix = "RES" if step == "research" else "MOD"
        candidates = sorted((ws / "exp-mono" / "logs").glob(
            f"{prefix}_{module}*.log" if module else f"{prefix}_*.log"), reverse=True)
        return str(candidates[0]) if candidates else None
    if phase == "accept":
        candidates = sorted((ws / "exp-accept" / "logs").glob("*.log"), reverse=True)
        return str(candidates[0]) if candidates else None
    return None


def _declared_tasks(ws: Path) -> list[dict]:
    """Read task/ledger entries so taskboard also shows runs without events."""
    result: list[dict] = []
    state = _json(ws / "prepare" / "state.json", {}) or {}
    for task in state.get("tasks", []) if isinstance(state, dict) else []:
        if not isinstance(task, dict):
            continue
        result.append({"task_id": str(task.get("id") or "prepare.task"),
                       "status": _status(task.get("status")),
                       "handoff": task.get("handoff"),
                       "session_id": task.get("session_id")})
    for phase, filename in (("exp-mono", "exp-mono/ledger.json"),
                            ("accept", "exp-accept/ledger.json")):
        ledger = _json(ws / filename, {}) or {}
        modules = ledger.get("modules", {}) if phase == "exp-mono" else {}
        for module, entry in modules.items():
            if not isinstance(entry, dict):
                continue
            for step in ("research", "translate"):
                sub = (entry if step == "translate" else entry.get(step)) or {}
                if isinstance(sub, dict) and sub:
                    result.append({"task_id": f"{phase}.{step}.{module}",
                                   "status": _status(sub.get("status")),
                                   "session_id": sub.get("session_id"),
                                   "handoff": None,
                                   "log": sub.get("log") or
                                   _guess_log(ws, phase, step, module)})
        for step, sub in (ledger.get("tiers", {}) or {}).items():
            if isinstance(sub, dict):
                result.append({"task_id": f"accept.{step}",
                               "status": _status(sub.get("status")),
                               "session_id": sub.get("session_id"),
                               "handoff": None})
        if isinstance(ledger.get("execute"), dict) and ledger["execute"]:
            sub = ledger["execute"]
            result.append({"task_id": "accept.execute",
                           "status": _status(sub.get("status")),
                           "session_id": sub.get("session_id"),
                           "handoff": None})
    return result


def _handoff_for(ws: Path, task_id: str, declared=None) -> Path | None:
    if declared:
        path = _path(ws, declared)
        if path and path.is_file():
            return path
    slug = _slug(task_id)
    candidates = list((ws / "handoffs").rglob("*.md")) + \
        list((ws / "prepare" / "handoffs").glob("*.md"))
    for path in sorted(set(candidates), reverse=True):
        if slug and (slug in path.name or slug in str(path)):
            return path
        try:
            if task_id and task_id in path.read_text(encoding="utf-8", errors="replace"):
                return path
        except OSError:
            continue
    return None


def _task_name(run: dict) -> str:
    if run.get("task_id"):
        return str(run["task_id"])
    bits = [run.get("phase"), run.get("step"), run.get("module")]
    label = ".".join(str(bit) for bit in bits if bit)
    return label or str(run.get("run_id") or "unknown")


def discover_tasks(ws: Path) -> list[dict]:
    """Build current task/session records from events, logs and ledgers."""
    ws = Path(ws).resolve()
    declared = {str(item["task_id"]): item for item in _declared_tasks(ws)
                if item.get("task_id")}
    runs = _event_runs(ws)
    seen_logs = {str(_path(ws, run.get("log")) or "") for run in runs}
    for path in _log_files(ws):
        if str(path) in seen_logs:
            continue
        info = analyze_log(path)
        # Logs outside known execution directories are still useful evidence,
        # but do not fabricate a task id when no provider event exists.
        runs.append({"run_id": str(path.relative_to(ws).with_suffix("")),
                     "task_id": None, "phase": None, "step": None,
                     "module": None, "attempt": None,
                     "session_id": info.get("session_id"), "log": str(path),
                     "prompt": str(path.with_suffix(".prompt.md")),
                     "time_start": None, "rc": None if not info["timeout"]
                     and not info["blocked"] else -1,
                     "summary": None, "untracked": True})
    # Events retain history; the taskboard and recovery use the latest run.
    latest = {_task_name(run): run for run in runs}
    records = []
    for run in latest.values():
        path = _path(ws, run.get("log"))
        info = analyze_log(path)
        rc = run.get("rc")
        if run.get("untracked"):
            status = "unknown"
        elif rc is None:
            status = "running"
        elif rc == 0:
            status = "done"
        elif rc in (-1, 124):
            status = "timeout"
        else:
            status = "fail"
        task_id = _task_name(run)
        decl = declared.get(task_id, {})
        if rc is not None and decl.get("status") in ("done", "fail", "blocked", "timeout"):
            status = decl["status"]
        handoff = _handoff_for(ws, task_id, decl.get("handoff"))
        if status == "done" and decl.get("handoff") and handoff is None:
            info["reason"] = "task completed without its declared handoff"
        records.append({
            "task_id": task_id,
            "run_id": run.get("run_id"),
            "status": status,
            "rc": rc,
            "session_id": run.get("session_id") or info.get("session_id") or
            decl.get("session_id"),
            "log": str(path) if path else run.get("log"),
            "prompt": str(_path(ws, run.get("prompt"))) if run.get("prompt") else None,
            "handoff": str(handoff or _path(ws, decl.get("handoff")))
            if (handoff or decl.get("handoff")) else None,
            "handoff_ok": handoff is not None,
            "handoff_required": bool(decl.get("handoff")),
            "reason": info.get("reason", ""),
            "tail": info.get("tail", ""),
            "time": run.get("time_end") or run.get("time_start"),
        })
    # Declared tasks with no events get a useful row.
    seen_ids = {r["task_id"] for r in records}
    for task_id, decl in declared.items():
        if task_id in seen_ids:
            continue
        declared_handoff = _handoff_for(ws, task_id, decl.get("handoff"))
        expected_handoff = declared_handoff or _path(ws, decl.get("handoff"))
        decl_info = analyze_log(_path(ws, decl.get("log")))
        decl_status = decl.get("status") or "pending"
        if decl_status == "pending" and decl_info.get("timeout"):
            decl_status = "timeout"
        elif decl_status == "pending" and decl_info.get("blocked"):
            decl_status = "blocked"
        records.append({"task_id": task_id, "run_id": None,
                        "status": decl_status, "rc": None,
                        "session_id": decl.get("session_id") or decl_info.get("session_id"),
                        "log": decl.get("log"),
                        "prompt": None,
                        "handoff": str(expected_handoff) if expected_handoff else None,
                        "handoff_ok": declared_handoff is not None,
                        "handoff_required": bool(decl.get("handoff")),
                        "reason": decl_info.get("reason", ""),
                        "tail": decl_info.get("tail", ""), "time": None})
    return records


def render_taskboard(records: list[dict], *, monitor_status: str = "running") -> str:
    counts: dict[str, int] = {}
    for item in records:
        status = str(item.get("status") or "pending")
        counts[status] = counts.get(status, 0) + 1
    progress = ", ".join(f"{name}={counts[name]}" for name in sorted(counts)) or "pending=0"
    lines = ["# Porter taskboard", "", f"- monitor: `{monitor_status}`",
             f"- updated: `{_now()}`", f"- progress: {progress}", "",
             "| Task | Status | Session | RC | Handoff | Reason |",
             "|---|---|---|---:|---|---|"]
    if not records:
        lines.append("| (no tasks observed) | pending | — | — | — | — |")
    for item in records:
        reason = str(item.get("reason") or "").replace("|", "/").replace("\n", " ")[:200]
        handoff = item.get("handoff") or ("missing" if item.get("status") == "done" else "—")
        if handoff and item.get("handoff") and not item.get("handoff_ok"):
            handoff = f"missing: {handoff}"
        lines.append("| {task} | {status} | {session} | {rc} | {handoff} | {reason} |".format(
            task=str(item.get("task_id") or "unknown").replace("|", "/"),
            status=item.get("status") or "pending",
            session=item.get("session_id") or "—", rc=item.get("rc") if item.get("rc") is not None else "—",
            handoff=str(handoff).replace("|", "/"), reason=reason or "—"))
    return "\n".join(lines) + "\n"


def write_taskboard(ws: Path, records: list[dict], *, monitor_status: str = "running") -> Path:
    path = Path(ws) / TASKBOARD
    path.parent.mkdir(parents=True, exist_ok=True)
    text = render_taskboard(records, monitor_status=monitor_status)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                     delete=False) as stream:
        stream.write(text)
        temporary = Path(stream.name)
    temporary.replace(path)
    return path


def _load_state(ws: Path) -> dict:
    value = _json(ws / STATE_FILE, {})
    return value if isinstance(value, dict) else {}


def _save_state(ws: Path, state: dict) -> None:
    write_json(Path(ws) / STATE_FILE, state)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        pid = int(pid)
        # kill(0) reports zombies as alive.  A crashed Porter process is often
        # briefly a zombie while its launcher reaps it, so inspect proc state.
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists():
            fields = stat.read_text(encoding="ascii", errors="ignore").split()
            if len(fields) > 2 and fields[2] == "Z":
                return False
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _workspace_busy(ws: Path) -> bool:
    """Whether the Porter host still owns the workspace lock."""
    lock_path = ws / ".porter.lock"
    if not lock_path.exists():
        return False
    try:
        with lock_path.open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(stream, fcntl.LOCK_UN)
            return False
    except (OSError, BlockingIOError):
        return True


class Monitor:
    """One polling monitor.  ``poll_once`` is public for deterministic tests."""

    def __init__(self, workspace: Path, *, interval: float = POLL_INTERVAL,
                 owner_pid: int | None = None, budget: int = RECOVERY_BUDGET,
                 recovery_timeout: int = RECOVERY_TIMEOUT):
        self.workspace = Path(workspace).resolve()
        self.interval = max(0.1, float(interval))
        self.owner_pid = owner_pid
        self.budget = max(0, int(budget))
        self.recovery_timeout = max(1, int(recovery_timeout))
        self.state = _load_state(self.workspace)
        self.state.setdefault("tasks", {})
        self.state.setdefault("human_mtime", 0.0)

    def _persist(self) -> None:
        self.state["updated_at"] = _now()
        _save_state(self.workspace, self.state)

    def _run_opencode(self, message: str, stem: Path, session_id: str | None = None) -> dict:
        target = _json(self.workspace / "project.json", {}) or {}
        workdir = Path(target.get("target_os") or self.workspace)
        model = os.environ.get("PORTER_MODEL", "zhipu-ai/glm-5.3-flash")
        args = ["opencode", "run", "--auto", "--format", "json", "--model", model,
                "--dir", str(workdir)]
        if session_id:
            args += ["--session", session_id]
        stem.parent.mkdir(parents=True, exist_ok=True)
        prompt_path = stem.with_suffix(".prompt.md")
        log_path = stem.with_suffix(".log")
        prompt_path.write_text(message, encoding="utf-8")
        log.append_event("agent_start", intent=str(stem), cmd=message,
                         summary=f"monitor recovery{(' session=' + session_id) if session_id else ''}",
                         ws=self.workspace, run_id=str(stem), ref={"log": str(log_path),
                         "prompt": str(prompt_path)}, phase="monitor", task_id=stem.name)
        try:
            proc = subprocess.run(args, cwd=str(workdir), input=message, text=True,
                                  capture_output=True, timeout=self.recovery_timeout,
                                  env={**os.environ, "NO_COLOR": "1",
                                       "PORTER_ROLE": "monitor",
                                       "PORTER_WORKSPACE": str(self.workspace),
                                       "PORTER_TARGET_OS_ROOT": str(workdir)})
            output, rc = (proc.stdout or "") + (proc.stderr or ""), proc.returncode
        except subprocess.TimeoutExpired as exc:
            output = "TIMEOUT\n" + str(exc)
            rc = 124
        except OSError as exc:
            output, rc = str(exc), 127
        log_path.write_text(output, encoding="utf-8")
        parsed = analyze_log(log_path)
        log.append_event("agent_end", intent=str(stem), rc=rc,
                         summary=parsed.get("reason"), ws=self.workspace,
                         run_id=str(stem), phase="monitor", task_id=stem.name,
                         session_id=parsed.get("session_id") or session_id)
        return {"rc": rc, "output": output, "session_id": parsed.get("session_id") or session_id,
                "blocked": parsed.get("blocked", False), "log": str(log_path)}

    def _prompt_for(self, item: dict) -> str:
        prompt = _path(self.workspace, item.get("prompt"))
        original = _tail(prompt, 200) if prompt else ""
        return ("Continue the unfinished Porter task. Read the recorded log and existing "
                "workspace artifacts, fix the root cause, and write the task's handoff "
                "when complete. Do not discard completed work.\n\n"
                f"Task: {item.get('task_id')}\nLog: {item.get('log')}\n"
                f"Failure analysis: {item.get('reason')}\n\n{original}")

    def _restart_host(self, session_id: str | None = None) -> None:
        """Resume the recorded Porter command after an out-of-process repair."""
        command = _json(self.workspace / ".porter-command.json", {}) or {}
        argv = command.get("argv")
        if not isinstance(argv, list) or not all(isinstance(v, str) for v in argv):
            return
        if session_id and argv and argv[0] in ("mono", "accept") and "--session" not in argv:
            argv = [*argv, "--session", session_id]
        env = {**os.environ, "PORTER_NO_MONITOR": "1"}
        (self.workspace / ".porter-exit.json").unlink(missing_ok=True)
        try:
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("main.py")), *argv],
                                       cwd=command.get("cwd") or str(Path.cwd()),
                                       env=env, start_new_session=True)
        except OSError:
            return
        self.owner_pid = process.pid

    def _write_human(self, item: dict, reason: str) -> None:
        path = self.workspace / HUMAN_FILE
        if path.exists():
            try:
                existing = path.read_text(encoding="utf-8", errors="replace")
                marker = "<!-- Write the answer or instruction below this line. -->"
                if "status: open" in existing and marker in existing \
                        and existing.split(marker, 1)[1].strip():
                    with path.open("a", encoding="utf-8") as stream:
                        stream.write(f"\n\n## Additional blocked task\n"
                                     f"`{item.get('task_id')}`: {reason}\n")
                    return
            except OSError:
                pass
        text = ("# HUMAN.md\n\nstatus: open\n\n"
                f"## Task\n`{item.get('task_id')}`\n\n"
                f"## Reason\n{reason}\n\n"
                f"## Evidence\n- log: `{item.get('log')}`\n"
                f"- session: `{item.get('session_id') or 'none'}`\n\n"
                "## Human prompt\n"
                "<!-- Write the answer or instruction below this line. -->\n")
        path.write_text(text, encoding="utf-8")

    def _recover(self, item: dict) -> None:
        key = str(item.get("task_id") or item.get("run_id"))
        record = self.state["tasks"].setdefault(key, {"attempts": 0})
        if record.get("status") in ("waiting-human", "human-applied"):
            item["status"] = record["status"]
            item["reason"] = record.get("reason") or item.get("reason", "")
            return
        attempts = record.setdefault("attempts", 0)
        if attempts >= self.budget:
            self._write_human(item, item.get("reason") or "automatic recovery budget exhausted")
            item["status"] = "waiting-human"
            record["status"] = item["status"]
            record["reason"] = item.get("reason") or "automatic recovery budget exhausted"
            return
        record["attempts"] = attempts + 1
        record["last_action"] = _now()
        if item["status"] == "timeout" and item.get("session_id") and not item.get("handoff_ok"):
            result = self._run_opencode(self._prompt_for(item),
                                        self.workspace / "monitor-logs" / f"resume-{_slug(key)}",
                                        item.get("session_id"))
            item["status"] = "resumed" if result["rc"] == 0 and not result.get("blocked") else "fail"
            item["reason"] = f"timeout: {item.get('reason')}; resume rc={result['rc']}"
            item["session_id"] = result.get("session_id") or item.get("session_id")
            record["status"] = item["status"]
            record["reason"] = item["reason"]
            if result["rc"] == 0 and not result.get("blocked"):
                self._restart_host(item.get("session_id"))
            return
        result = self._run_opencode(
            "Diagnose and fix this Porter task using its log and current artifacts. "
            "If it cannot be solved, explain the concrete human decision needed.\n\n"
            f"Task: {item.get('task_id')}\nLog: {item.get('log')}\n"
            f"Failure: {item.get('reason')}\n\n{item.get('tail', '')}",
            self.workspace / "monitor-logs" / f"debug-{_slug(key)}")
        if result["rc"] == 0 and not result.get("blocked"):
            item["status"] = "debugged"
            item["reason"] = "automatic debug completed; rerun the task to verify"
            self._restart_host(item.get("session_id"))
        else:
            item["status"] = "waiting-human"
            item["reason"] = f"automatic debug failed (rc={result['rc']}): {item.get('reason')}"
            self._write_human(item, item["reason"])
        record["status"] = item["status"]
        record["reason"] = item["reason"]

    def _consume_human(self) -> None:
        path = self.workspace / HUMAN_FILE
        try:
            stat = path.stat()
        except OSError:
            return
        if stat.st_mtime <= float(self.state.get("human_mtime", 0)):
            return
        text = path.read_text(encoding="utf-8", errors="replace")
        marker = "<!-- Write the answer or instruction below this line. -->"
        prompt = text.split(marker, 1)[1] if marker in text else ""
        prompt = re.sub(r"^\s*[-#]*\s*status\s*:.*$", "", prompt,
                        flags=re.IGNORECASE | re.MULTILINE).strip()
        if not prompt:
            self.state["human_mtime"] = stat.st_mtime
            return
        result = self._run_opencode(
            "Apply this human instruction to the current Porter workspace and continue "
            "the blocked task. Preserve evidence and write a handoff when complete.\n\n"
            + prompt, self.workspace / "monitor-logs" / "human")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"\n\n## Monitor response ({_now()})\n\n"
                         f"rc: `{result['rc']}`\n\n{result['output'][-2000:]}\n")
            stream.write("\nstatus: handled\n" if result["rc"] == 0 and not result.get("blocked")
                         else "\nstatus: open\n")
        self.state["human_mtime"] = path.stat().st_mtime
        if result["rc"] == 0 and not result.get("blocked"):
            for record in self.state["tasks"].values():
                if record.get("status") == "waiting-human":
                    # The host is restarted below; let its new session replace
                    # the old failed row on the next poll.
                    record.pop("status", None)
            self._restart_host()

    def poll_once(self) -> list[dict]:
        if not self.workspace.exists():
            return []
        # Each phase updates .porter-command.json.  A persistent monitor must
        # follow that phase's owner instead of retaining the previous PID.
        command = _json(self.workspace / ".porter-command.json", {}) or {}
        if isinstance(command, dict) and command.get("owner_pid"):
            try:
                recorded_pid = int(command["owner_pid"])
                # The child registers itself after launch; do not replace a
                # live recovery PID with the previous command's dead owner.
                if _pid_alive(recorded_pid) or not _pid_alive(self.owner_pid):
                    self.owner_pid = recorded_pid
            except (TypeError, ValueError):
                pass
        records = discover_tasks(self.workspace)
        owner_alive = _pid_alive(self.owner_pid) if self.owner_pid else False
        host_busy = _workspace_busy(self.workspace)
        host_stopped = (self.owner_pid is None or
                        (not owner_alive and not host_busy))
        exit_info = _json(self.workspace / ".porter-exit.json", {}) or {}
        clean_exit = host_stopped and exit_info.get("owner_pid") == self.owner_pid and bool(exit_info)
        for item in records:
            key = str(item.get("task_id") or item.get("run_id"))
            prior = self.state["tasks"].get(key, {})
            handoff_missing = (item["status"] == "done" and item.get("handoff_required")
                               and not item.get("handoff_ok"))
            if item["status"] == "done" and not handoff_missing:
                prior = {"attempts": 0, "run_id": item.get("run_id")}
                self.state["tasks"][key] = prior
            elif prior.get("run_id") != item.get("run_id"):
                prior = {"attempts": prior.get("attempts", 0), "run_id": item.get("run_id")}
                self.state["tasks"][key] = prior
            if prior.get("status") in ("waiting-human", "human-applied"):
                item["status"] = prior["status"]
                item["reason"] = prior.get("reason") or item.get("reason", "")
            if item["status"] == "running" and self.owner_pid and not owner_alive:
                item["status"] = "timeout"
                item["reason"] = "owner exited while this session had no agent_end event"
            if (item["status"] in ("timeout", "fail", "blocked") or handoff_missing) \
                    and host_stopped and not clean_exit:
                self._recover(item)
                # A restarted host owns all remaining work, even before locking.
                host_stopped = not _pid_alive(self.owner_pid) and not _workspace_busy(self.workspace)
        if host_stopped and not clean_exit:
            self._consume_human()
        self._persist()
        write_taskboard(self.workspace, records,
                        monitor_status="running" if host_busy else "detached")
        return records

    def run(self, *, once: bool = False) -> int:
        stop = False
        def _stop(_sig, _frame):
            nonlocal stop
            stop = True
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _stop)
        while not stop and self.workspace.exists():
            self.poll_once()
            if once:
                break
            exit_info = _json(self.workspace / ".porter-exit.json", {}) or {}
            if (self.owner_pid and exit_info.get("owner_pid") == self.owner_pid
                    and not _pid_alive(self.owner_pid)
                    and not _workspace_busy(self.workspace)):
                break
            time.sleep(self.interval)
        if self.workspace.exists():
            final_records = discover_tasks(self.workspace)
            final_status = ("waiting-human" if any(r.get("status") == "waiting-human"
                                                    for r in final_records)
                            else "stopped")
            write_taskboard(self.workspace, final_records, monitor_status=final_status)
        try:
            pid_path = self.workspace / PID_FILE
            if pid_path.read_text().strip() == str(os.getpid()):
                pid_path.unlink()
        except (OSError, ValueError):
            pass
        return 0


# Stable descriptive alias for callers that prefer the role name.
TaskMonitor = Monitor


def start(workspace: Path, *, owner_pid: int | None = None,
          interval: float = POLL_INTERVAL, budget: int = RECOVERY_BUDGET) -> subprocess.Popen:
    """Start the detached monitor used by the Porter CLI."""
    command = [sys.executable, "-m", "porter.monitor",
               "--workspace", str(Path(workspace).resolve()),
               "--owner-pid", str(owner_pid or os.getpid()),
               "--interval", str(interval), "--budget", str(budget)]
    process = subprocess.Popen(command, cwd=str(Path(__file__).resolve().parents[1]),
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True, close_fds=True,
                            env={**os.environ, "PORTER_MONITOR_CHILD": "1"})
    try:
        (Path(workspace) / PID_FILE).write_text(str(process.pid), encoding="utf-8")
    except OSError:
        pass
    return process


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="porter monitor")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--owner-pid", type=int)
    parser.add_argument("--interval", type=float, default=POLL_INTERVAL)
    parser.add_argument("--budget", type=int, default=RECOVERY_BUDGET)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    return Monitor(Path(args.workspace), owner_pid=args.owner_pid,
                   interval=args.interval, budget=args.budget).run(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
