"""Durable task results and input changes shared by all migration phases.

Consumers require a successful upstream execution. Ordinary artifact changes are
recorded for agent review; approved acceptance criteria use their own gates.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from ..workspace import write_json


_ACTIVE: ContextVar["Execution | None"] = ContextVar("porter_handoff", default=None)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "task"


def fingerprints(paths: list[str | Path], *, allow_directories: bool = False) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in paths:
        path = Path(raw).resolve()
        if allow_directories and path.is_dir():
            entries = []
            for item in sorted(path.rglob("*")):
                if item.is_file():
                    entries.append((str(item.relative_to(path)), digest(item)))
            out[str(path)] = hashlib.sha256(
                json.dumps(entries, ensure_ascii=False).encode()).hexdigest()
        elif path.is_file():
            out[str(path)] = digest(path)
        else:
            raise ValueError(f"Handoff path is missing: {path}")
    return out


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    dependencies: tuple[str, ...] = ()
    materials: tuple[str | Path, ...] = ()
    description: str = ""


class Execution:
    def __init__(self, manager: "Manager", spec: TaskSpec):
        self.manager = manager
        self.spec = spec
        self.execution_id = f"{time.time_ns()}-{uuid.uuid4().hex[:8]}"
        self.directory = (manager.workspace / "handoffs" / "tasks" /
                          _slug(spec.task_id) / "executions" / self.execution_id)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.record_path = self.directory / "record.json"
        self.record = {
            "task_id": spec.task_id,
            "execution_id": self.execution_id,
            "status": "running",
            "description": spec.description,
            "dependencies": list(spec.dependencies),
            "materials": fingerprints(list(spec.materials), allow_directories=True)
                         if spec.materials else {},
            "providers": [],
        }
        self._save()
        (self.directory / "input.md").write_text(
            f"# Handoff input: {spec.task_id}\n\n"
            f"Execution: `{self.execution_id}`\n\n"
            + (spec.description.strip() + "\n" if spec.description.strip() else ""),
            encoding="utf-8")

    def _save(self) -> None:
        write_json(self.record_path, self.record)

    def record_provider(self, *, invocation_id: str, provider: str, session_id: str | None,
                        rc: int, log_path: Path, prompt_path: Path,
                        output: str = "", logical_task_id: str = "") -> None:
        item = {"invocation_id": invocation_id, "provider": provider,
                "session_id": session_id, "rc": rc,
                "logical_task_id": logical_task_id,
                "log": str(log_path), "prompt": str(prompt_path),
                "output_tail": (output or "")[-2000:]}
        self.record["providers"].append(item)
        self._save()

    def finish(self, *, ok: bool, summary: str, artifacts: list[str | Path],
               verification: list[str] | tuple[str, ...] = ()) -> Path:
        artifact_paths = [Path(p).resolve() for p in artifacts if Path(p).exists()]
        artifact_fps = fingerprints(artifact_paths, allow_directories=True)
        self.record.update(status="succeeded" if ok else "failed",
                           summary=summary, artifacts=artifact_fps,
                           verification=list(verification),
                           finished_at=time.time())
        self._save()
        name = "handoff.md" if ok else "handoff-fail.md"
        path = self.directory / name
        lines = [f"# Handoff: {self.spec.task_id}", "",
                 f"- status: `{self.record['status']}`",
                 f"- execution: `{self.execution_id}`", "", summary.strip()]
        if artifact_fps:
            lines += ["", "## Artifacts", ""] + [f"- `{p}` (`{v}`)"
                    for p, v in artifact_fps.items()]
        if verification:
            lines += ["", "## Verification", ""] + [f"- {v}" for v in verification]
        path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        _update_index(self.manager.workspace, self.spec.task_id, self, ok, path)
        return path


class Manager:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()


def _update_index(ws: Path, task_id: str, execution: Execution,
                  ok: bool, handoff: Path) -> None:
    task_dir = ws / "handoffs" / "tasks" / _slug(task_id)
    index_path = task_dir / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    except json.JSONDecodeError:
        index = {}
    index.setdefault("task_id", task_id)
    index.setdefault("executions", [])
    item = {"execution_id": execution.execution_id, "status": execution.record["status"],
            "handoff_sha256": digest(handoff),
            "handoff": str(handoff), "record": str(execution.record_path)}
    index["executions"].append(item)
    if ok:
        index["latest_success"] = item
    write_json(index_path, index)


def latest_success(ws: Path, task_id: str) -> dict | None:
    path = Path(ws).resolve() / "handoffs" / "tasks" / _slug(task_id) / "index.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    item = value.get("latest_success")
    if not isinstance(item, dict):
        return None
    record_path = Path(item.get("record", ""))
    if not record_path.is_absolute():
        record_path = (Path(ws) / record_path).resolve()
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(record, dict) or record.get("status") != "succeeded":
        return None
    return {**item, "record_data": record}


def require_success(ws: Path, task_id: str) -> dict:
    value = latest_success(ws, task_id)
    if value is None:
        raise ValueError(f"Required handoff is missing or not successful: {task_id}")
    changes = []
    current = {}
    artifacts = dict(value.get("record_data", {}).get("artifacts") or {})
    if value.get("handoff"):
        artifacts[value["handoff"]] = value.get("handoff_sha256")
    for name, expected in artifacts.items():
        path = Path(name)
        actual = (fingerprints([path], allow_directories=True).get(str(path.resolve()))
                  if path.exists() else None)
        # Commands append their own audit log during validation.
        if path.name == "runner.md" and actual is not None:
            current[name] = "present"
            continue
        current[name] = actual
        if actual != expected:
            changes.append({"path": name, "before": expected, "after": actual})
    value["changes"] = changes
    value["current_artifacts"] = current
    return value


def current_execution() -> Execution | None:
    return _ACTIVE.get()


def prepare_agent_prompt(prompt: str, *, new_session: bool = True) -> str:
    execution = current_execution()
    if execution is None or not new_session:
        return prompt
    return (prompt.rstrip() + "\n\n---\n\n## Handoff protocol\n"
            f"Task: `{execution.spec.task_id}`\n"
            f"Execution record: `{execution.record_path}`\n"
            "Review supplied handoffs and current workspace evidence; reconcile changes and report unresolved items.\n")


def run_task(workspace: Path, spec: TaskSpec, fn, *, success=None,
             summary=None, artifacts=None, verification=None):
    """Run one task and persist a success/failure handoff.

    The function's return value is deliberately preserved; callers can keep
    their existing business protocol while the handoff records the boundary.
    """
    manager = Manager(workspace)
    inputs = {dep: require_success(manager.workspace, dep) for dep in spec.dependencies}
    execution = Execution(manager, spec)
    execution.record["input_changes"] = {dep: item["changes"] for dep, item in inputs.items()}
    execution._save()
    token = _ACTIVE.set(execution)
    try:
        value = fn()
        ok = bool(success(value)) if success else True
        summary_text = str(summary(value) if summary else "Task completed.")
        artifact_values = artifacts(value) if callable(artifacts) else (artifacts or [])
        verification_values = verification(value) if callable(verification) else (verification or [])
        execution.finish(ok=ok, summary=summary_text,
                         artifacts=list(artifact_values),
                         verification=list(verification_values))
        return value
    except BaseException as exc:
        try:
            execution.finish(ok=False, summary=f"Execution failed: {exc}", artifacts=[],
                             verification=["failure recorded; cause is preserved"])
        finally:
            _ACTIVE.reset(token)
        raise
    finally:
        if _ACTIVE.get() is execution:
            _ACTIVE.reset(token)


def publish_handoff(workspace: Path, task_id: str, *, summary: str,
                    artifacts: list[str | Path] = (),
                    verification: list[str] = (),
                    dependencies: tuple[str, ...] = (),
                    materials: tuple[str | Path, ...] = ()) -> Path:
    """Publish a deterministic phase handoff without involving a provider."""
    box: dict[str, Path] = {}

    def done():
        return True

    run_task(workspace, TaskSpec(task_id, dependencies=dependencies,
                                 materials=materials), done,
             artifacts=lambda _v: list(artifacts),
             verification=lambda _v: verification,
             summary=lambda _v: summary)
    latest = latest_success(workspace, task_id)
    if latest is None:
        raise RuntimeError(f"Failed to publish handoff: {task_id}")
    return Path(latest["handoff"])


__all__ = ["TaskSpec", "Execution", "Manager", "run_task", "current_execution",
           "prepare_agent_prompt", "publish_handoff", "latest_success",
           "require_success", "fingerprints"]
