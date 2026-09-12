"""Turn prepare's advisory plan into the smallest executable mono handoff."""

from __future__ import annotations

import json
from pathlib import Path

from .common import agent
from .handoff import latest_success, publish_handoff, require_success, run_task, TaskSpec
from .workspace import ensure_runner


def _ensure_prepare(ws: Path) -> None:
    """Import a completed old workspace once; new runs publish normally."""
    if latest_success(ws, "prepare"):
        ensure_runner(ws)
        require_success(ws, "prepare")
        return
    state = ws / "prepare" / "state.json"
    if state.exists():
        value = json.loads(state.read_text(encoding="utf-8"))
        if value.get("status") == "complete":
            ensure_runner(ws)
            publish_handoff(ws, "prepare", summary="Imported completed prepare state.",
                            artifacts=[ws / "project.json", ws / "goals.md", state,
                                       ws / "runner.md", ws / "plan" / "migration-plan.md"],
                            verification=["historical prepare state was complete"])
            return
    raise ValueError("pre-mono requires a successful prepare handoff")


def run(ws: Path) -> int:
    ws = Path(ws).resolve()
    _ensure_prepare(ws)

    prepare = require_success(ws, "prepare")
    handoff_record = prepare.get("record") or prepare.get("record_data")
    prompt = f"""你是 pre-mono 主 agent。请完成一次真实的迁移前模块划分任务，而不是调用脚本做正则解析。

必须先阅读：
- prepare 成功 handoff：`{prepare.get('handoff', '见 prepare/index.json')}`
- prepare execution record：`{handoff_record}`
- `runner.md`（只记录迁移内部的模块编译、镜像编译、设备自启动、设备注入/交互和单元测试命令；Porter CLI 调用不要写入）
- `knowledgebase/README.md` 及与当前驱动有关的入口
- 源码树、目标树和 prepare 交付物中与模块边界有关的文件

请依据事实完成模块划分和计划修订，允许拆分、合并、改依赖和调整顺序。不要把 prepare 的建议当成固定命令。

必须写入工作区：
1. `module-divsion.md` 和 `module-divsion.json`（严格保留 divsion 拼写）
2. 更新后的 `migration-plan.md` 和 `migration-plan.json`
3. `mono-input/modules/<module>/module.json` 与 `spec.md`

JSON 至少记录模块的 source_files、target、depends_on、verification、status、description，顶层记录 order、driver_home、unknown。
所有源文件必须分配到模块，或明确标为 unassigned/blocked；未知依赖和冲突必须写入 unknown。
保留证据路径和变更理由。没有涉及的 runner.md 能力项留空；成功和失败的迁移命令都可以记录。
完成后只需说明已写入的文件和阻塞项。"""

    def build() -> dict:
        log_dir = ws / "handoffs" / "tasks" / "pre-mono" / "agent-logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        rc, output = agent.run_agent(prompt, ws, str(log_dir / "decompose"),
                                     timeout_sec=1800,
                                     task={"phase": "pre-mono", "task_id": "pre-mono"})
        if rc != 0:
            raise RuntimeError(f"pre-mono agent failed ({rc}); inspect {log_dir}")
        division = json.loads((ws / "module-divsion.json").read_text(encoding="utf-8"))
        plan = json.loads((ws / "migration-plan.json").read_text(encoding="utf-8"))
        if not isinstance(division, dict) or not division.get("order"):
            raise ValueError("agent produced an empty module-divsion.json")
        if not isinstance(plan, dict) or not plan.get("order"):
            raise ValueError("agent produced an empty migration-plan.json")
        for required in ("module-divsion.md", "migration-plan.md"):
            if not (ws / required).is_file() or not (ws / required).read_text(encoding="utf-8").strip():
                raise ValueError(f"agent did not produce {required}")
        modules = division.get("modules") or {}
        for name in division["order"]:
            if name not in modules:
                raise ValueError(f"module-divsion.json order references missing module: {name}")
            mdir = ws / "mono-input" / "modules" / name
            if not (mdir / "module.json").is_file() or not (mdir / "spec.md").is_file():
                raise ValueError(f"agent did not produce mono input for {name}")
        return {"division": division, "plan": plan, "output": output}

    try:
        result = run_task(
            ws, TaskSpec("pre-mono", dependencies=("prepare",),
                         materials=tuple(p for p in (ws / "migration-plan.json",
                                                     ws / "plan" / "migration-plan.md") if p.exists()),
                         description="Decompose prepare output into executable mono inputs."),
            build,
            success=lambda value: bool(value["division"].get("order")),
            summary=lambda value: f"pre-mono produced {len(value['division']['order'])} modules.",
            artifacts=lambda _value: [ws / name for name in
                                      ("module-divsion.md", "module-divsion.json",
                                       "migration-plan.md", "migration-plan.json",
                                       "runner.md")],
            verification=lambda value: ["module division and migration plan are parseable",
                                        f"module order: {value['division'].get('order', [])}"],
        )
    except (OSError, ValueError, RuntimeError):
        return 1
    return 0 if result else 1
