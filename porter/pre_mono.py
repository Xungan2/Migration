"""Turn prepare's advisory plan into the smallest executable mono handoff."""

from __future__ import annotations

import json
from pathlib import Path

from .artifacts import ARTIFACTS, PREPARE_ARTIFACTS, canonicalize, locate, write_state
from .common import agent
from .handoff import latest_success, publish_handoff, require_success, run_task, TaskSpec
from .workspace import ensure_runner


def _ensure_prepare(ws: Path) -> None:
    """Import a completed old workspace once; new runs publish normally."""
    if latest_success(ws, "prepare"):
        ensure_runner(ws)
        require_success(ws, "prepare")
        try:
            locate(ws, required=PREPARE_ARTIFACTS)
        except ValueError:
            # Historical handoffs predate the artifact index and let this
            # agent create the first plan files. New prepare runs always
            # write state.json and therefore fail here when files are gone.
            if (ws / "state.json").exists():
                raise
        return
    state = ws / "prepare" / "state.json"
    if state.exists():
        value = json.loads(state.read_text(encoding="utf-8"))
        if value.get("status") == "complete":
            ensure_runner(ws)
            try:
                locate(ws, required=PREPARE_ARTIFACTS)
            except ValueError:
                if (ws / "state.json").exists():
                    raise
            publish_handoff(ws, "prepare", summary="Imported completed prepare state.",
                            artifacts=[ws / "project.json", ws / "goals.md", state,
                                       ws / "runner.md"],
                            verification=["historical prepare state was complete"])
            return
    raise ValueError("pre-mono requires a successful prepare handoff")


