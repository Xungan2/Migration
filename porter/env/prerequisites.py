"""Identify prerequisite capabilities and stop before scaffolding missing ones."""

import hashlib
import json
import re
from graphlib import TopologicalSorter
from pathlib import Path

from .. import log
from ..bootstrap import scaffold_p0
from ..bootstrap.extract_spine import _find_kernel_root
from ..common import agent
from ..handoff import HandoffManager, NotReady, TaskSpec, run_task
from ..loop import gates

REPORT = "P0/reports/prerequisites.json"
TASKS = "P0/reports/prerequisite_tasks.json"
TASK_VIEW = "P0/reports/prerequisite_tasks.md"
GATE = "p0.prerequisites"


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须为非空文本")


def validate(value: dict, source: Path, target: Path) -> None:
    if not isinstance(value, dict):
        raise ValueError("前置依赖报告必须为对象")
    for key in ("subject", "platform", "binding", "handoff_summary"):
        _text(value.get(key), key)
    scaffold_p0._check_evidence(value.get("source_evidence"), source)
    dependencies = value.get("dependencies")
    if not isinstance(dependencies, list):
        raise ValueError("dependencies 必须为数组；无前置依赖时显式为空")
    graph, available = {}, set()
    for dep in dependencies:
        if not isinstance(dep, dict):
            raise ValueError("dependency 必须为对象")
        key = dep.get("id")
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", key) or key in graph:
            raise ValueError("dependency id 必须唯一且只含小写字母、数字、下划线或连字符")
        for field in ("capability", "reason", "target_status", "action"):
            _text(dep.get(field), field)
        if dep.get("status") not in ("available", "missing", "unknown"):
            raise ValueError("dependency status 必须为 available|missing|unknown")
        for field in ("source_files", "acceptance"):
            items = dep.get(field)
            if not isinstance(items, list) or not items:
                raise ValueError(f"{field} 必须为非空数组")
            for item in items:
                _text(item, field)
        for name in dep["source_files"]:
            rel = Path(name)
            path = (source / rel).resolve()
            if rel.is_absolute() or ".." in rel.parts or not path.is_relative_to(source.resolve()) or not path.is_file():
                raise ValueError(f"source_files 必须是 Linux 参考根内的实际文件：{name}")
        scaffold_p0._check_evidence(dep.get("source_evidence"), source)
        evidence = dep.get("target_evidence")
        if not isinstance(evidence, list):
            raise ValueError("target_evidence 必须为数组")
        if evidence or dep["status"] == "available":
            scaffold_p0._check_evidence(evidence, target)
        needs = dep.get("depends_on")
        if not isinstance(needs, list) or any(not isinstance(n, str) for n in needs):
            raise ValueError("depends_on 必须为依赖 id 数组")
        graph[key] = needs
        if dep["status"] == "available":
            available.add(key)
    if any(n not in graph for needs in graph.values() for n in needs):
        raise ValueError("depends_on 引用了未声明依赖")
    tuple(TopologicalSorter(graph).static_order())
    if any(n not in available for key in available for n in graph[key]):
        raise ValueError("available 依赖的前置能力尚未就绪")


def _write_tasks(ws: Path, value: dict, source: Path, target: Path) -> list[dict]:
    deps = {d["id"]: d for d in value["dependencies"]}
    pending = {key for key, dep in deps.items() if dep["status"] != "available"}
    tasks = []
    lines = ["# P0 前置迁移任务", "", "完成后重跑原 P0 命令；工具重新核对依赖，手工标记不作为放行依据。", ""]
    for key in TopologicalSorter({key: d["depends_on"] for key, d in deps.items()}).static_order():
        if key not in pending:
            continue
        dep = deps[key]
        task = {**dep, "linux_reference_root": str(source), "target_os": str(target),
                "blocked_by": [n for n in dep["depends_on"] if n in pending]}
        tasks.append(task)
        lines += [f"## {dep['capability']} ({key})", "", f"状态：{dep['status']}",
                  f"平台：{value['platform']}", f"目标树：{target}",
                  f"阻塞于：{', '.join(task['blocked_by']) or '无'}", "",
                  f"必要性：{dep['reason']}", f"目标现状：{dep['target_status']}",
                  f"任务：{dep['action']}", "", "源码参考：", "",
                  *(f"- {source / name}" for name in dep["source_files"]), "", "完成条件：", "",
                  *(f"- {a}" for a in dep["acceptance"]), ""]
    if not tasks:
        lines += ["所有已识别前置依赖可用；主驱动仍须通过后续三个验证 loop。", ""]
    (ws / TASKS).write_text(json.dumps(tasks, ensure_ascii=False, indent=2) + "\n")
    (ws / TASK_VIEW).write_text("\n".join(lines))
    return tasks


