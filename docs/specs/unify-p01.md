# 统一 P0/P1：主 agent 调度、分别验收与持续知识沉淀

发布状态：本地规格；需求与测试入口均已确认。项目尚未配置 issue tracker 和标签映射，未发布 issue，未应用标签。配置后发布并应用 `ready-for-agent`，无需额外 triage。

## Problem Statement

用户希望从 Linux 驱动源码和迁移意图出发，一次得到可以构建并实际载入的目标 OS 原生驱动骨架，以及可供后续执行的建议性迁移规划。现有 P0 已经调查迁移范围、源码依赖和目标 OS 接线，独立 P1 再做相关调查会重复工作，也使知识与交接分散在阶段之间。

当前新 P0 以执行 agent 自验为主，而旧 P1 和下游仍依赖源码物理抽取、结构化模块方案和依赖图等旧协议。阶段编排和历史兼容带来大量 Python 文件与入口，掩盖了真正需要的职责。共享知识由执行者汇总，缺少持续负责一致性的专门维护者；固定大文档或全量注入又会增加后续任务上下文。

用户要求在新的功能分支中只实现新架构，精简代码组织，保留必要的输入校验、失败恢复、执行证据和测试。

## Solution

提供统一的迁移准备流程，由一个主 agent 对结果负责，按需派发子任务，共享源码调查和源/目标 OS 知识。取消固定的 P0/P1 副 agent 层级，不要求两个职责各占一个 agent，也不把原来的两个阶段机械串起来。

统一流程保留两项分别记录的验收职责：

| 职责 | 完成标准 |
| --- | --- |
| 骨架构建与载入 | 构建成功；有框架实际参与编译的证据；通过目标 OS 支持的方式实际载入驱动并有执行证据；满足用户明确指定的本阶段特殊要求 |
| 迁移规划 | 确定迁移范围、划分迁移模块、整理依赖顺序，形成建议性迁移规划；未知项及相应决策、待办如实记录 |

只有主 agent 作出验收结论。两项都通过，且最后一批知识已经沉淀，统一流程才完成。一项通过、另一项阻塞时保留有效成果，续跑针对受影响的内容推进，无需重跑所有子任务。

一个专门的知识 agent 通过 opencode 会话续接持续维护本工作区知识。子任务留下任务交接记录，主 agent 传递决策与验收结论，知识 agent 统一落盘、整理索引和处理冲突。所有任务按需读取相关交接与知识入口。

知识采用 Markdown 主题目录，目录内部由知识 agent 自主组织。主题覆盖源端分析、目标端集成、环境、构建、载入、测试和迁移规划。根目录与已创建的各主题目录都有简短 README 阅读入口；验收记录以及 AUTO-DECISION、AUTO-TODO、AUTO-FIXME 保留单文件总览。

## User Stories

