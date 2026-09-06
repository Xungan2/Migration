# Multica 多 session 交接源码调研

调研日期：2026-09-05。范围：Porter handoff 设计的参考机制，不包含实现方案定稿。

已实际浏览 [官方仓库](https://github.com/multica-ai/multica)，并读取官方源码副本。
全部源码结论固定到 commit [`7a438bd5b8bf39afd54259a7eb0971390e50a8ef`](https://github.com/multica-ai/multica/commit/7a438bd5b8bf39afd54259a7eb0971390e50a8ef)；下文链接均为该版本。
本次仅静态核读代码和已有测试，没有启动 Multica 或运行其测试。测试存在表示作者明确表达了行为契约，不表示本次验证通过。

## 结论

Multica 的主要交接路径是持久化 issue/comments，由评论触发 run，协调者醒来后重新判断下一步。
它还实现了同 parent 下的 stage barrier 和最终失败回传，但没有在本次检索中发现通用依赖 DAG 的自动门控与成果打包器。
名字最接近需求的 `handoff_note` 实际是旧客户端在分配任务时传入的范围说明，不能直接当作执行者的交接总结。
上述结论分别依据 [squad 协调协议][squad]、[stage barrier][stage]、[legacy API 字段][handoff-api]。

## 1. 区分业务任务、执行尝试与会话

| 对象 | 已实现的数据与职责 |
|---|---|
| `Issue` | 长期业务目标；含 description、status、assignee、parent_issue_id、stage、revision。 |
| `AgentTaskQueue` | 一次执行/run；含 issue_id、agent_id、runtime_id、status、result、error、session_id、work_dir。 |
| 执行沿袭 | `delegated_from_task_id` 表示委派来源；`retry_of_task_id` 与 `rerun_of_task_id` 区分自动重试和人工重跑。 |
| `Comment` | issue 上的共享信息；含 content、parent_id、source_task_id、revision，可以追到作者和来源 run。 |
| provider session | 执行器的会话指针；不是跨 agent 共享的交接记录。 |
| `TaskMessage` | 按 task_id/seq 保存工具调用、输入输出及消息，服务执行日志。 |

字段证据见 [执行模型][task-model]、[Issue/IssueDependency 模型][issue-model]、[Comment 模型][comment-model]、[TaskMessage 模型][message-model]。
同一 issue 的普通后续 run 按 `(agent_id, issue_id)` 查找之前的可恢复 session，并限制在匹配 runtime 上恢复。
人工 rerun 则定位被点击的源 task，避免误用同 issue 上其他执行的上下文。[session 选择实现][session]

恢复失败时，daemon 可在满足条件时尝试一次 fresh session，清空旧指针、重建冷启动评论读取指令并标明连续性缺口；永久拒绝的 session 会记录为 retired。
该路径重建同一业务任务的会话，不生成供其他依赖节点消费的总结。不能把 provider session 恢复或 compaction 当成业务 handoff。[fresh retry 实现][fresh-retry]

## 2. `handoff_note` 的作者、写入时机和范围

`UpdateIssueRequest.HandoffNote` 来自执行分配/状态提升写入的客户端；API 注释明确说明 UI 已移除，该字段为兼容旧客户端保留。
只有该写操作实际启动 run 时才消费它，字段不会存到 issue 本体。[API 边界][handoff-api]

`EnqueueTaskForIssueWithHandoff` 将文本写入新 task 的 `handoff_note`。
daemon 在 assignment 分支把它作为分配者对本次 run 的范围指令，放入每轮 prompt；无 note 时不增加相关段落。[入队实现][handoff-enqueue]、[prompt 分支][handoff-prompt]

因此它不是执行者退出前生成的总结，不是依赖节点的输出，也没有自动从上一 session 提炼内容的调用链。
它和 issue comment、task `result` 是不同载体。旧 migration 提及 `issue_context.md`，当前行为应以实际 prompt 路径为准。[migration][handoff-migration]、[现有 prompt 测试][handoff-test]

## 3. 成果如何留下来：提示词要求强，完成条件较弱

runtime brief 要求 agent 将 final results 通过 `multica issue comment add` 发到 issue；comment-triggered run 回复对应线程。
普通 agent 的文本指令明确称该步骤 mandatory；squad leader 的 `no_action` 有例外。[工作流提示词][workflow]

`CompleteTask` 先在事务中写入 task 的 `result`、状态和 session/workdir 等执行信息。
随后检查该 agent 自本次 `started_at` 后是否发过 comment；若没有，尝试将 final `Output` 补发为 comment。[完成实现][completion]

这个后备机制不等于“必须落一份完整总结”：

- 任意 progress comment 也能满足“曾发过评论”，从而跳过最终输出补发。
- 空输出、成功记录的 leader `no_action`、comment-triggered 的平凡 `Done` 输出可不补发。
- 超长后备输出被替换为查看 execution log 的提示，不保留完整正文。
- 后备 comment 在完成事务之后写入，辅助函数写失败可直接返回，task 已完成不会回滚。

这些边界均可从 [完成路径][completion]、[补发 comment 辅助函数][comment-fallback]、[输出长度处理][fallback-limit] 核验。
本次全仓检索未发现 `result_summary` 专用字段；已核读的任务结果、评论、日志三者也没有统一的结构化成果契约。
这限定为该 commit 的搜索结论，不代表其他分支或私有部署没有相关实现。

## 4. 下游上下文：指定触发输入，加按需读取评论

新 assignment 的 prompt 要求先 `issue get`，再扫描评论线程摘要，按需展开相关线程。
comment-triggered run 则直接收到触发评论正文及已合并评论，同时获得线程 ID 和继续查询命令。[prompt 构建][prompt]

当前策略是先 `--roots-only --summary --compact` 扫描，再 `--thread <id> --tail 30` 展开；不是无条件注入全部历史。
恢复 session 时会考虑自上次 run 的 `started_at` 起出现的新评论；锚定 started_at 可包含上次执行期间的新信息。
“已成功检查、没有新增”与“没有检查成功”由 `NewCommentsDeltaKnown` 区分，不能都解释成零条新信息。[增量判断][session]、[冷/热会话提示][prompt]

在这些路径中，选取单位是 issue 评论及触发/合并 ID，没有看到根据依赖边选择各上游完整交接包的逻辑。
同 issue 的信息由执行者自行综合；跨子 issue 的 stage 通知只给进度计数和代表性子 issue 引用，并要求 parent agent 综合成果。[stage 通知][stage-comment]

## 5. 值得借鉴：投递计划与实际投递回执分开

`coalesced_comment_ids` 是计划覆盖的输入集合，`delivered_comment_ids` 是本次 claim 实际装入响应的集合。
claim 以固定字节预算保留 primary trigger，再按稳定顺序容纳其他评论；未加载或预算外的输入不能记为已投递。[claim 输入构建][delivery]

回执写入使用 task/runtime/status/dispatched_at 等条件校验，并保证回执是计划的子集。
重新 claim 时替换回执；重试继承原计划，但清空回执，让新执行重新取得交付证据。[回执 SQL][receipt]、[retry SQL][retry]

完成后 reconciliation 查找已计划但未交付、或执行期间新增且应路由给该 agent 的评论，合并为后续 run。
它有明确的防循环边界，不把每条普通 agent 回复都重放成唤醒。[补偿实现][reconcile]

注意：这是“正文进入 claim 响应”的回执，不能证明模型理解、采用或完成了该输入。
现有测试 `TestClaimTaskByRuntime_PayloadOverflowReceiptsOnlyEmbeddedPrefix` 覆盖溢出后补跑；
`TestCreateRetryTask_PreservesCommentPlanAndResetsReceipt` 覆盖重试保留计划、重置回执。[测试][delivery-test]

## 6. 依赖调度：stage barrier 已实现，通用 DAG 门控未找到

同 parent 下的子 issue 可用 `stage` 整数分组。
无 stage 的兄弟集合在所有子项 terminal 后关闭；有 stage 时，完成项所在 S 阶段要求所有 `stage <= S` 子项 terminal。
terminal 的实际谓词只接受 `done`/`cancelled`；混合 staged/unstaged 时，unstaged 子项不参与 staged barrier。[实际谓词与算法][stage]

关闭 barrier 后，server 发 system comment 并显式唤醒 parent 的 agent 或 squad leader。
server 不自动启动所有下游；创建/提升下一阶段由醒来的 agent 判断。代码明确说明没有声明式 workflow model，不能因为暂时没有下一 stage 就认定工作流结束。[阶段推进说明][stage-advance]

`issue_dependency` 表及索引确实存在；本次对 server/apps/packages 检索其表名和 `depends_on_issue_id`，只找到 schema、生成模型、索引和删除清理，未找到据此门控 claim 的实现。
实际 claim 查询主要处理 runtime 可用性和同 `(issue, agent)` 的执行串行化，允许不同 agents 同 issue 并行。[依赖模型][issue-model]、[claim SQL][claim]

fan-in 有 stage 汇合和评论合并两种局部实现。批量子项更新在所有写入完成后统一读最终 sibling 状态，避免中途过时进度抢占唯一 pending run。
但 stage 通知是 status 更新后的 best-effort 副作用，不能称为通用、原子提交的 DAG 完成事件。[批量 barrier 与注释][stage-batch]

## 7. 失败时的反向交接，比“成功总结”更明确

最终失败且没有自动 retry pending 时，系统沿 `delegated_from_task_id` 找源 coordinator，创建一条平台 system/progress_update comment。
内容含 failed task ID、failure reason、最多 800 rune 的诊断摘要及 source coordinator task ID，要求协调者检查后重新分配、跳过或显式结束流程。[失败回传][failure]

该记录按 failed task 去重，支持补偿扫描与投递回执结算；有 retry pending 时暂不唤醒 coordinator，避免重复协调。
现有测试覆盖一次失败仅产生一次回传、sweeper 发现失败也能回传、retry pending 不回传。[失败测试][failure-test]

如果进程没来得及写业务总结，系统留下的是已上报日志/错误和失败事实，不会凭空重建“改了什么”。
daemon 正常结束会尽力上报 session/workdir/branch；若 complete 回调重试耗尽，代码选择保留 running，并把持久 pending queue 写为未来修复，不能宣称所有终止结果都保证持久化。[终止上报边界][terminal]

## 8. 对 Porter 的设计启示（建议，不是 Multica 事实或已定方案）

本地对照已核实：Porter 按模块拓扑序串行执行 P3→P4→P5；依赖图来自源 C 符号引用，无法覆盖共享 `lib.rs`、映射表等产物的修改关系。[循环](../../porter/loop/run.py)、[构图](../../porter/divide/resolve.py)
P4 每个切片新开 `run_agent_seq`，返回的 `files/notes` 未进入下一片 prompt，`migration.json` 只保留切片位置与 `ok/blocked`。
代码在 `p4.py:297` 记录过后片覆盖前片 1472 行成果的事故；现有文件清单刷新和行数守卫仍没有提供前片的工作说明，可作为首个 handoff 验收场景。[P4 实现](../../porter/loop/p4.py)
现有日志 stem 在跨命令重跑时会复用，部分证据文件可被覆盖；新交接记录的执行身份与证据版本需要明确，不能直接将旧 run_id 当不可变记录。[agent 实现](../../porter/common/agent.py)

1. 将交接数据与调度就绪分开：handoff 负责记录、筛选、注入，现有依赖调度是否改变另行决定。
2. 将执行者总结与宿主事实分开：总结描述决策/剩余事项，宿主记录 task/session、状态、产物引用及版本；不能仅靠 prompt 宣布 mandatory。
3. 若下游依赖某项成果，保存这次实际交付的上游记录 ID/版本；截断和缺失应显式可见，不能把计划输入当已交付。
4. 成功、失败、中断分别定义结果；允许失败记录缺少总结，同时让下游知道缺口，避免将取消等同于成果可用。
5. 防止使用过时成果需要显式策略。Multica 有评论 revision、claim 回执条件检查、PR review `head_sha` 去重，但本次没有找到统一的依赖产物版本/失效传播机制。[版本相关证据][comment-model]、[receipt]、[review-sha]

## 源码索引

[prompt]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/daemon/prompt.go
[squad]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/handler/squad_briefing.go#L31-L89
[fresh-retry]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/daemon/daemon.go#L8203-L8269
[task-model]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/pkg/db/generated/models.go#L109-L175
[issue-model]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/pkg/db/generated/models.go#L756-L792
[comment-model]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/pkg/db/generated/models.go#L538-L557
[message-model]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/pkg/db/generated/models.go#L1312-L1322
[session]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/handler/daemon.go#L2595-L2729
[handoff-api]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/handler/issue.go#L3131-L3134
[handoff-enqueue]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/service/task.go#L1177-L1182
[handoff-prompt]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/daemon/prompt.go#L217-L228
[handoff-migration]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/migrations/122_task_handoff_note.up.sql
[handoff-test]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/daemon/handoff_prompt_test.go#L8-L27
[workflow]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/daemon/execenv/runtime_config_sections.go#L710-L723
[completion]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/service/task.go#L4311-L4472
[comment-fallback]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/service/task.go#L7206-L7240
[fallback-limit]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/service/task.go#L154-L177
[delivery]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/handler/daemon.go#L2490-L2523
[receipt]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/pkg/db/queries/agent.sql#L808-L849
[retry]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/pkg/db/queries/agent.sql#L480-L511
[reconcile]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/handler/daemon.go#L4001-L4117
[delivery-test]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/handler/daemon_comment_delivery_test.go#L585-L714
[stage]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/handler/issue_child_done.go#L353-L428
[stage-comment]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/handler/issue_child_done.go#L272-L350
[stage-advance]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/handler/issue_child_done.go#L470-L499
[stage-batch]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/handler/issue_child_done.go#L149-L258
[claim]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/pkg/db/queries/agent.sql#L738-L806
[failure]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/service/task.go#L5941-L6142
[failure-test]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/service/delegated_failure_recovery_test.go#L618-L670
[terminal]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/internal/daemon/daemon.go#L5883-L6008
[review-sha]: https://github.com/multica-ai/multica/blob/7a438bd5b8bf39afd54259a7eb0971390e50a8ef/server/pkg/db/queries/agent.sql#L1747-L1768
