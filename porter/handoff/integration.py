"""Thin orchestration adapters for the handoff module.

This file contains Porter-specific task names and dependency declarations.  The
storage and validation implementation stays in :mod:`porter.handoff.core`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .core import HandoffError, HandoffManager, NotReady, TaskSpec


PIPELINE_COMMANDS = {
    "p0", "p1", "p1-strategy", "p1-divide", "p1-resolve", "p1-import", "p1-prune",
    "p2", "p2-map", "p2-scaffold", "p2-skeleton", "p2-probes",
    "p3", "p4", "p5", "loop", "p6", "p7", "kb",
}


def execute_cli(args, operation: Callable[[], int]) -> int:
    """Run a CLI business task with strict readiness and terminal recording."""
    phase = str(getattr(args, "phase", ""))
    if phase not in PIPELINE_COMMANDS:
        return operation()
    if phase == "p6" and getattr(args, "defect_list", False):
        return operation()
    if not getattr(args, "output_dir", None):
        # Commands whose workspace flag is optional own the user-facing missing
        # argument diagnostic. They reject before any provider transport.
        return operation()
    ws = Path(args.output_dir).resolve()
    spec = cli_spec(args, ws)
    manager = HandoffManager(ws)
    try:
        with manager.start(spec) as execution:
            before = {str(p): p.exists() for p in output_artifacts(ws, spec.task_id)}
            rc = operation()
            artifacts = output_artifacts(ws, spec.task_id, args=args)
            verification = [
                f"CLI command `{phase}` returned business status rc={rc}.",
                ("The command's existing acceptance checks accepted its outputs."
                 if rc == 0 else
                 "The command did not reach an accepted business result."),
            ]
            summary = (f"Porter task `{spec.task_id}` completed. Provider-authored "
                       "summaries and immutable evidence pointers are recorded below."
                       if rc == 0 else
                       f"Porter task `{spec.task_id}` ended with rc={rc}; inspect the "
                       "failure reason and provider evidence below.")
            if rc == 0:
                reused = (not execution.record.get("provider_runs") and
                          bool(artifacts) and all(before.get(str(p), False)
                                                  for p in artifacts))
                execution.complete(summary, artifacts=artifacts,
                                   verification=verification, reused=reused)
            else:
                execution.fail(f"CLI business result rc={rc}", summary=summary,
                               artifacts=artifacts, verification=verification)
            return rc
    except NotReady as exc:
        print(f"[porter] handoff: not ready — {exc}")
        return 2
    except HandoffError as exc:
        print(f"[porter] handoff: publication failed — {exc}")
        return 1


def execute_phase(ws: Path, task_id: str, dependencies: tuple[str, ...],
                  operation: Callable[[], int], *, materials=(), artifacts=(),
                  description: str = "") -> int:
    """Run a module/phase child task; used by the loop composite command."""
    spec = TaskSpec(task_id, dependencies, tuple(materials), description)
    manager = HandoffManager(ws)
    try:
        with manager.start(spec) as execution:
            rc = operation()
            # Preserve the declaration verbatim.  Execution.complete owns the
            # fail-closed existence check; filtering here would let rc=0 publish
            # success without its required output.
            outputs = [Path(p) for p in artifacts]
            facts = [f"Phase business function returned rc={rc}."]
            if rc == 0:
                execution.complete(
                    f"Porter accepted `{task_id}` after its phase validation.",
                    artifacts=outputs, verification=facts)
            else:
                execution.fail(
                    f"phase business result rc={rc}",
                    summary=f"`{task_id}` did not reach accepted completion.",
                    artifacts=outputs, verification=facts)
            return rc
    except NotReady as exc:
        print(f"[porter] handoff: not ready — {exc}")
        return 2
    except HandoffError as exc:
        print(f"[porter] handoff: publication failed — {exc}")
        return 1


def module_task_id(module: str, phase: str) -> str:
    return f"loop.module.{module}.{phase}"


def module_dependencies(ws: Path, module: str, phase: str) -> tuple[str, ...]:
    """Return direct source-DAG predecessors plus explicit shared-state producer."""
    if phase == "p3":
        deps = ["p2.probes"]
        graph = _deps_json(ws)
        order = list(graph.get("order") or [])
        if module in order:
            # Use the latest *actually published* earlier phase as the producer
            # of shared target state.  This preserves deliberate bypass of an
            # unrelated parked module while retaining a precise version when an
            # earlier module did modify shared files.
            manager = HandoffManager(ws)
            for prior in reversed(order[:order.index(module)]):
                chosen = None
                for prior_phase in ("p5", "p4"):
                    candidate = module_task_id(str(prior), prior_phase)
                    records = manager.inspect(candidate)
                    statuses = {record.get("status") for record in records}
                    if statuses & {"success", "running", "corrupt"}:
                        # Declare the actual producer and let prepare validate
                        # it strictly. A damaged or superseded real producer
                        # must not silently fall back to an older task.
                        chosen = candidate
                        break
                if chosen:
                    deps.append(chosen)
                    break
        for upstream in (graph.get("edges") or {}).get(module, []):
            deps.append(module_task_id(str(upstream), "p5"))
        return tuple(dict.fromkeys(deps))
    if phase == "p4":
        return (module_task_id(module, "p3"),)
    if phase == "p5":
        return (module_task_id(module, "p4"),)
    raise ValueError(phase)


def module_failure_inputs(ws: Path, module: str) -> tuple[str, ...]:
    """Failed earlier shared-state producers to promote into a P3 input."""
    graph = _deps_json(ws)
    order = list(graph.get("order") or [])
    if module not in order:
        return ()
    manager = HandoffManager(ws)
    promoted = []
    for prior in order[:order.index(module)]:
        for phase in ("p4", "p5"):
            task_id = module_task_id(str(prior), phase)
            if any(r.get("status") == "failed" for r in manager.inspect(task_id)):
                promoted.append(task_id)
    return tuple(promoted)


def cli_spec(args, ws: Path) -> TaskSpec:
    phase = str(args.phase)
    materials: list[Path | str] = []
    if phase == "p0":
        materials = [Path(args.linux_driver), Path(args.target_os)]
        materials.extend(Path(p) for p in (args.materials or []))
        if getattr(args, "intent_file", None):
            materials.append(Path(args.intent_file))
        return TaskSpec("p0", materials=tuple(materials),
                        description="environment inputs and executable gate")
    materials.append("project.json")
    if phase in ("p1", "p1-strategy"):
        return TaskSpec("p1.pipeline" if phase == "p1" else "p1.strategy",
                        ("p0",), tuple(materials), "P1 strategy or composite")
    if phase == "p1-divide":
        return TaskSpec("p1.divide", ("p1.strategy",), tuple(materials))
    if phase == "p1-resolve":
        materials.extend(["P1/reports/P1D_plan.json", "P1/strategy.md"])
        return TaskSpec("p1.resolve", ("p1.divide",), tuple(materials))
    if phase == "p1-import":
        materials.extend(Path(p) for p in
                         (args.plan, args.deps, args.strategy) if p)
        return TaskSpec("p1.resolve", ("p0",), tuple(materials),
                        "externally supplied P1 artifacts, locally revalidated")
    if phase == "p1-prune":
        materials.extend(p for p in ("goals.md", "P1/scope.json")
                         if (ws / p).exists())
        return TaskSpec("p1.prune", ("p1.resolve",), tuple(materials),
                        "functional pruning decisions and restored dependencies")
    if phase in ("p2", "p2-map"):
        task_id = "p2.pipeline" if phase == "p2" else "p2.map"
        predecessor = "p1.prune" if (ws / "P1/scope.json").exists() else "p1.resolve"
        return TaskSpec(task_id, (predecessor,), tuple(materials),
                        "P2 composite" if phase == "p2" else "P2 mapping")
    if phase in ("p2-scaffold", "p2-skeleton"):
        materials.append("runner.json")
        return TaskSpec("p2.scaffold", ("p0",), tuple(materials))
    if phase == "p2-probes":
        materials.extend(["runner.json", "P2/mapping.json"])
        return TaskSpec("p2.probes", ("p2.scaffold",), tuple(materials))
    if phase in ("p3", "p4", "p5"):
        module = _selected_module(ws, getattr(args, "module", None))
        if not module:
            # A completed loop still receives a concrete, queryable task record.
            return TaskSpec(f"loop.noop.{phase}", (), tuple(materials))
        materials.append("P1/modules/deps.json")
        return TaskSpec(
            module_task_id(module, phase),
            module_dependencies(ws, module, phase), tuple(materials),
            include_failures=(module_failure_inputs(ws, module)
                              if phase == "p3" else ()))
    if phase == "loop":
        materials.append("P1/modules/deps.json")
        return TaskSpec("loop.command", ("p2.probes",), tuple(materials),
                        "composite scheduler; child phase executions are linked")
    if phase == "p6":
        task_id = _p6_task_id(args)
        if any(getattr(args, name, None)
               for name in ("defect_add", "defect_close", "defect_park")):
            if (ws / "defects.json").exists():
                materials.append("defects.json")
            return TaskSpec(task_id, ("p0",), tuple(materials),
                            "defect ledger mutation")
        materials.append("P1/modules/deps.json")
        module_deps = tuple(module_task_id(m, "p5") for m in
                            (_deps_json(ws).get("order") or []))
        if not module_deps:
            module_deps = ("p2.probes",)
        if task_id == "p6.l4.finalize":
            deps = ("p6.l4.draft",)
        elif task_id == "p6.execute" and getattr(args, "l4", False):
            deps = module_deps + ("p6.l4.finalize",)
        else:
            deps = module_deps
        return TaskSpec(task_id, deps, tuple(materials))
    if phase == "p7":
        if getattr(args, "patch_register", None):
            materials.extend(p for p in ("platform_patches.json",)
                             if (ws / p).exists())
            safe = _safe_id(args.patch_register)
            return TaskSpec(f"p7.patch.{safe}.register", ("p0",),
                            tuple(materials))
        if getattr(args, "patch_status", None):
            materials.append("platform_patches.json")
            safe = _safe_id(args.patch_status)
            return TaskSpec(f"p7.patch.{safe}.status", ("p0",),
                            tuple(materials))
        dep = _latest_available(manager=HandoffManager(ws),
                                candidates=("p6.execute", "p6.aggregate"))
        return TaskSpec("p7", (dep,), tuple(materials))
    if phase == "kb":
        return TaskSpec("kb.manage", ("p0",), tuple(materials))
    raise ValueError(f"no handoff task mapping for {phase}")


def output_artifacts(ws: Path, task_id: str, args=None) -> list[Path]:
    if task_id == "p0":
        if args is not None and getattr(args, "t1_only", False):
            return [ws / "project.json"]
        return [ws / "project.json", ws / "runner.json",
                ws / "P0" / "reports" / "p0_report.md"]
    p1_outputs = {
        "p1.strategy": [ws / "P1" / "strategy.md"] +
                       ([ws / "P1/scope.json"] if (ws / "P1/scope.json").exists() else []),
        "p1.divide": [ws / "P1" / "reports" / "P1D_plan.json"],
        "p1.resolve": [ws / "P1" / "modules" / "deps.json"],
        "p1.prune": [ws / "P1/reports/pruning.json", ws / "P1/modules/deps.json"],
        "p1.pipeline": [ws / "P1" / "modules" / "deps.json",
                        ws / "P1" / "reports" / "report.md"],
    }
    if task_id in p1_outputs:
        return p1_outputs[task_id]
    p2_outputs = {
        "p2.map": [ws / "P2" / "mapping.json"],
        "p2.scaffold": [ws / "P2" / "reports" / "scaffold_manifest.json"],
        "p2.probes": [ws / "P2" / "reports" / "pregen_report.md"],
        "p2.pipeline": [ws / "P2" / "mapping.json",
                        ws / "P2" / "reports" / "scaffold_manifest.json",
                        ws / "P2" / "reports" / "pregen_report.md"],
    }
    if task_id in p2_outputs:
        return p2_outputs[task_id]
    match = __import__("re").fullmatch(r"loop\.module\.(.+)\.(p[345])", task_id)
    if match:
        module, phase = match.groups()
        return [ws / phase.upper() / module / "reports"]
    if task_id.startswith("p6.defect."):
        return [ws / "defects.json"]
    if task_id.startswith("p7.patch."):
        return [ws / "platform_patches.json"]
    if task_id.startswith("p6.l4."):
        return [ws / "P6" / "reports" / "l4_criteria.json"]
    if task_id.startswith("p6."):
        return [ws / "P6" / "reports" / "health.json",
                ws / "P6" / "reports" / "health.md"]
    if task_id == "p7":
        return [ws / "P7" / "reports" / "final_report.json",
                ws / "P7" / "reports" / "final_report.md"]
    if task_id == "loop.command":
        return [ws / "loop_state.json", ws / "reports" / "loop_report.md"]
    return []


def _deps_json(ws: Path) -> dict:
    path = ws / "P1" / "modules" / "deps.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _selected_module(ws: Path, requested: str | None) -> str | None:
    if requested:
        return requested
    try:
        state = json.loads((ws / "loop_state.json").read_text(encoding="utf-8"))
        order = state.get("order") or []
        modules = state.get("modules") or {}
        return next((m for m in order
                     if (modules.get(m) or {}).get("phase") != "done"), None)
    except (OSError, json.JSONDecodeError):
        order = _deps_json(ws).get("order") or []
        return str(order[0]) if order else None


def _p6_task_id(args) -> str:
    if getattr(args, "defect_add", None):
        return f"p6.defect.{_safe_id(args.defect_add)}.add"
    if getattr(args, "defect_close", None):
        return f"p6.defect.{_safe_id(args.defect_close)}.close"
    if getattr(args, "defect_park", None):
        return f"p6.defect.{_safe_id(args.defect_park)}.park"
    if getattr(args, "defect_diagnose", None):
        return f"p6.defect.{args.defect_diagnose}.diagnose"
    if getattr(args, "defect_fix", None):
        return f"p6.defect.{args.defect_fix}.fix"
    if getattr(args, "draft_l4", False):
        return "p6.l4.draft"
    if getattr(args, "finalize_l4", False):
        return "p6.l4.finalize"
    if getattr(args, "execute", False):
        return "p6.execute"
    return "p6.aggregate"


def _safe_id(value: object) -> str:
    return __import__("re").sub(r"[^A-Za-z0-9_.-]+", "_", str(value)).strip("_.") \
        or "item"


def _latest_available(manager: HandoffManager,
                      candidates: tuple[str, ...]) -> str:
    for task_id in candidates:
        if any(r.get("status") == "success" for r in manager.inspect(task_id)):
            return task_id
    return candidates[-1]
