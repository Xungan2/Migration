"""P0 scaffold subtasks: independent sessions joined by validated handoffs."""

import hashlib
import json
import time
from pathlib import Path

from .. import log
from ..common import agent, scope
from ..handoff import HandoffManager, NotReady, TaskSpec, run_task
from . import recipe_apply, scaffold


DISCOVERY = {
    "build": ("language", "driver_home", "precedent", "dependencies", "wiring"),
    "device": ("binding", "initialization", "injection", "acceptance"),
    "test": ("substrate", "scope", "minimal_test", "probe_channel"),
}


def _followup_entries(recipe: dict) -> list[dict]:
    entries = recipe.get("deferred_findings")
    if not isinstance(entries, list):
        raise ValueError("施工单须包含 deferred_findings 数组（无后续发现时为空）")
    for item in entries:
        if (not isinstance(item, dict) or any(not isinstance(item.get(k), str)
                or not item[k].strip() for k in ("topic", "read_when", "finding"))
                or item.get("status") not in ("source_confirmed", "unverified")
                or not isinstance(item.get("evidence"), list)
                or not item["evidence"]
                or any(not isinstance(p, str) or not p.strip() for p in item["evidence"])):
            raise ValueError("deferred_findings 缺少主题、阅读条件、内容、证据或验证状态")
    return entries


def publish_followup(ws: Path) -> None:
    """Publish agent-authored future work separately from P0 acceptance."""
    recipe_path = ws.joinpath(*scaffold.RECIPE_NAME)
    manager = HandoffManager(ws)
    manager.require_success("p0.scaffold.recipe", current_artifacts=(recipe_path,))
    output = ws / "P0/reports/scaffold/followup.json"
    task_id = "p0.scaffold.followup"
    try:
        manager.require_success(task_id, current_artifacts=(output,))
        return
    except NotReady:
        pass

    def publish():
        recipe = json.loads(recipe_path.read_text())
        entries = _followup_entries(recipe)
        output.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n")
        return ("后续开发资料；不是 P0 验收，也不表示建议已实现。"
                f"遇到以下条件时读取 `{output}` 中对应主题，并核实当前源码：\n"
                + "\n".join(f"- {item['topic']}：{item['read_when']}" for item in entries))

    run_task(ws, TaskSpec(task_id, ("p0.scaffold.recipe",), inherit_parent_inputs=False),
             publish, success=lambda result: True, summary=lambda result: result,
             artifacts=(output,), verification=("Agent-authored notes preserved with evidence and verification status.",))


def _check_discovery(value: dict, fields: tuple[str, ...], target: Path) -> None:
    findings = value.get("findings", {})
    if not isinstance(findings, dict) or any(
            not isinstance(findings.get(k), str) or not findings[k].strip() for k in fields):
        raise ValueError(f"findings 必须包含非空文本字段 {fields}")
    _check_evidence(value.get("evidence"), target)
    if not isinstance(value.get("uncertainties"), list) or not value.get("handoff_summary"):
        raise ValueError("必须记录 uncertainties 和 handoff_summary")


def _check_evidence(evidence, target: Path) -> None:
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("evidence 必须含实际源码证据")
    for item in evidence:
        rel = Path(item["path"])
        path = (target / rel).resolve()
        if rel.is_absolute() or ".." in rel.parts or not path.is_relative_to(target.resolve()):
            raise ValueError("evidence 路径必须位于指定源码根内")
        lines = path.read_text().splitlines()
        line, quote = item["line"], item["quote"]
        if (type(line) is not int or not 1 <= line <= len(lines)
                or not isinstance(quote, str) or not quote.strip() or quote not in lines[line - 1]):
            raise ValueError(f"evidence 未命中原文：{rel}:{line}")


def _generate(ws: Path, target: Path, task_id: str, dependencies: tuple[str, ...],
              output: Path, prompt: str, check, input_hash: str, *, reuse: bool = True) -> dict:
    manager = HandoffManager(ws)
    try:
        if not reuse:
            raise NotReady("本任务须重新核对当前输入")
        manager.require_success(task_id, current_artifacts=(output,))
        value = json.loads(output.read_text())
        if value.get("input_sha256") != input_hash:
            raise ValueError("任务输入已变化")
        check(value)
    except (NotReady, OSError, ValueError, KeyError, TypeError):
        pass
    else:
        log.console_line(f"[porter] P0: 复用 {task_id} handoff")
        return value

    def generate():
        output.unlink(missing_ok=True)
        session = None
        started = time.monotonic()
        message = prompt + f"\n输出文件：`{output}`\n本任务预算 {scaffold.AGENT_TIMEOUT_SEC} 秒。"
        for attempt in range(1, scaffold.AGENT_TRIES + 1):
            remaining = int(scaffold.AGENT_TIMEOUT_SEC - (time.monotonic() - started))
            if remaining <= 0:
                raise RuntimeError(f"{task_id}: agent 预算耗尽")
            stem = str(ws / "P0/logs" / f"{task_id}_R{attempt}")
            rc, text = agent._opencode_json_runner(
                message, workdir=target, log_stem=stem, timeout_sec=remaining,
                session_id=session, task={"phase": "P0", "step": task_id, "attempt": attempt})
            events = agent._parse_events(text) or {}
            if rc != 0:
                raise RuntimeError(f"{task_id}: provider rc={rc}; log={stem}.log")
            session = events.get("session_id") or session
            if not session:
                raise RuntimeError(f"{task_id}: session id missing; log={stem}.log")
            try:
                value = json.loads(output.read_text())
                if not isinstance(value, dict):
                    raise ValueError("输出必须是 JSON 对象")
                check(value)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                message = f"修订本任务输出 `{output}`：{exc}。保留已确认发现，不扩展调查范围。"
                continue
            value["input_sha256"] = input_hash
            output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            return value
        raise RuntimeError(f"{task_id}: 输出校验失败；{message}")

    proj = json.loads((ws / "project.json").read_text())
    materials = [ws / "project.json", Path(proj["linux_driver"]), target,
                 *(Path(p) for p in proj.get("materials", []))]
    if (ws / "goals.md").exists():
        materials.append(ws / "goals.md")
    return run_task(
        ws, TaskSpec(task_id, dependencies, tuple(materials),
                     description="scaffold discovery/proposal; build and runtime not yet verified",
                     inherit_parent_inputs=False),
        generate, success=lambda value: True,
        summary=lambda value: str(value.get("handoff_summary") or f"已校验施工单 {output}"),
        artifacts=(output,), verification=("Output schema and source evidence accepted; no build/runtime claim.",))


