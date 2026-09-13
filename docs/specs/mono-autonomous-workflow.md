# Mono 自主迁移工作流

## Problem Statement

`prepare` 能交付源代码范围、目标树骨架、建议性的 migration plan、知识库和验收记录，但 `mono` 把这些结果当作固定且完整的输入。模块边界、依赖顺序、目标接口、构建登记、测试和遗留 TODO 一旦不完整，agent 就只能停车或越过约束继续推进。中间缺少一个把建议转换为可执行模块输入的 `pre-mono` 任务。

这使 `mono` 难以完成迁移主体工作，也无法消化 prepare 留下的 AUTO-TODO、AUTO-FIXME 和决策。需要一个能阅读并核实 prepare 输出、根据目标树事实补齐输入、用证据修订计划、完成模块迁移并在终局执行必要验收的自主流程。

## Solution

增加 `pre-mono` 大任务，并修改 `mono` 的编排和交接 seam。`pre-mono` 消费 prepare 的全部输出与目标树事实，把建议性的 plan 转成可执行模块清单；它可以自主拆分模块划分、依赖核实、目标落点和验证面等子任务。prepare plan 是起点而非不可变命令；mono 主 agent 仍可以自主决定模块顺序、模块边界、依赖、迁移方法、接线范围和测试方法，并把变化记录为最新事实和可追溯变更。

每个模块完成后同步 mappings、module facts 和 verification knowledge，按节流 hook 领取可处理的 prepare TODO。缺失或有争议的信息先由主 agent 做有界核实，再派一个带预算的 research agent；仍无法解决的必要问题进入 blocked，生成或更新 `HUMAN.md`。全部模块完成后，编译和驱动自启动是阻断验收；设备注入和设备交互是允许失败的 option 验收。

## User Stories

1. As a mono 主 agent, I want to read all prepare outputs, so that I can start from the actual handoff rather than an incomplete fixed schema.
2. As a mono 主 agent, I want to inspect source and target trees when input is missing, so that prepare gaps do not automatically block migration.
3. As a mono 主 agent, I want to verify prepare claims against target-tree facts, so that stale knowledge cannot silently drive implementation.
4. As a mono 主 agent, I want to choose migration order, so that I can prioritize work that is currently unblocked.
5. As a mono 主 agent, I want to split, merge, or reassign modules with evidence, so that the plan reflects real code boundaries.
6. As a mono 主 agent, I want to change dependencies with evidence, so that false dependencies do not prevent progress.
7. As a mono 主 agent, I want to recalculate affected modules after a plan change, so that passed work is revisited when necessary.
8. As a mono 主 agent, I want to choose the migration strategy per module, so that platform facts determine implementation.
9. As a mono 主 agent, I want to extend wiring scope when target-tree evidence requires it, so that required integration files can be changed safely.
10. As a mono 主 agent, I want to modify an existing artifact when it is demonstrably wrong, so that historical mistakes do not become permanent constraints.
11. As a mono 主 agent, I want every scope and plan change recorded with evidence, so that autonomy remains reviewable.
12. As a mono 主 agent, I want to maintain a latest factual migration plan, so that later modules consume current facts.
13. As a maintainer, I want a separate change history, so that the original prepare recommendation and later decisions remain distinguishable.
14. As a mono 主 agent, I want mappings and contracts in a dedicated knowledgebase area, so that API decisions are reusable.
15. As a mono 主 agent, I want to publish module facts after each module, so that later agents inherit actual implementation knowledge.
16. As a maintainer, I want verification knowledge synchronized before a task ends, so that active knowledge does not remain stale.
17. As a mono 主 agent, I want to claim eligible AUTO-TODOs at controlled points, so that deferred work progresses without interrupting every function.
18. As a maintainer, I want TODO, FIXME, and decision entries to share lifecycle states, so that stages interpret outstanding work consistently.
19. As a mono 主 agent, I want to mark work passed only with current-stage evidence, so that completion cannot rely on an old result.
20. As a maintainer, I want AUTO-FIXME to record whether a problem passed the current stage gate, so that known failures remain visible after a workaround.
21. As a mono 主 agent, I want bounded local investigation before escalation, so that simple gaps are solved without another session.
22. As a mono 主 agent, I want one budgeted research-agent attempt for unresolved necessary work, so that difficult questions receive focused investigation.
23. As a maintainer, I want blocked necessary work to produce HUMAN.md, so that a human can resume it with evidence and context.
24. As a migration owner, I want full compilation after all modules, so that the target tree is buildable as a whole.
25. As a migration owner, I want the driver to self-start after all modules, so that module-local success is not mistaken for system integration.
26. As an operator, I want optional device checks recorded even when they fail, so that useful evidence is retained without weakening required acceptance.
27. As an operator, I want resumable state after a blocked module or failed gate, so that valid completed work is retained.
28. As a pre-mono 主 agent, I want to turn the prepare recommendation into an executable module manifest, so that mono receives actionable work rather than a loose plan.
29. As a pre-mono 主 agent, I want to choose and delegate decomposition sub-tasks, so that the preparation method adapts to the driver.
30. As a mono 主 agent, I want incomplete but non-blocking manifest items to remain explicit, so that I can continue independent migration work.

