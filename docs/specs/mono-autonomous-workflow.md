# Mono 自主迁移工作流

## Problem Statement

`mono` 需要把 `prepare` 的建议真正落地，但当前输入契约和执行限制过严，无法自主补齐信息、调整计划、处理遗留 TODO 或修复迁移问题。

## Solution

`mono` agent 读取 prepare 全部产物和目标树事实，自主完成模块迁移。prepare plan 只是起点；agent 可基于证据调整模块边界、依赖、顺序、迁移策略和接线范围，并持续更新事实与知识。

## User Stories

1. As a mono agent, I want to consume and verify prepare outputs, so that missing inputs can be filled from source and target trees.
2. As a mono agent, I want to change the migration plan with evidence, so that execution follows current facts.
3. As a mono agent, I want to choose a valid module order and fix affected modules after plan changes, so that dependencies remain correct.
4. As a mono agent, I want to handle eligible prepare auto-TODOs through throttled hooks, so that deferred work progresses without interrupting every step.
5. As a maintainer, I want current mappings, module facts and verification results synchronized, so that later agents have no stale active knowledge.
6. As a maintainer, I want blocked necessary work recorded in `HUMAN.md`, so that humans can resume it.
7. As an operator, I want full compile and driver self-start after all modules, so that migration has required system acceptance.

## Implementation Decisions

- Inputs: `project.json`, goals, prepare state/handoffs, migration plan, knowledgebase, and target-tree manifest/runner facts. Agent performs bounded inspection when fields are missing.
- Plan: maintain latest factual migration plan plus `change.md` (change, reason, evidence, affected modules, rollback condition). Agent owns content; orchestrator validates parseability and dependency cycles.
- Execution: agent chooses order. Plan changes can invalidate passed modules, which become regression work.
- Scope: edits stay in workspace/target repository. Existing artifacts and wiring may be changed when evidence requires it; every scope change is recorded.
- Knowledge: use `knowledgebase/mappings/`, `knowledgebase/modules/`, and `knowledgebase/verification/`. Synchronize and deduplicate before task completion.
- Work items: TODO/FIXME/decision entries share `open|claimed|passed|blocked`; `passed` requires current-stage evidence. Hooks run at module completion, necessary-gate failure, or explicit request, with per-module/failure throttling.
- Escalation: bounded inspection → one budgeted research-agent attempt → blocked plus `HUMAN.md`.
- Acceptance: after all modules, full compile and driver self-start are blocking. Device injection/interaction checks are optional and recorded even when they fail.

## Testing Decisions

Test the mono orchestration seam through existing CLI/workspace tests. Cover input discovery, plan/change persistence, dependency revalidation, hook throttling, escalation/HUMAN.md, knowledge synchronization, and required versus optional acceptance. Assert persisted state and gate outcomes, not helper internals.

## Out of Scope

Replacing prepare; making optional device checks blocking; global knowledgebase sync; edits outside configured workspace and target repository.