def prepare(ws: Path, target: Path, device_ids: list[str] | None = None) -> int:
    """Discover three narrow contracts, compose a recipe, then apply it."""
    from ..env import prerequisites
    rc = prerequisites.run(ws, target, device_ids)
    if rc:
        return rc
    proj = json.loads((ws / "project.json").read_text())
    driver = scope.driver_name_of(proj)
    inputs = {k: proj.get(k) for k in ("linux_driver", "target_os", "driver_name", "category", "materials")}
    goals = ws / "goals.md"
    inputs.update(device_ids=device_ids, goals=goals.read_text() if goals.exists() else "")
    input_hash = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    manager = HandoffManager(ws)
    manifest = ws.joinpath(*scaffold.MANIFEST_NAME)
    if manifest.exists():
        build = manager.require_success("p0.scaffold.build")
        if "p0.prerequisites" not in build.get("dependencies", []):
            raise NotReady("旧骨架发现未绑定前置依赖检查；请在新工作区重新生成骨架")
        manager.require_success("p0.scaffold.apply")
        publish_followup(ws)
        return 0
    out = ws / "P0/reports/scaffold"
    out.mkdir(parents=True, exist_ok=True)
    (ws / "P0/logs").mkdir(parents=True, exist_ok=True)
    from . import kb
    context = (f"目标树：{target}\nLinux 输入：{proj['linux_driver']}\n驱动身份：{driver}\n"
               f"类别：{proj.get('category')}\n意图：{goals if goals.exists() else '未提供'}\n"
               f"认领键：{device_ids or '未提供；从 Linux 源码的注册入口与匹配条件确认'}\n")
    context += f"前置依赖报告：{ws / 'P0/reports/prerequisites.json'}（必须读取）\n"
    previous = ("p0.prerequisites",)
    for name, fields in DISCOVERY.items():
        task_id = f"p0.scaffold.{name}"
        prompt = (agent.load_skill("P0-scaffold-discover") + "\n" + context
                  + f"\n当前子任务：{name}；findings 必填字段：{', '.join(fields)}。\n"
                  + f"已发布发现位于 {out}，按前置 handoff 指针读取。\n")
        if name == "build":
            prompt += kb.kb_face(ws, ["pitfalls"])
        _generate(ws, target, task_id, previous, out / f"{name}.json", prompt,
                  lambda value, fields=fields: _check_discovery(value, fields, target), input_hash)
        previous = (task_id,)

    recipe_path = ws.joinpath(*scaffold.RECIPE_NAME)
    recipe_path.parent.mkdir(parents=True, exist_ok=True)
    prompt = (agent.load_skill("P2-scaffold") + "\n" + context
              + f"\n本任务只生成施工单。构建、认领和测试发现已通过 handoff 交付，"
              f"必须先读 {out}/build.json、device.json、test.json。"
              "在各项已确认的适用范围内复用先例和接口；仅补读影响本次代码生成的缺项。"
              "保留待实测项交给后续三个 loop，不为消除所有不确定性延迟落盘。\n"
              f"施工单输出路径：`{recipe_path}`。")

    def check_recipe(value):
        _followup_entries(value)
        errors = recipe_apply.validate_recipe(value, driver)
        if errors:
            raise ValueError("; ".join(errors))

    recipe = _generate(ws, target, "p0.scaffold.recipe", previous, recipe_path,
                       prompt, check_recipe, input_hash)
    publish_followup(ws)

    def apply():
        journal = ws.joinpath(*scaffold.JOURNAL_NAME)
        recipe_apply.rollback(target, journal)
        result = recipe_apply.apply_recipe(target, recipe, journal)
        if result.get("skipped"):
            raise RuntimeError(f"P0: 骨架施工未完成：{result['skipped']}")
        scaffold._write_manifest(ws, proj, recipe, result, {}, 1)
        return 0

    return run_task(ws, TaskSpec("p0.scaffold.apply", ("p0.scaffold.recipe",)), apply,
                    summary="骨架已施工；尚未通过编译、启动或单测。",
                    artifacts=(manifest,), verification=("Recipe applied without skipped edits.",))