## Implementation Decisions

- Reuse the existing mono orchestration seam; do not create a second workflow engine.
- Add `pre-mono` as a bounded preparation phase. Its main agent may split the work into any useful sub-tasks; sub-task count, roles, and order are not fixed.
- `pre-mono` is an agent-owned task. It consumes the prepare success handoff, `runner.md`, knowledgebase, and source/target facts, then produces `module-division.md/json`, the latest `migration-plan.md/json`, and module input specs.
- The manifest must assign every in-scope source file to one module or explicitly mark it `unassigned`/`blocked`; each module records source files, target landing, dependencies, status, and a verification surface.
- Unknown dependencies and conflicts are explicit. Non-blocking unknowns may enter mono; unknowns that prevent necessary acceptance or all progress block and produce `HUMAN.md`.
- `pre-mono` is content-validated rather than workflow-validated: no fixed decomposition recipe is required.
- Prepare planning acceptance requires a non-empty `migration-plan.md`; pre-mono updates it and delivers
  `module-division.md/json` plus `migration-plan.json`. Their relative paths are recorded in root
  `state.json`; pre-mono and mono read that index first and recursively repair it when paths are missing.
- Discover inputs from project/goals, the prepare success handoff, knowledgebase, `runner.md`, and target-tree facts. The pre-mono agent fills missing fields by bounded inspection; the host only validates and records the handoff.
- Maintain the latest factual migration plan separately from `change.md`. Agents own content; the orchestrator checks parseability, evidence presence, and dependency consistency.
- Each change records prior value, new value, reason, evidence, affected modules, and rollback condition.
- The agent chooses order. A plan change triggers dependency and cycle checks and marks affected passed modules for regression.
- Existing artifacts and wiring files may be changed when evidence requires it. Changes stay inside the configured workspace/target repository and are recorded.
- Use `knowledgebase/mappings/`, `knowledgebase/modules/`, and `knowledgebase/verification/`. AUTO-TODO, AUTO-FIXME, and AUTO-DECISION remain at established locations.
- Synchronization deduplicates current facts and resolves conflicts against target-tree evidence before module completion.
- Work items use `open`, `claimed`, `passed`, and `blocked`. `passed` requires current-stage evidence; blocked necessary work updates `HUMAN.md`.
- Hooks run at module completion, necessary-gate failure, or explicit request. A module key and failure signature suppress duplicate triggers.
- Escalation is bounded inspection, then one budgeted research-agent attempt, then blocked plus `HUMAN.md`.
- Final blocking acceptance is full compile and driver self-start after all modules. Device injection and interaction are optional and their failures remain verification records.
- The orchestrator protects repository boundaries, required acceptance, evidence/coverage records, and outstanding work. It does not prescribe module order, helper choice, or a fixed recipe.

## Testing Decisions

- Test the existing CLI/workspace orchestration seam through real temporary workspaces and provider substitutes, following current tests.
- Assert persisted inputs, latest plan, change history, affected-module invalidation, knowledge synchronization, work-item transitions, hook throttling, HUMAN.md, and gate results.
- Cover missing prepare fields repaired by tree inspection, plan split/merge and dependency changes, existing-artifact edits, and wiring-scope extension.
- Cover successful research escalation, budget exhaustion, and blocked necessary work.
- Cover TODO hooks at module completion and necessary-gate failure, including duplicate suppression.
- Cover final compile/self-start as blocking and device checks as non-blocking while retaining evidence.
- Do not assert prompt wording, role choice, module order, or internal helper calls.

## Out of Scope

- Replacing prepare.
- Making device injection or interaction blocking.
- Synchronizing this workspace knowledgebase to a global service.
- Edits outside the configured workspace and target repository.
- Removing validation, evidence requirements, necessary gates, or HUMAN.md escalation.
- Fixed parallelism, fixed roles, fixed retries, or a universal migration recipe.

## Further Notes

The prepare plan is advisory. The current migration plan is the factual source for subsequent mono work; `change.md` explains how it changed. A module is complete only when implementation, work-item state, and knowledge synchronization agree. Final system acceptance is deferred until all modules are present.