def run(ws: Path) -> int:
    ws = Path(ws).resolve()
    try:
        _ensure_prepare(ws)
        prepare = require_success(ws, "prepare")
    except (ValueError, OSError) as exc:
        print(f"pre-mono: {exc}")
        return 1
    try:
        prepare_paths = locate(ws, required=PREPARE_ARTIFACTS)
    except ValueError:
        prepare_paths = {}
    location_lines = "\n".join(
        f"- `{name}`: `{path}`" for name, path in prepare_paths.items())
    handoff_record = prepare.get("record") or prepare.get("record_data")
    prompt = f"""你是 pre-mono 主 agent。请完成一次真实的迁移前模块划分任务，而不是调用脚本做正则解析。

必须先阅读：
- prepare 成功 handoff：`{prepare.get('handoff', '见 prepare/index.json')}`
- prepare execution record：`{handoff_record}`
- `runner.md`（只记录迁移内部的模块编译、镜像编译、设备自启动、设备注入/交互和单元测试命令；Porter CLI 调用不要写入）
- `runner.json`（参考 runner.md 生成的机器执行契约；固定顶层段落按实际执行需要设计，
  允许保留 agent 认为有用的扩展字段，必须是可解析的 JSON 对象）
- `knowledgebase/README.md` 及与当前驱动有关的入口
- 源码树、目标树和 prepare 交付物中与模块边界有关的文件
- state.json 当前计划文件位置：
{location_lines or '（索引尚无记录；交付后必须建立）'}

请依据事实完成模块划分和计划修订，允许拆分、合并、改依赖和调整顺序。不要把 prepare 的建议当成固定命令。

必须写入工作区：
1. `module-division.md` 和 `module-division.json`
2. 更新后的 `migration-plan.md` 和 `migration-plan.json`
3. `mono-input/modules/<module>/module.json` 与 `spec.md`

`migration-plan.md` 是 prepare 阶段已有的输入；pre-mono 必须更新它，并交付另外三个文件，最终四个文件都必须是工作区内的非空文件。文件推荐直接放在工作区根目录，也可以位于工作区其他子目录。
完成后让宿主把实际位置写入工作区 `state.json`。
兼容旧工作区时可读取历史 `module-divsion.json` 拼写，但交付文件使用 `module-division.*`。

JSON 至少记录模块的 source_files、target、depends_on、verification、status、description，顶层记录 order、driver_home、unknown。
所有源文件必须分配到模块，或明确标为 unassigned/blocked；未知依赖和冲突必须写入 unknown。
保留证据路径和变更理由。没有涉及的 runner.md 能力项留空；成功和失败的迁移命令都可以记录。
完成后只需说明已写入的文件和阻塞项。"""

    def build() -> dict:
        log_dir = ws / "handoffs" / "tasks" / "pre-mono" / "agent-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        checked = {}

        def validate(_parsed):
            paths = canonicalize(ws, locate(ws, record=False))
            runner_path = ws / "runner.json"
            if runner_path.is_file():
                if not isinstance(json.loads(runner_path.read_text(encoding="utf-8")), dict):
                    raise ValueError("runner.json must be a JSON object")
            division = json.loads(paths["module-division.json"].read_text(encoding="utf-8"))
            plan = json.loads(paths["migration-plan.json"].read_text(encoding="utf-8"))
            for name, data in (("module-division.json", division), ("migration-plan.json", plan)):
                if not isinstance(data, dict) or not isinstance(data.get("order"), list) or not data["order"]:
                    raise ValueError(f"{name} requires a nonempty order array")
            modules = division.get("modules")
            if not isinstance(modules, dict):
                raise ValueError("module-division.json requires a modules object")
            for name in division["order"] + plan["order"]:
                if not isinstance(name, str) or name not in modules:
                    raise ValueError(f"order references missing module: {name!r}")
                mdir = (ws / "mono-input" / "modules" / name).resolve()
                if not mdir.is_relative_to((ws / "mono-input" / "modules").resolve()):
                    raise ValueError(f"module path leaves workspace: {name}")
                for filename in ("module.json", "spec.md"):
                    path = mdir / filename
                    if not path.is_file() or not path.read_text(encoding="utf-8").strip():
                        raise ValueError(f"Missing nonempty mono input: {path}")
                if not isinstance(json.loads((mdir / "module.json").read_text(encoding="utf-8")), dict):
                    raise ValueError(f"{mdir / 'module.json'} must be an object")
            checked.update(division=division, plan=plan, paths=paths)
            return True, "Module inputs validated"

        previous = log_dir / "decompose.seq.json"
        resume = None
        if previous.is_file():
            try:
                resume = json.loads(previous.read_text()).get("session_id")
            except ValueError:
                pass
        outcome = agent.run_agent_seq(
            prompt, ws, str(log_dir / "decompose"), agent_budget_sec=1800,
            resume_session=resume, complete_check=validate,
            handoff_inputs=(ws, ("prepare",)),
            task={"phase": "pre-mono", "task_id": "pre-mono"})
        if outcome.get("status") != "done" or (outcome.get("parsed") or {}).get("status") == "blocked":
            raise RuntimeError(f"pre-mono incomplete: {outcome.get('status')}; inspect {log_dir}")
        write_state(ws, checked["paths"])
        return {**checked, "output": outcome.get("parsed")}

    try:
        result = run_task(
            ws, TaskSpec("pre-mono", dependencies=("prepare",),
                         materials=tuple(prepare_paths[name] for name in ARTIFACTS
                                         if name in prepare_paths),
                         description="Decompose prepare output into executable mono inputs."),
            build,
            success=lambda value: bool(value["division"].get("order")),
            summary=lambda value: f"pre-mono produced {len(value['division']['order'])} modules.",
            # Plans are mutable workspace inputs; runner.md is append-only and
            # require_success validates its presence while allowing growth.
            artifacts=lambda _value: [p for p in (ws / "runner.md",
                                                   ws / "runner.json")
                                      if p.is_file()],
            verification=lambda value: ["four module division and migration plan artifacts are present and parseable",
                                        f"module order: {value['division'].get('order', [])}"],
        )
    except (OSError, ValueError, RuntimeError):
        return 1
    return 0 if result else 1
