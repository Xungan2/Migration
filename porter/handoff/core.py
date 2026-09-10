"""Durable task handoffs for Porter.

The module owns readiness, immutable execution identity, input delivery receipts,
and terminal records.  Scheduling stays with the phase orchestrators.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import socket
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence, TypeVar

try:  # POSIX is already required by Porter's bash based runner.
    import fcntl
except ImportError:  # pragma: no cover - Porter does not support Windows today.
    fcntl = None


SCHEMA_VERSION = 1
MAX_HANDOFF_CHARS = 24_000
MAX_AGENT_EXCERPT_CHARS = 2_000
MAX_HISTORY_INDEX = 100
MAX_PROVIDER_INDEX = 16
MAX_CHILD_INDEX = 32
MAX_ARTIFACT_INDEX = 32
MAX_DELIVERY_INDEX = 32
MAX_VERIFICATION_FACTS = 24
_CURRENT: contextvars.ContextVar["Execution | None"] = contextvars.ContextVar(
    "porter_handoff_execution", default=None)


class HandoffError(RuntimeError):
    """Base class for an invalid handoff operation."""


class NotReady(HandoffError):
    """A task cannot start because a required handoff/material is unavailable."""


class StaleInputs(HandoffError):
    """An input changed while the task was executing."""


@dataclass(frozen=True)
class Material:
    """A required path and whether it must remain unchanged during execution."""

    path: Path | str
    must_remain_unchanged: bool = False


@dataclass(frozen=True)
class TaskSpec:
    """Logical task identity and its explicitly declared inputs."""

    task_id: str
    dependencies: tuple[str, ...] = ()
    materials: tuple[Path | str | Material, ...] = ()
    description: str = ""
    include_failures: tuple[str, ...] = ()
    inherit_parent_inputs: bool = True

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,199}",
                            self.task_id):
            raise ValueError(f"invalid handoff task id: {self.task_id!r}")
        if self.task_id in self.dependencies:
            raise ValueError(f"task {self.task_id!r} depends on itself")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError(f"duplicate dependencies for {self.task_id!r}")


@dataclass
class Execution:
    """One attempt of a logical task.

    Callers may finish it exactly once.  The context manager records an explicit
    failure for exceptions, interrupts, and forgotten terminal calls.
    """

    manager: "HandoffManager"
    spec: TaskSpec
    execution_id: str
    directory: Path
    record: dict
    _token: contextvars.Token | None = field(default=None, repr=False)
    _finished: bool = field(default=False, repr=False)
    _parent: "Execution | None" = field(default=None, repr=False)

    @property
    def input_text(self) -> str:
        return (self.directory / "input.md").read_text(encoding="utf-8")

    def complete(self, summary: str, *, artifacts: Sequence[Path | str] = (),
                 verification: Sequence[str] = (), reused: bool = False) -> Path:
        return self.manager._finish(
            self, "success", summary=summary, artifacts=artifacts,
            verification=verification, reused=reused)

    def fail(self, reason: str, *, summary: str = "",
             artifacts: Sequence[Path | str] = (),
             verification: Sequence[str] = ()) -> Path:
        return self.manager._finish(
            self, "failed", summary=summary, reason=reason,
            artifacts=artifacts, verification=verification)

    def record_provider(self, *, invocation_id: str, provider: str,
                        session_id: str | None, rc: int, log_path: Path | str,
                        prompt_path: Path | str, output: str,
                        logical_task_id: str = "") -> None:
        self.manager._record_provider(
            self, invocation_id=invocation_id, provider=provider,
            session_id=session_id, rc=rc, log_path=log_path,
            prompt_path=prompt_path, output=output,
            logical_task_id=logical_task_id)


class HandoffManager:
    """The storage and validation implementation behind the handoff seam."""

    def __init__(self, workspace: Path | str):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / "handoffs"

    @contextmanager
    def start(self, spec: TaskSpec) -> Iterator[Execution]:
        execution = self.prepare(spec)
        token = _CURRENT.set(execution)
        execution._token = token
        original_error: BaseException | None = None
        try:
            yield execution
        except BaseException as exc:
            original_error = exc
            if not execution._finished:
                try:
                    execution.fail(
                        f"{type(exc).__name__}: {exc}" if str(exc)
                        else type(exc).__name__,
                        summary="Execution was interrupted before a business result was accepted.")
                except BaseException:
                    # Preserve the original task error. Recovery will detect the
                    # remaining running record on the next start.
                    pass
            raise
        finally:
            try:
                if not execution._finished and original_error is None:
                    execution.fail(
                        "execution left without a terminal business result",
                        summary="The host did not publish success; work status is unknown.")
            finally:
                _CURRENT.reset(token)

    def prepare(self, spec: TaskSpec) -> Execution:
        """Validate all inputs, snapshot their versions, and create an attempt."""
        self.root.mkdir(parents=True, exist_ok=True)
        with self._locked():
            self._recover_local_abandoned_locked(spec.task_id)
            parent = current_execution()
            dependencies = [self._required_success(task_id)
                            for task_id in spec.dependencies]
            self._check_cycle(spec.task_id, dependencies)
            failures = self._selected_failures(spec)
            materials = [self._material_ref(p) for p in spec.materials]

            # A child cannot depend on its still-running parent handoff.  It must,
            # however, receive the exact predecessor versions and original
            # materials with which that parent was prepared.  Copy those fixed
            # inputs into the child's own delivery plan so the provider receipt
            # proves what was actually sent.
            inherited_from_parent = None
            if parent is not None and spec.inherit_parent_inputs:
                parent_record = self._load_execution(parent)
                parent_stale = self._stale_inputs(parent_record)
                if parent_stale:
                    raise NotReady(
                        "parent task inputs became stale before child start: "
                        + "; ".join(parent_stale))
                inherited_from_parent = {
                    "task_id": parent.spec.task_id,
                    "execution_id": parent.execution_id,
                }
                known = {(r["task_id"], r["execution_id"])
                         for r in dependencies + failures}
                for ref in parent_record.get("inputs", {}).get("planned", []):
                    key = (str(ref["task_id"]), str(ref["execution_id"]))
                    if key in known:
                        continue
                    rec = self._record_for_delivery_ref(ref)
                    (dependencies if ref.get("status") == "success"
                     else failures).append(rec)
                    known.add(key)
                material_paths = {
                    str(r.get("absolute_path") or r["path"]): r for r in materials}
                for ref in parent_record.get("inputs", {}).get("materials", []):
                    path = str(ref.get("absolute_path") or ref["path"])
                    if path not in material_paths:
                        material_paths[path] = self._material_ref(Material(
                            path, bool(ref.get("must_remain_unchanged"))))
                materials = list(material_paths.values())
                # Inheritance can add a predecessor that was absent from the
                # child's explicit declaration. Re-run cycle validation after
                # the merge (for example, a child must not reuse the task id of
                # one of its parent's fixed predecessors).
                self._check_cycle(spec.task_id, dependencies)
            input_text = self._render_input(spec, dependencies, failures,
                                            materials)
            if len(input_text) > MAX_HANDOFF_CHARS:
                raise NotReady(
                    f"handoff input for {spec.task_id} is {len(input_text)} chars; "
                    f"limit is {MAX_HANDOFF_CHARS}. Split the dependency or keep its "
                    "handoff concise; input was not silently truncated")
            execution_id = self._new_execution_id()
            directory = self._task_dir(spec.task_id) / "executions" / execution_id
            directory.mkdir(parents=True, exist_ok=False)
            created = _now()
            inputs = {
                "planned": [self._delivery_ref(d) for d in dependencies + failures],
                "delivered": [],
                "materials": materials,
                "history_index": self._history_index(spec, dependencies, failures),
            }
            record = {
                "schema_version": SCHEMA_VERSION,
                "task_id": spec.task_id,
                "execution_id": execution_id,
                "description": spec.description,
                "status": "running",
                "created_at": created,
                "host": socket.gethostname(),
                "pid": os.getpid(),
                "finished_at": None,
                "dependencies": [d["task_id"] for d in dependencies],
                "inputs": inputs,
                "provider_runs": [],
                "parent_execution": ({"task_id": parent.spec.task_id,
                                      "execution_id": parent.execution_id}
                                     if parent is not None else None),
                "inherited_inputs_from": inherited_from_parent,
                "children": [],
                "artifacts": [],
                "verification": [],
                "summary": "",
                "reason": "",
                "historical_import": False,
                "reused": False,
            }
            _atomic_json(directory / "record.json", record)
            _atomic_text(directory / "input.md", input_text)
            _atomic_json(directory / "delivery.json", inputs)
            self._update_index(spec.task_id, record)
        return Execution(self, spec, execution_id, directory, record,
                         _parent=parent)

    def import_history(self, task_id: str, *, summary: str,
                       artifacts: Sequence[Path | str],
                       verification: Sequence[str]) -> Path:
        """Create an explicit handoff for real, pre-handoff artifacts.

        It is deliberately impossible to import an empty claim: at least one real
        artifact and one verification statement are required.
        """
        spec = TaskSpec(task_id=task_id, description="historical handoff import")
        refs = [self._artifact_ref(p) for p in artifacts]
        if not refs:
            raise HandoffError("historical import requires at least one artifact")
        if not summary.strip() or not [v for v in verification if str(v).strip()]:
            raise HandoffError("historical import requires summary and verification")
        self.root.mkdir(parents=True, exist_ok=True)
        with self._locked():
            self._recover_local_abandoned_locked(task_id)
            execution_id = self._new_execution_id()
            directory = self._task_dir(task_id) / "executions" / execution_id
            directory.mkdir(parents=True, exist_ok=False)
            now = _now()
            record = {
                "schema_version": SCHEMA_VERSION,
                "task_id": task_id,
                "execution_id": execution_id,
                "description": spec.description,
                "status": "success",
                "created_at": now,
                "host": socket.gethostname(),
                "pid": os.getpid(),
                "finished_at": now,
                "dependencies": [],
                "inputs": {"planned": [], "delivered": [], "materials": []},
                "provider_runs": [],
                "parent_execution": None,
                "children": [],
                "artifacts": refs,
                "verification": [str(v) for v in verification if str(v).strip()],
                "summary": summary.strip(),
                "reason": "",
                "historical_import": True,
                "reused": True,
            }
            _atomic_json(directory / "record.json", record)
            _atomic_text(directory / "input.md",
                         f"# Historical import for `{task_id}`\n\nNo upstream "
                         "handoffs were claimed.\n")
            doc = self._render_handoff(record)
            _atomic_text(directory / "handoff.md", doc)
            record["document"] = self._document_ref(directory / "handoff.md")
            _atomic_json(directory / "record.json", record)
            self._update_index(task_id, record)
            return directory / "handoff.md"

    def recover_abandoned(self, task_id: str, *, reason: str,
                          force: bool = False) -> list[Path]:
        """Turn orphaned running attempts into failure handoffs.

        A live local process is never recovered. Attempts owned by another host
        require ``force=True`` because their liveness cannot be established here.
        """
        if not reason.strip():
            raise HandoffError("recovery requires an explicit reason")
        recovered = []
        self.root.mkdir(parents=True, exist_ok=True)
        with self._locked():
            for record in self._running_records(task_id):
                if self._record_is_live(record):
                    raise NotReady(f"task {task_id!r} is active in pid {record.get('pid')}")
                if record.get("host") not in (None, socket.gethostname()) and not force:
                    raise NotReady(
                        f"task {task_id!r} belongs to host {record.get('host')!r}; "
                        "explicit forced recovery is required")
                recovered.append(self._recover_record_locked(record, reason))
        return recovered

    def inspect(self, task_id: str | None = None) -> list[dict]:
        """Return immutable record snapshots, newest first."""
        if task_id:
            dirs = [self._task_dir(task_id)]
        else:
            dirs = [p for p in (self.root / "tasks").glob("*") if p.is_dir()]
        records: list[dict] = []
        for task_dir in dirs:
            for path in (task_dir / "executions").glob("*/record.json"):
                try:
                    records.append(_read_json(path))
                except (OSError, json.JSONDecodeError, ValueError):
                    records.append({"status": "corrupt", "record": str(path)})
        return sorted(records, key=lambda r: str(r.get("created_at", "")),
                      reverse=True)

    def require_success(self, task_id: str, *,
                        current_artifacts: Sequence[Path | str] = ()) -> dict:
        """Validate and return the current successful version of ``task_id``.

        ``current_artifacts`` is an opt-in现场 consistency check for cached
        control paths.  Normal dependency validity remains version based because
        shared files can be changed legitimately by later consumers.
        """
        with self._locked():
            record = self._required_success(task_id)
            try:
                by_path = {
                    str(Path(a.get("absolute_path") or a["path"]).resolve()): a
                    for a in record.get("artifacts", [])
                    if isinstance(a, dict)
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise NotReady(
                    f"artifact index for {task_id!r} is invalid: {exc}") from exc
            for item in current_artifacts:
                current = self._artifact_ref(item)
                key = str(Path(current["absolute_path"]).resolve())
                saved = by_path.get(key)
                if saved is None:
                    raise NotReady(
                        f"handoff for {task_id!r} did not record required artifact {key}")
                if saved.get("fingerprint") != current.get("fingerprint"):
                    raise NotReady(
                        f"cached artifact for {task_id!r} changed: {key}")
            return dict(record)

    def record_delivery(self, execution: Execution) -> None:
        """Record the exact handoff versions embedded in a new provider session."""
        with self._locked():
            record = self._load_execution(execution)
            self._ensure_running(record)
            planned = list(record["inputs"]["planned"])
            delivered = list(record["inputs"].get("delivered", []))
            known = {(d["task_id"], d["execution_id"], d["sha256"])
                     for d in delivered}
            for ref in planned:
                key = (ref["task_id"], ref["execution_id"], ref["sha256"])
                if key not in known:
                    delivered.append({**ref, "delivered_at": _now()})
            record["inputs"]["delivered"] = delivered
            self._write_execution(execution, record)
            _atomic_json(execution.directory / "delivery.json", record["inputs"])

    def _record_provider(self, execution: Execution, **facts) -> None:
        with self._locked():
            record = self._load_execution(execution)
            self._ensure_running(record)
            output = str(facts.pop("output", "") or "")
            invocation_id = str(facts.get("invocation_id"))
            evidence_dir = execution.directory / "provider" / invocation_id
            evidence_dir.mkdir(parents=True, exist_ok=False)
            original_log = Path(str(facts.pop("log_path")))
            original_prompt = Path(str(facts.pop("prompt_path")))
            if original_log.is_file():
                raw = original_log.read_bytes()
            else:
                raw = output.encode("utf-8", "replace")
            if original_prompt.is_file():
                prompt_raw = original_prompt.read_bytes()
            else:
                prompt_raw = b""
            snapshot_log = evidence_dir / "raw.log"
            snapshot_prompt = evidence_dir / "prompt.md"
            _atomic_bytes(snapshot_log, raw)
            _atomic_bytes(snapshot_prompt, prompt_raw)
            summary = _agent_summary(output)
            omitted = max(0, len(output) - len(summary))
            run = {
                **facts,
                "log_path": self._display_path(snapshot_log),
                "prompt_path": self._display_path(snapshot_prompt),
                "original_log_path": self._display_path(original_log),
                "original_prompt_path": self._display_path(original_prompt),
                "log_sha256": _sha_file(snapshot_log),
                "prompt_sha256": _sha_file(snapshot_prompt),
                "recorded_at": _now(),
                "output_sha256": _sha_text(output),
                "agent_summary": summary,
                "summary_omitted_chars": omitted,
            }
            record["provider_runs"].append(run)
            self._write_execution(execution, record)

    def _finish(self, execution: Execution, status: str, *, summary: str,
                reason: str = "", artifacts: Sequence[Path | str] = (),
                verification: Sequence[str] = (), reused: bool = False) -> Path:
        if execution._finished:
            raise HandoffError(f"execution {execution.execution_id} already finished")
        if status not in ("success", "failed"):
            raise ValueError(status)
        requested_status = status
        with self._locked():
            record = self._load_execution(execution)
            if record.get("status") != "running":
                raise HandoffError(f"execution is already {record.get('status')}")
            stale = self._stale_inputs(record)
            if stale:
                status = "failed"
                reason = "inputs changed during execution: " + "; ".join(stale)
            refs = []
            missing = []
            for item in artifacts:
                try:
                    refs.append(self._artifact_ref(item))
                except NotReady as exc:
                    missing.append(str(exc))
            if status == "success" and missing:
                status = "failed"
                reason = "declared output artifact missing: " + "; ".join(missing)
            # Missing outputs are a publication error only when the caller tried
            # to publish success.  A rejected business result still returns its
            # original rc/value after its failure handoff truthfully records the
            # missing evidence.
            publication_error = requested_status == "success" and bool(missing)
            if status == "failed" and missing and "declared output artifact" not in reason:
                suffix = "declared output artifact missing: " + "; ".join(missing)
                reason = f"{reason}; {suffix}" if reason else suffix
            record.update({
                "status": status,
                "finished_at": _now(),
                "summary": summary.strip(),
                "reason": reason.strip(),
                "artifacts": refs,
                "verification": [str(v) for v in verification if str(v).strip()],
                "reused": bool(reused),
            })
            name = "handoff.md" if status == "success" else "handoff-fail.md"
            path = execution.directory / name
            _atomic_text(path, self._render_handoff(record))
            record["document"] = self._document_ref(path)
            self._write_execution(execution, record)
            self._update_index(execution.spec.task_id, record)
            execution.record = record
            execution._finished = True
            parent = execution._parent
            if (parent is not None and parent is not execution and
                    not parent._finished):
                parent_record = self._load_execution(parent)
                parent_record.setdefault("children", []).append({
                    "task_id": record["task_id"],
                    "execution_id": record["execution_id"],
                    "status": record["status"],
                    "document": record["document"],
                })
                self._write_execution(parent, parent_record)
            if stale:
                raise StaleInputs(reason)
            if publication_error:
                raise HandoffError(reason)
            return path

    def _required_success(self, task_id: str) -> dict:
        running = self._running_records(task_id)
        if running:
            raise NotReady(
                f"task {task_id!r} has a newer running execution "
                f"{running[-1].get('execution_id')}; its previous success cannot "
                "be consumed concurrently")
        index = self._read_index(task_id)
        eid = index.get("latest_success") if index else None
        if not eid:
            raise NotReady(
                f"task is not ready: required handoff for {task_id!r} is missing")
        record_path = self._task_dir(task_id) / "executions" / eid / "record.json"
        try:
            record = _read_json(record_path)
            self._validate_terminal_record(
                record, expected_task=task_id, expected_status="success",
                expected_execution=eid)
            self._validate_current_dependency_tree(record, set())
        except (OSError, json.JSONDecodeError, ValueError, HandoffError) as exc:
            raise NotReady(f"required handoff for {task_id!r} is invalid: {exc}")
        return record

    def _selected_failures(self, spec: TaskSpec) -> list[dict]:
        task_ids = (spec.task_id,) + spec.include_failures
        result = []
        for task_id in dict.fromkeys(task_ids):
            index = self._read_index(task_id)
            eid = index.get("latest_failure") if index else None
            if not eid:
                continue
            path = self._task_dir(task_id) / "executions" / eid / "record.json"
            try:
                record = _read_json(path)
                self._validate_terminal_record(
                    record, expected_task=task_id, expected_status="failed",
                    expected_execution=eid)
            except (OSError, json.JSONDecodeError, ValueError, HandoffError) as exc:
                raise NotReady(f"failure handoff for {task_id!r} is invalid: {exc}")
            result.append(record)
        return result

    def _validate_terminal_record(self, record: dict, *, expected_task: str,
                                  expected_status: str,
                                  expected_execution: str | None = None) -> None:
        if not isinstance(record, dict):
            raise HandoffError("record is not an object")
        if record.get("schema_version") != SCHEMA_VERSION:
            raise HandoffError("unsupported schema version")
        if record.get("task_id") != expected_task:
            raise HandoffError("task identity mismatch")
        execution_id = record.get("execution_id")
        if not isinstance(execution_id, str) or not execution_id:
            raise HandoffError("execution identity is missing")
        if expected_execution is not None and execution_id != expected_execution:
            raise HandoffError("execution identity does not match its index")
        if record.get("status") != expected_status:
            raise HandoffError(f"status is {record.get('status')!r}")
        inputs = record.get("inputs")
        if not isinstance(inputs, dict):
            raise HandoffError("inputs is not an object")
        for field in ("planned", "delivered", "materials"):
            if not isinstance(inputs.get(field, []), list):
                raise HandoffError(f"inputs.{field} is not a list")
        for ref in inputs.get("planned", []):
            if not isinstance(ref, dict):
                raise HandoffError("planned input is not an object")
            if not all(isinstance(ref.get(k), str) and ref.get(k)
                       for k in ("task_id", "execution_id", "status", "sha256")):
                raise HandoffError("planned input has missing identity/version fields")
            if ref["status"] not in ("success", "failed"):
                raise HandoffError("planned input has invalid status")
        doc = record.get("document") or {}
        if not isinstance(doc, dict) or not isinstance(doc.get("sha256"), str):
            raise HandoffError("document reference is malformed")
        path = Path(str(doc.get("path", "")))
        if doc.get("absolute_path"):
            path = Path(str(doc["absolute_path"]))
        if not path.is_absolute():
            path = self.workspace / path
        if not path.is_file():
            raise HandoffError("handoff document is missing")
        if _sha_file(path) != doc.get("sha256"):
            raise HandoffError("handoff document hash mismatch")

    def _validate_current_dependency_tree(self, record: dict,
                                          seen: set[tuple[str, str]]) -> None:
        """Reject a success whose declared predecessor version was superseded."""
        key = (str(record["task_id"]), str(record["execution_id"]))
        if key in seen:
            return
        seen.add(key)
        running = self._running_records(str(record["task_id"]))
        if running:
            raise HandoffError(
                f"dependency {record['task_id']!r} has a running newer attempt")
        success_refs = [r for r in record.get("inputs", {}).get("planned", [])
                        if r.get("status") == "success"]
        for ref in success_refs:
            task_id = str(ref["task_id"])
            expected = str(ref["execution_id"])
            index = self._read_index(task_id)
            if index.get("latest_success") != expected:
                raise HandoffError(
                    f"dependency {task_id!r} was superseded after this handoff")
            path = self._task_dir(task_id) / "executions" / expected / "record.json"
            upstream = _read_json(path)
            self._validate_terminal_record(
                upstream, expected_task=task_id, expected_status="success",
                expected_execution=expected)
            if upstream["document"]["sha256"] != ref["sha256"]:
                raise HandoffError(f"dependency {task_id!r} version hash mismatch")
            self._validate_current_dependency_tree(upstream, seen)

    def _record_for_delivery_ref(self, ref: dict) -> dict:
        """Load and verify one exact input version without selecting a newer one."""
        try:
            task_id = str(ref["task_id"])
            execution_id = str(ref["execution_id"])
            status = str(ref["status"])
            path = (self._task_dir(task_id) / "executions" / execution_id /
                    "record.json")
            record = _read_json(path)
            self._validate_terminal_record(
                record, expected_task=task_id, expected_status=status,
                expected_execution=execution_id)
            if record["document"]["sha256"] != ref["sha256"]:
                raise HandoffError("document version hash mismatch")
            return record
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError,
                HandoffError) as exc:
            label = locals().get("task_id", "unknown")
            raise NotReady(
                f"fixed handoff version for {label!r} is invalid: {exc}") from exc

    def _check_cycle(self, task_id: str, dependencies: Sequence[dict]) -> None:
        graph: dict[str, list[str]] = {task_id: [d["task_id"] for d in dependencies]}
        pending = list(graph[task_id])
        while pending:
            node = pending.pop()
            if node in graph:
                continue
            idx = self._read_index(node)
            eid = idx.get("latest_success") if idx else None
            if not eid:
                graph[node] = []
                continue
            rec = _read_json(self._task_dir(node) / "executions" / eid /
                             "record.json")
            graph[node] = list(rec.get("dependencies") or [])
            pending.extend(graph[node])
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visiting:
                raise NotReady(f"handoff dependency cycle contains {node!r}")
            if node in visited:
                return
            visiting.add(node)
            for dep in graph.get(node, []):
                visit(dep)
            visiting.remove(node)
            visited.add(node)

        visit(task_id)

    def _running_records(self, task_id: str) -> list[dict]:
        index = self._read_index(task_id)
        records = []
        for execution_id in index.get("executions", []):
            path = self._task_dir(task_id) / "executions" / execution_id / "record.json"
            try:
                record = _read_json(path)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise NotReady(f"cannot inspect execution {execution_id}: {exc}")
            if record.get("status") == "running":
                records.append(record)
        return records

    def _recover_local_abandoned_locked(self, task_id: str) -> None:
        for record in self._running_records(task_id):
            if self._record_is_live(record):
                raise NotReady(
                    f"task {task_id!r} already has active execution "
                    f"{record.get('execution_id')} (pid {record.get('pid')})")
            if record.get("host") not in (None, socket.gethostname()):
                raise NotReady(
                    f"task {task_id!r} has an unresolved running execution on "
                    f"host {record.get('host')!r}; use explicit recovery")
            self._recover_record_locked(
                record, "host process ended without publishing a terminal result")

    @staticmethod
    def _record_is_live(record: dict) -> bool:
        if record.get("host") not in (None, socket.gethostname()):
            return False
        try:
            pid = int(record.get("pid"))
        except (TypeError, ValueError):
            return False
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _recover_record_locked(self, record: dict, reason: str) -> Path:
        task_id = str(record["task_id"])
        execution_id = str(record["execution_id"])
        directory = self._task_dir(task_id) / "executions" / execution_id
        record.update({
            "status": "failed",
            "finished_at": _now(),
            "summary": ("Unknown: the process ended before an agent or host semantic "
                        "summary was durably published."),
            "reason": reason.strip(),
            "verification": [
                "Host recovery observed that the recorded process was no longer active.",
                "No unrecorded work or validation result was inferred.",
            ],
        })
        path = directory / "handoff-fail.md"
        _atomic_text(path, self._render_handoff(record))
        record["document"] = self._document_ref(path)
        _atomic_json(directory / "record.json", record)
        self._update_index(task_id, record)
        return path

    def _stale_inputs(self, record: dict) -> list[str]:
        stale = []
        for ref in record["inputs"]["planned"]:
            task_id = ref["task_id"]
            eid = ref["execution_id"]
            path = self._task_dir(task_id) / "executions" / eid / "record.json"
            try:
                current = _read_json(path)
                self._validate_terminal_record(
                    current, expected_task=task_id,
                    expected_status=ref["status"], expected_execution=eid)
                doc = current["document"]
                if doc["sha256"] != ref["sha256"]:
                    stale.append(f"{task_id} document changed")
                if ref["status"] == "success":
                    idx = self._read_index(task_id)
                    if idx.get("latest_success") != eid:
                        stale.append(f"{task_id} has a newer successful version")
                    # A direct predecessor can remain latest while one of its
                    # own fixed inputs is superseded.  Revalidate the full tree
                    # at completion as well as at initial readiness selection.
                    self._validate_current_dependency_tree(current, set())
            except Exception as exc:
                stale.append(f"{task_id} invalid ({exc})")
        for ref in record["inputs"]["materials"]:
            if not ref.get("must_remain_unchanged"):
                continue
            try:
                current = self._material_ref(Material(
                    ref.get("absolute_path") or ref["path"], True))
                if current["fingerprint"] != ref["fingerprint"]:
                    stale.append(f"material {ref['path']} changed")
            except NotReady as exc:
                stale.append(str(exc))
        return stale

    def _render_input(self, spec: TaskSpec, dependencies: Sequence[dict],
                      failures: Sequence[dict], materials: Sequence[dict]) -> str:
        lines = [
            f"# Required inputs for `{spec.task_id}`", "",
            f"Workspace root: `{self.workspace}`. Relative paths in embedded "
            "handoffs resolve from this directory.", "",
            "Read every document and material listed below before doing task work.",
            "The handoff bodies are injected in full. Provider prompts and raw logs",
            "are copied evidence; code and artifacts remain at recorded paths with",
            "their publication-time fingerprints.", "",
        ]
        if materials:
            lines.extend(["## Original required materials", ""])
            for ref in materials:
                lines.append(f"- `{ref['absolute_path']}` ({ref['kind']}, "
                             f"fingerprint `{ref['fingerprint']}`)")
            lines.append("")
        if not dependencies:
            lines.extend(["## Required predecessor handoffs", "", "None.", ""])
        for rec in dependencies:
            lines.extend(self._embedded_document(rec, "Required predecessor handoff"))
        for rec in failures:
            title = ("Most recent failed attempt for this task" if
                     rec["task_id"] == spec.task_id else
                     "Explicitly promoted failure handoff")
            lines.extend(self._embedded_document(rec, title))
        history = self._history_index(spec, dependencies, failures)
        if history:
            lines.extend(["## Related history index (not embedded as required body)", ""])
            for ref in history[:MAX_HISTORY_INDEX]:
                lines.append(f"- `{ref['task_id']}` / `{ref['execution_id']}` / "
                             f"`{ref['status']}`: `{ref['absolute_path']}` "
                             f"sha256 `{ref['sha256']}`")
            if len(history) > MAX_HISTORY_INDEX:
                lines.append(f"- {len(history) - MAX_HISTORY_INDEX} older entries "
                             "omitted from this bounded index; inspect task indexes "
                             f"under `{self.root / 'tasks'}`.")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _embedded_document(self, record: dict, title: str) -> list[str]:
        doc = record["document"]
        path = Path(doc.get("absolute_path") or doc["path"])
        if not path.is_absolute():
            path = self.workspace / path
        body = path.read_text(encoding="utf-8")
        return [f"## {title}: `{record['task_id']}`", "",
                f"Document `{record['execution_id']}` / `{doc['sha256']}`:", "",
                body.rstrip(), ""]

    def _render_handoff(self, record: dict) -> str:
        success = record["status"] == "success"
        title = "Handoff" if success else "Failed handoff"
        lines = [
            f"# {title}: `{record['task_id']}`", "",
            f"- Execution: `{record['execution_id']}`",
            f"- Status: `{record['status']}`",
            f"- Started: `{record['created_at']}`",
            f"- Finished: `{record['finished_at']}`",
            f"- Historical import: `{str(record.get('historical_import', False)).lower()}`",
            f"- Reused existing artifacts: `{str(record.get('reused', False)).lower()}`",
            "", "## Semantic work summary", "",
            _bounded_text(record.get("summary") or
            "Unknown: no semantic work summary was available from the execution.",
                          4_000, "semantic summary"),
        ]
        if not success:
            lines.extend(["", "## Failure", "", _bounded_text(
                record.get("reason") or
                "Unknown: the host observed failure without a reason.",
                2_000, "failure reason")])
        lines.extend(["", "## Dependency versions delivered", ""])
        delivered = record["inputs"].get("delivered") or []
        if delivered:
            for ref in delivered[-MAX_DELIVERY_INDEX:]:
                lines.append(f"- `{ref['task_id']}`: `{ref['execution_id']}` / "
                             f"`{ref['sha256']}`")
            if len(delivered) > MAX_DELIVERY_INDEX:
                lines.append(
                    f"- {len(delivered) - MAX_DELIVERY_INDEX} earlier delivery "
                    "receipts omitted; see record.json.")
        else:
            lines.append("None.")
        lines.extend(["", "## Verification facts", ""])
        facts = record.get("verification") or []
        lines.extend(f"- {_bounded_text(str(fact), 500, 'verification fact')}"
                     for fact in facts[:MAX_VERIFICATION_FACTS])
        if len(facts) > MAX_VERIFICATION_FACTS:
            lines.append(f"- {len(facts) - MAX_VERIFICATION_FACTS} verification "
                         "facts omitted; see record.json.")
        if not facts:
            lines.append("- Unknown: no verification fact was recorded.")
        lines.extend(["", "## Artifacts and evidence", ""])
        artifacts = record.get("artifacts") or []
        for ref in artifacts[:MAX_ARTIFACT_INDEX]:
            lines.append(f"- `{ref.get('absolute_path') or ref['path']}` "
                         f"({ref['kind']}, `{ref['fingerprint']}`)")
        if len(artifacts) > MAX_ARTIFACT_INDEX:
            lines.append(f"- {len(artifacts) - MAX_ARTIFACT_INDEX} artifact pointers "
                         "omitted; see record.json.")
        provider_runs = record.get("provider_runs") or []
        visible_runs = provider_runs[-MAX_PROVIDER_INDEX:]
        if len(provider_runs) > len(visible_runs):
            lines.append(f"- {len(provider_runs) - len(visible_runs)} older provider "
                         "runs omitted from this bounded index; see record.json.")
        excerpt_ids = {r.get("invocation_id") for r in provider_runs[-3:]}
        for run in visible_runs:
            session = f", session `{run['session_id']}`" if run.get("session_id") else ""
            lines.append(f"- Provider run `{run['invocation_id']}` for auxiliary "
                         f"task `{run.get('logical_task_id') or 'unspecified'}` "
                         f"(rc `{run['rc']}`"
                         f"{session}); log `{run['log_path']}`; prompt "
                         f"`{run['prompt_path']}`; output sha256 "
                         f"`{run['output_sha256']}`")
            if run.get("agent_summary") and run.get("invocation_id") in excerpt_ids:
                lines.extend(["", "  Agent-authored output excerpt:", "",
                              "  > " + run["agent_summary"].replace("\n", "\n  > ")])
                if run.get("summary_omitted_chars"):
                    lines.append(f"  > [Full output at log pointer; "
                                 f"{run['summary_omitted_chars']} characters omitted "
                                 "from this concise handoff.]" )
        if not artifacts and not record.get("provider_runs"):
            lines.append("- None recorded.")
        children = record.get("children") or []
        if children:
            lines.extend(["", "## Child executions", ""])
            for child in children[-MAX_CHILD_INDEX:]:
                lines.append(f"- `{child['task_id']}` / `{child['execution_id']}` "
                             f"status `{child['status']}`; document "
                             f"`{child['document']['path']}` / "
                             f"`{child['document']['sha256']}`")
            if len(children) > MAX_CHILD_INDEX:
                lines.append(f"- {len(children) - MAX_CHILD_INDEX} earlier child "
                             "executions omitted; see record.json.")
        return "\n".join(lines).rstrip() + "\n"

    def _history_index(self, spec: TaskSpec, dependencies: Sequence[dict],
                       failures: Sequence[dict]) -> list[dict]:
        """Compact pointers to older failures and transitive upstream handoffs."""
        required = {(r["task_id"], r["execution_id"])
                    for r in dependencies + failures}
        refs: list[dict] = []
        task_ids = {spec.task_id}
        pending = list(dependencies)
        visited: set[tuple[str, str]] = set()
        while pending:
            rec = pending.pop()
            rec_key = (str(rec.get("task_id")), str(rec.get("execution_id")))
            if rec_key in visited:
                continue
            visited.add(rec_key)
            for ref in rec.get("inputs", {}).get("planned", []):
                task_ids.add(str(ref["task_id"]))
                if ref.get("status") != "success":
                    continue
                path = (self._task_dir(str(ref["task_id"])) / "executions" /
                        str(ref["execution_id"]) / "record.json")
                try:
                    pending.append(_read_json(path))
                except (OSError, json.JSONDecodeError, ValueError):
                    continue
        for task_id in sorted(task_ids):
            index = self._read_index(task_id)
            for execution_id in index.get("executions", []):
                if (task_id, execution_id) in required:
                    continue
                path = (self._task_dir(task_id) / "executions" / execution_id /
                        "record.json")
                try:
                    rec = _read_json(path)
                except (OSError, json.JSONDecodeError, ValueError):
                    continue
                if rec.get("status") not in ("success", "failed") or not rec.get("document"):
                    continue
                doc = rec["document"]
                refs.append({"task_id": task_id, "execution_id": execution_id,
                             "status": rec["status"], "sha256": doc["sha256"],
                             "path": doc["path"],
                             "absolute_path": doc.get("absolute_path") or
                             str((self.workspace / doc["path"]).resolve())})
        refs.sort(key=lambda r: r["execution_id"], reverse=True)
        return refs

    def _material_ref(self, item: Path | str | Material) -> dict:
        immutable = item.must_remain_unchanged if isinstance(item, Material) else False
        raw_path = item.path if isinstance(item, Material) else item
        path = Path(raw_path)
        if not path.is_absolute():
            path = self.workspace / path
        path = path.resolve()
        if not path.exists():
            raise NotReady(f"required material is missing: {path}")
        if path.is_file():
            fingerprint = _sha_file(path)
            kind = "file"
        elif path.is_dir():
            stat = path.stat()
            fingerprint = _sha_text(f"{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}")
            kind = "directory-pointer"
        else:
            raise NotReady(f"required material has unsupported type: {path}")
        return {"path": self._display_path(path), "absolute_path": str(path),
                "kind": kind, "fingerprint": fingerprint,
                "must_remain_unchanged": immutable}

    def _artifact_ref(self, item: Path | str) -> dict:
        return self._material_ref(item)

    def _document_ref(self, path: Path) -> dict:
        return {"path": self._display_path(path), "absolute_path": str(path.resolve()),
                "sha256": _sha_file(path),
                "bytes": path.stat().st_size}

    def _delivery_ref(self, record: dict) -> dict:
        doc = record["document"]
        return {"task_id": record["task_id"],
                "execution_id": record["execution_id"],
                "status": record["status"], "sha256": doc["sha256"],
                "path": doc["path"]}

    def _display_path(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.workspace))
        except ValueError:
            return str(path.resolve())

    def _task_dir(self, task_id: str) -> Path:
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_id).strip("_.") or "task"
        digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:10]
        return self.root / "tasks" / f"{slug[:80]}--{digest}"

    def _read_index(self, task_id: str) -> dict:
        path = self._task_dir(task_id) / "index.json"
        if not path.exists():
            return {}
        try:
            index = _read_json(path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise NotReady(f"handoff index for {task_id!r} is corrupt: {exc}")
        if index.get("task_id") != task_id:
            raise NotReady(f"handoff index identity mismatch for {task_id!r}")
        return index

    def _update_index(self, task_id: str, record: dict) -> None:
        directory = self._task_dir(task_id)
        directory.mkdir(parents=True, exist_ok=True)
        index = self._read_index(task_id) or {
            "schema_version": SCHEMA_VERSION, "task_id": task_id,
            "executions": [], "latest_success": None, "latest_failure": None,
        }
        if record["execution_id"] not in index["executions"]:
            index["executions"].append(record["execution_id"])
        if record["status"] == "success":
            index["latest_success"] = record["execution_id"]
        elif record["status"] == "failed":
            index["latest_failure"] = record["execution_id"]
        _atomic_json(directory / "index.json", index)

    def _load_execution(self, execution: Execution) -> dict:
        return _read_json(execution.directory / "record.json")

    def _write_execution(self, execution: Execution, record: dict) -> None:
        _atomic_json(execution.directory / "record.json", record)
        execution.record = record

    @staticmethod
    def _ensure_running(record: dict) -> None:
        if record.get("status") != "running":
            raise HandoffError(
                f"execution {record.get('execution_id')} is terminal "
                f"({record.get('status')}); it cannot be modified")

    def _new_execution_id(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        return f"{stamp}-{uuid.uuid4().hex[:12]}"

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        lock_path = self.root / ".lock"
        with lock_path.open("a+") as lock:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def current_execution() -> Execution | None:
    return _CURRENT.get()


def prepare_agent_prompt(prompt: str, *, new_session: bool) -> str:
    """Embed required handoffs in the first message of every fresh session."""
    execution = current_execution()
    if execution is None or not new_session:
        return prompt
    execution.manager.record_delivery(execution)
    record = execution.manager._load_execution(execution)
    prior = record.get("provider_runs") or []
    prior_lines = []
    if prior:
        prior_lines = ["\n## Earlier provider attempts in this execution", ""]
        for run in prior[-3:]:
            prior_lines.append(
                f"- Auxiliary task `{run.get('logical_task_id')}` rc={run.get('rc')}; "
                f"immutable log `{execution.manager.workspace / run['log_path']}`; "
                f"agent summary: {_bounded_text(run.get('agent_summary') or 'unknown', 800, 'attempt summary')}")
        if len(prior) > 3:
            prior_lines.append(
                f"- {len(prior) - 3} older attempts indexed in "
                f"`{execution.directory / 'record.json'}`.")
        prior_lines.append("")
    return (execution.input_text + "\n" + "\n".join(prior_lines) +
            "\n---\n\n# Current task prompt\n\n" + prompt +
            "\n\n---\n\nBefore ending, include a concise semantic summary of "
            "work performed, decisions, validation, and remaining risks. Keep the "
            "existing output contract. Add `handoff_summary` to JSON only when "
            "the task schema explicitly permits it; otherwise put the summary "
            "in the response text if permitted. Never add fields to a closed schema.\n")


T = TypeVar("T")


def run_task(workspace: Path | str, spec: TaskSpec, operation: Callable[[], T],
             *, success: Callable[[T], bool] = lambda result: result == 0,
             summary: Callable[[T], str] | str = "Task completed.",
             artifacts: Callable[[T], Sequence[Path | str]] | Sequence[Path | str] = (),
             verification: Callable[[T], Sequence[str]] | Sequence[str] = ()) -> T:
    """Run one business task and publish only after its acceptance predicate."""
    manager = HandoffManager(workspace)
    with manager.start(spec) as execution:
        result = operation()
        sm = summary(result) if callable(summary) else summary
        arts = artifacts(result) if callable(artifacts) else artifacts
        checks = verification(result) if callable(verification) else verification
        if success(result):
            execution.complete(sm, artifacts=arts, verification=checks)
        else:
            execution.fail(
                "business acceptance rejected result " +
                _bounded_text(repr(result), 2_000, "business result"),
                summary=sm, artifacts=arts, verification=checks)
        return result


def _agent_summary(output: str) -> str:
    if not output:
        return ""
    candidates = []
    for block in re.findall(r"```json\s*(.*?)```", output, re.DOTALL):
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            for key in ("handoff_summary", "summary", "patch_summary", "message",
                        "reason", "notes"):
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip())
                    break
    text = candidates[-1] if candidates else output.strip()
    if len(text) <= MAX_AGENT_EXCERPT_CHARS:
        return text
    # The omission is separately recorded in the handoff; this is never silent.
    return text[-MAX_AGENT_EXCERPT_CHARS:]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_json(path: Path) -> dict:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        raise ValueError(f"expected JSON object at {path}")
    return obj


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(value)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _atomic_json(path: Path, value: dict) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2,
                                  sort_keys=True) + "\n")


def _bounded_text(text: str, limit: int, label: str) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return (text[:limit] + f"\n\n[{omitted} characters omitted from the concise "
            f"{label}; complete evidence remains in record/log pointers.]")