1. As a migration user, I want one preparation workflow, so that I obtain both a working skeleton and a migration plan without starting a separate P1 task.
2. As a migration user, I want source investigation to be shared, so that overlapping responsibilities do not repeatedly analyze the same driver.
3. As a migration user, I want my intent to define included and excluded behavior, so that preparation remains aligned with the requested migration scope.
4. As a migration user, I want unknown environmental details to be investigated from source and evidence, so that I do not need to supply internal implementation classifications.
5. As a migration user, I want unresolved scope questions surfaced, so that the workflow does not silently change my requirements.
6. As a migration user, I want a successful build with evidence of skeleton participation, so that a successful unrelated build cannot count as completion.
7. As a migration user, I want actual driver loading evidence, so that declarations and static log statements cannot substitute for execution.
8. As a migration user, I want full device functionality to remain a later objective by default, so that preparation does not expand into the entire migration.
9. As a migration user, I want explicit special acceptance requirements honored, so that defaults do not override my instructions.
10. As a migration user, I want a plan describing scope, modules and dependency order, so that subsequent migration work has an actionable starting point.
11. As a migration user, I want planning unknowns recorded with decisions and follow-up work, so that a suggested plan does not pretend implementation uncertainties are resolved.
12. As a migration executor, I want to adjust module boundaries and order using new evidence, so that the plan can evolve during implementation.
13. As a migration user, I want changes to required functionality confirmed with me, so that a suggested plan cannot silently reduce the migration goal.
14. As a main agent, I want to dispatch tasks according to actual needs, so that task allocation is not constrained by fixed phase-specific agents.
15. As a main agent, I want sole dispatch authority, so that task ownership remains visible without nested delegation.
16. As a task executor, I want relevant earlier handoffs and knowledge entry points, so that I can continue useful work without loading every document.
17. As a task executor, I want to preserve findings after failure or interruption, so that subsequent work can reuse evidence and understand unfinished work.
18. As a main agent, I want one active code writer, so that concurrent research does not produce conflicting source edits.
19. As a main agent, I want to perform both acceptance decisions myself, so that task completion claims are not mistaken for accepted outcomes.
20. As a migration user, I want the two acceptance states recorded separately, so that partial progress remains visible when the overall workflow is blocked.
21. As a migration user, I want valid evidence retained on resume, so that completed work is not repeated without cause.
22. As a migration user, I want changed inputs and source revisions to trigger affected revalidation, so that stale success cannot satisfy the current requirements.
23. As a knowledge agent, I want exclusive responsibility for shared knowledge, so that multiple task writers do not create inconsistent current records.
24. As a knowledge agent, I want to resume an opencode session when new material arrives, so that knowledge maintenance continues without restarting its context each time.
25. As a main agent, I want unresolved knowledge conflicts escalated with evidence, so that documentation does not silently choose an unsupported conclusion.
26. As a task executor, I want knowledge organized into topic directories with small reading indexes, so that I can locate a relevant subset quickly.
27. As a knowledge agent, I want to split and reorganize topic documents as needed, so that document structure follows actual knowledge rather than a fixed template.
28. As a task executor, I want old document references to remain navigable, so that historical handoffs remain useful after knowledge reorganization.
29. As a migration user, I want decisions, pending work, faults and acceptance kept in concise current summaries, so that I can understand the state without reading raw logs.
30. As a migration user, I want knowledge to state evidence and applicability, so that verified facts are distinguishable from inference and unverified claims.
31. As a migration user, I want the final findings incorporated before completion, so that the delivered knowledge matches the delivered work.
32. As a maintainer, I want the branch to contain a compact implementation of the new architecture, so that redundant phase code and compatibility layers do not dominate maintenance.
33. As a maintainer, I want public-entry integration tests, so that simplifying internal files does not require rewriting tests for every internal rearrangement.
34. As a migration user, I want failures, timeouts and unavailable sessions to preserve durable records, so that I can resume without losing work or treating an incomplete run as successful.

## Implementation Decisions