def run(ws: Path, target: Path, device_ids=None) -> int:
    proj = json.loads((ws / "project.json").read_text())
    driver = Path(proj["linux_driver"]).resolve()
    source = _find_kernel_root(driver) or driver
    output = ws / REPORT
    output.parent.mkdir(parents=True, exist_ok=True)
    (ws / "P0/logs").mkdir(parents=True, exist_ok=True)
    prompt = (agent.load_skill("P0-prerequisites") + "\n\n"
              + f"主驱动：{driver}\nLinux 参考根：{source}\n目标树：{target}\n"
              + f"类别：{proj.get('category')}\n指定认领键：{device_ids or '未指定'}\n"
              + f"项目与资料入口：{ws / 'project.json'}\n迁移意图：{ws / 'goals.md'}（若存在）\n"
              + f"历史报告与任务：{output}、{ws / TASKS}（若存在，须重新查当前源码）\n")
    # Recheck on every P0 invocation: an absent capability can appear outside
    # the old report's evidence files after a prerequisite migration.
    value = scaffold_p0._generate(
        ws, target, "p0.prerequisites.discover", (), output, prompt,
        lambda value: validate(value, source, target),
        hashlib.sha256(prompt.encode()).hexdigest(), reuse=False)
    tasks = _write_tasks(ws, value, source, target)
    manager = HandoffManager(ws)
    artifacts = (output, ws / TASKS, ws / TASK_VIEW)
    if not tasks:
        try:
            manager.require_success(GATE, current_artifacts=artifacts)
        except NotReady:
            pass
        else:
            return 0

    def assess():
        ledger = gates.GateLedger(ws).load()
        if not tasks:
            if ledger.find(GATE):
                ledger.mark(GATE, "resolved", resolution="当前源码依赖复核通过")
                gates.render_human_questions(ws, ledger)
            log.console_line("[porter] P0: 前置依赖可用，继续主驱动骨架与验证")
            return 0
        if ledger.find(GATE):
            ledger.mark(GATE, "open", resolution=None)
        gates.panic(ws, {
            "id": GATE, "kind": "fact", "phase": "P0", "blocking": True,
            "question": (f"主驱动缺少或尚未确认 {len(tasks)} 项前置能力。"
                         f"按 {TASK_VIEW} 完成前置任务后重跑 P0；填写答案不能跳过依赖复核。"),
            "context_files": [REPORT, TASKS, TASK_VIEW],
            "answer_form": [{"field": "note", "type": "text", "required": True,
                             "hint": "补齐的能力及验证证据位置（仅供重验参考）"}],
        })
        return 3

    return run_task(ws, TaskSpec(GATE, materials=artifacts, inherit_parent_inputs=False),
                    assess, artifacts=artifacts,
                    summary=value["handoff_summary"],
                    verification=("Dependency graph and source references checked; missing/unknown capabilities block scaffolding. Runtime acceptance remains mandatory.",))


def is_ready(ws: Path, target: Path) -> bool:
    """T5 checks the recorded decision and its evidence, without calling agents."""
    try:
        proj = json.loads((ws / "project.json").read_text())
        driver = Path(proj["linux_driver"]).resolve()
        value = json.loads((ws / REPORT).read_text())
        validate(value, _find_kernel_root(driver) or driver, target)
        HandoffManager(ws).require_success(
            GATE, current_artifacts=(ws / REPORT, ws / TASKS, ws / TASK_VIEW))
        return all(d["status"] == "available" for d in value["dependencies"])
    except (NotReady, OSError, ValueError, KeyError, TypeError):
        return False