- Implement on the already-created `feat/unifyp01` branch. Preserve unrelated existing work and target-tree changes. This specification does not authorize resetting migration workspaces or source trees.
- Collapse the public orchestration around the unified preparation workflow. The branch implements the new architecture, rather than carrying a second active legacy P0/P1 pipeline. Remove obsolete entry points, phase-specific modules, imports and tests whose behavior is intentionally retired; simplify retained code by its real callers. File-count reduction is an outcome, not a reason to remove necessary behavior.
- Reuse the existing opencode transport, session continuation, public CLI testing pattern and durable execution records where they serve the new workflow. Consolidate wrappers that no longer provide a separate responsibility. Avoid adding a generic scheduling framework, knowledge database or dependency for this change.
- Retain validation of source/target inputs, readable intent, writable target, budgets and workspace identity. Explicit new intent updates the workspace intent; omitted intent reuses the existing version. Revalidate conclusions affected by intent or relevant source/configuration changes.
- The main agent is the sole dispatcher and acceptance authority. It may perform simple work directly or delegate bounded tasks. Task executors do not dispatch further agents. Eliminate the fixed deputy-agent concept.
- Each dispatched task receives its objective, scope, relevant inputs, completion expectations and handoff destination. Select relevant prior handoffs and knowledge entry points rather than injecting the whole knowledge corpus.
- Independent investigation may run concurrently. At most one executor modifies the code at a time, and responsibility transfers only after the current writer finishes. Knowledge maintenance has its separate, exclusive document ownership.
- Task executors produce their own handoffs with findings, evidence, changes, unresolved items and follow-up links, including on failure or interruption. Handoffs are durable task records, not an alternative current knowledge store. Task executors may collect verification evidence, but their completion signals are not acceptance decisions.
- The main agent reviews current evidence for skeleton participation, successful build, actual load and the planning deliverables. Reuse still-valid evidence; rerun affected operations when evidence is missing or invalidated. Neither provider exit status nor an unsubstantiated success sentence establishes acceptance.
- Record the two responsibility states separately. Overall completion requires both accepted and knowledge caught up. Preserve the valid side when the other side is blocked. Host-side status handling must not turn a timeout, missing delivery or incomplete knowledge update into overall success.
- Planning covers migration scope, module responsibilities and source ownership, dependencies, and suggested migration order. It is a proposal for later work, not a requirement to physically extract source modules or produce the legacy machine-oriented P1 artifacts. This follows the request to implement only the new architecture.
- Planning unknowns are recorded in AUTO-DECISION and AUTO-TODO as appropriate, with known implications and follow-up information. Recording uncertainty does not establish an unknown fact as verified. The main agent determines whether the required planning deliverable has been provided.
- Evidence-based changes to module boundaries and order are allowed and recorded as decisions. Changes to user-required functional scope require user confirmation. Complete business functionality, full device claiming and unit-test success remain outside the default preparation gate unless explicitly required for this phase.
- Assign one dedicated knowledge agent across the workflow. Resume it through opencode when new handoffs or main-agent decisions arrive; it waits when there is no new input. Preserve enough durable state to recover its work when session continuation is unavailable, without treating volatile session memory as the knowledge source of truth.
- The knowledge agent is the only agent writing shared knowledge and AUTO summaries. It incorporates task findings and the main agent's decisions, including acceptance conclusions. It does not make acceptance decisions or silently overwrite conflicting findings. Resolve conflicts from evidence where possible; send unresolved conflicts to the main agent.
- Maintain only this workspace's knowledge. Annotate applicable versions, conditions and evidence. Cross-driver knowledge publication or synchronized global knowledge is not part of the workflow.
- Organize knowledge into the agreed topics: source analysis, target integration, environment, build, loading, testing and suggested migration planning. Create topic directories only when there is content. Every created topic has a short README with content descriptions and links; the knowledge root has an overall reading index. Internal filenames and nesting are agent decisions.
- Preserve single-file current summaries for verification, AUTO-DECISION, AUTO-TODO and AUTO-FIXME. Decisions explain current choices and superseded reasoning; pending work captures unknowns, triggers and completion conditions; faults preserve failures, effective fixes and revalidation. Keep detailed material in topic documents or execution evidence and link to it.
- Handoffs preserve what an individual task delivered; topic knowledge captures current applicable understanding. The plan describes a proposed migration route, while AUTO-TODO manages outstanding work. Use links to avoid duplicate authoritative copies of facts and progress.
- Allow the knowledge agent to move, split or merge documents. Update current indexes and retain a short redirect at the former location. Historical handoffs remain unchanged, so their references still lead to the relevant knowledge.
- Keep raw logs with execution records. Knowledge contains concise conclusions and evidence links, distinguishes fact/inference/unverified material, and documents commands with necessary execution context. Documents remain free-form Markdown rather than rigid section-parsing contracts.
- Before declaring overall completion, the main agent confirms that the knowledge agent has incorporated the final delivered findings and acceptance decisions. Knowledge recording does not require rerunning the underlying tasks.

## Testing Decisions

已确认的测试入口：沿用公开 CLI 作为主要、高层集成入口，使用真实临时工作区和 opencode provider 替身；真实目标 OS 构建与载入单独验证。不因内部模块合并而创建逐文件测试套件。

- 好测试观察输入、工作区产物、返回状态、任务边界与恢复结果，不绑定私有函数名、Python 文件数量、具体提示措辞或 agent 自由决定的文档层级。
- 测试覆盖统一编排、provider 续接及其持久化状态、handoff 交接、知识单写入者的调用边界和最终完成判定。优先经同一公开入口测试，不分别搭建新的低层测试入口。
- 先例是现有自主 P0 CLI 测试：真实临时源树和目标树、provider 替身、本地编译和动态载入，以及输入更新、恢复、超时和缺失证据场景。保留适用案例并按新职责改写；旧阶段专属案例随被移除行为退出。
- 完成矩阵至少覆盖：两项都通过并完成沉淀；仅骨架通过；仅规划通过；两项通过但知识未完成；provider 宣称完成却缺少本轮交付。后四者不能被报告为整体完成。
- 验证任务执行者的交付不会直接发布验收成功，最终状态来自主 agent；知识维护只能记录该结论，不能替代主 agent 验收。
- 验证多个新交接触发同一知识会话续接，处理过的结果可追溯；续跑或会话不可用时可从落盘材料恢复。最后一批知识处理失败或中断时，保存已有成果并保持未完成状态。
- 验证任务获得指定的相关 handoff/知识入口，而非全量知识正文；目录内部采取不同合法组织方式仍可交付，不要求固定文件数量。
- 验证知识重组后旧交接引用可经跳转找到新材料，且历史 handoff 未被改写。该行为可由 provider 替身交付多种组织结果，检查用户可见的可导航性。
- 验证继续执行时保留有效一侧的状态和证据，相关意图或代码变化使受影响结论需要复核；无依据的全部重跑不作为测试期望。
- 保留有价值的负向场景：无效输入不破坏已有意图；超时、中断、缺失可执行程序和 provider 失败不丢执行日志或错误标记成功；同一工作区的并发执行不会破坏状态。
- 实际 agent 是否正确理解源码、作出合理规划、正确解读日志，不能由 provider 替身证明。真实验证需在可控目标 OS 工作区运行统一流程，检查实际构建/载入、规划质量、按需交接和知识持续更新；结果如实区分“CLI 回归通过”和“真实目标验证通过”。

## Out of Scope

- 恢复旧 P1 的物理源码抽取、机械解环和结构化下游协议，或同时维护新旧两套执行架构。
- 实现、适配或重新开放 P2–P7、旧模块循环及历史实验迁移流程。
- 在迁移准备阶段完成全部设备业务行为、完整认领、端到端功能迁移，或默认把全部单测成功升级为门槛。
- 固定两个副 agent、子任务继续嵌套派发、额外引入独立验收 agent。
- 新建跨驱动全局知识库、自动晋升/同步知识或替换 Markdown 为数据库。
- 为所有主题预建空文档，固定主题内部文件结构，或用机械标题解析替代主 agent 的语义验收。
- 重置或删除现有迁移工作区、目标 OS 改动和与本次架构无关的用户工作。

## Further Notes

- 领域术语沿用项目词汇：迁移范围、迁移模块、迁移规划、任务交接记录和知识沉淀。统一流程合并执行组织，保留验收职责区别。
- 相关架构决策是“合并 P0 与 P1 的执行流程，保留分别验收”；本规格将已经完成的讨论整理为实施要求。
- 功能分支已创建。生成本规格不表示功能已经实现或验证；此前读取到的工作区包含大量已有未提交变更，后续实施应保留并理解它们。
- 未配置项目 issue tracker 和标签映射，无法选择正式发布目的地。按 to-spec 约定，运行 `/setup-matt-pocock-skills` 配置后，将本规格发布并应用 `ready-for-agent`；不能把技能自带的 tracker 模板视为本项目已配置的 tracker。
