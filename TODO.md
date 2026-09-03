# TODO（driver_migration_tool）

> 全局待办。按"已商定但不做在本轮"的原则记录；每条含背景与入手点。

## 1. 工作区级配置覆写通用机制

现状：仅 `routing.gates` / `routing.default` 支持工作区 `routing.json`
覆写（routing.py `load_routing`）；`checkpoints` / `policy_file` /
`panic` 阈值只认仓级 `porter/config.json`。

要做：设计统一的两级配置访问器（仓级默认 + 工作区覆写 + 特异性合并），
把 `_cp_config`、`debt_limit`、`policy_path` 等读取方全部收编。后续其他
功能（预计）也有工作区级覆写需求——做成通用机制而非逐功能补。

## 2. 工具 log 子系统重建（用户规划）——已完成

两轮落地：`porter/log/` 统一框架（record 双 sink；v1.1 附加字段且
phase 缺省回落 bind；run 登记 + prompt 归档 + context_block/
tail_block 上下文接续 API 并接入 P4/ut_verify；`porter log` CLI；
快照 >5MB 裁剪钳制；loop/events.py 转 re-export 门面零破坏）。
**print 全量收编完成**：试点走 record()，其余 284 处（30 文件）经
机械 codemod 统一 `_log.console_line`（byte 兼容 + 级别门控）。
规范 = `docs/sub-systems/log.md`。

**残余项（后续轮）**：真实 e2e 验证——留待下次真实迁移轮次顺带完成
（2026-09-03 用户定案）：跑完后用 `porter log tail/timeline/runs` 核验
新埋桩（prompt 归档 / judge 流 / 界标 / 自动 phase / **errorloop_round/
end**）的现场累积形态与体积预期。§15 重设计的读路径已接 query API
（errorloop 证据组装 + 轮间接续，2026-09-03 落地）。

## 3. §15 失败自诊重设计——已完成（2026-09-03）

重设计为**错误处理模块**（docs/modules/error-handling.md 为规范）：知识辅助的
agent 求解循环（≤3 轮 + 同签名早退 + 双信号复验），三挂载（p5/p6/d1）
+ unsolved 关口（attempts 在挂载点退役）；签名知识入 kb failures 域
（base+lineage 两级，去硬编码）；报告生成接线到耗尽处；criteria 修正
撤人工闸改决策债审计；旧机器（triage/diagnose 深诊/--defect-fix/
skills 三件/failures.md）删除，六案例 fixture 迁移为 test_replay 新
契约。直接生效（config enabled=true 兼熔断）。

**残余项（后续轮）**：首个真实迁移轮的实测校准——求解判定质量、
轮数/早退命中分布、kb_consulted 遥测（failures 域命中率）、criteria
修正债的审计实操（与 TODO #2 e2e 验证同轮顺带）。

## 4. p6 私有 boot 助手与共享版去重（审计 #19）

现状：p6.py `_boot_and_log` 已接共享的 `_recover_boot_log` /
`_log_face`（最小接入，保持 SLIRP 注入不动），但仍是独立实现（返回
四元组含去 ANSI 文本 vs 共享三元组）。完整去重 = 共享助手支持
extra_env 注入 + ANSI 变体，p6 改调共享版。

## 5. 知识分类子目录（taxonomy）完备性

现状：知识库子目录（maps/gaps/runbook/splits/pitfalls）即知识分类，
由工具静态决定（kb.py 域注册表、调用点域预选、skills/kb-guide.md
三处），新增分类须改代码。本轮已保证"加一类=单点改动"（注册表一行
+ skill 文本一节 + 域预选一行）。

问题：静态分类可能不全。已知压力点：方法类教训只能挤 pitfalls
（标签区分，长期稀释"平台坑"语义）；平台缺口/上游提案知识在
pitfalls 与工作区 platform_patches 两处漂移。~~§15 失败签名
（knowledge/failures.md）是事实上的第六类（域外待归位）~~
——已解决（2026-09-03）：failures 域归位（base+lineage 两级，
docs/sub-systems/knowledge.md §3.2）。

要做（后续轮）：
- 复盘一次真实迁移产生的全部候选，检验五类的实际覆盖度；
- 若需扩类：按单点改动路径加域；
- 备选方向：受控扩展机制（用户自建子目录只要带 INDEX 即被
  kb-guide 宣告可查）——可行性依赖 agent 行为稳定性，需实验评估。

## 6. 固定知识的差异化检索/使用设计

现状：本轮已将全部知识域统一为 INDEX 目录注入 + agent 自取
（kb_consulted 记咨询）。此前讨论中有三个"内容注入"候选因质量承重
被搁置：
- P3 增量映射的历史映射内容注入（原 collect_hints 机制——域过滤
  保证该看见的一定被看见）；
- T3 的 runbook"起点假设"内容注入（同目标 OS 相关性极强）；
- P1S 同驱动精确命中时注入完整 strategy（强相关）。

要做（后续轮）：
- 用 kb_consulted 遥测验证统一指针化后 P3 映射质量无回归
  （持续零咨询 = agent 不读 = 回归信号）；
- 若有回归，按"注入深度∝相关性"恢复局部内容注入，或加强
  kb-guide 措辞/调用点提示。

## 7. ktest 静默案的无会话复演验证（SIG-02）

背景：~3h 破案走了交互会话——越出运行模型（工具自动解决 ∨ 人工
经 gates 介入解决，交互会话不在模型内）。

问题：现有工具链（P5 判据失败 → events/快照留证 → attempts→panic
关口 → 人答 note → 重跑）能否不经交互会话解决此类问题？

要做：按时间轴复盘 ktest 案，逐环节检查工具当时给人的证据面
（判据 diff、unit_test 烟测输出、events）是否足以让人在 gate note
里给出可执行修复；找出"若没有会话，人在哪一步会卡住"——该环节
就是证据面缺口。产出 = 缺口清单，供知识子系统的 gate note 指引与
未来自诊重设计共用。

## 8. corpus→base 通用化晋升

现状：知识库目录（corpus）内的知识无通道回流 base（工具随附的
任意目标 OS 通用知识）；跨 lineage 通用化只能人手搬文件。

要做：`porter kb to-base` 类命令（人工触发；策展权必人语义同
promote；目标 base/<域>/）。

## 9. CP5 知识审核细节细研

现状：CP5 已扩为候选队列 + temp 草稿清点 + KB 健康报告（薄方案：
材料渲染 + kb promote/reject 命令，未新建问答协议）。

要做：细研批审交互——approve/reject 是否上关口表单（复用
answers.md）、健康报告的阈值化（多少零咨询该提示下架）、
classify 的 agent 提示词随分类错误的校准（改判日志已有数据）。

## 10. vcs 遗留项（2026-09 git 管理模块）

已完成：porter/common/vcs.py（两 repo commit 管理 + 分支/baseline
登记于 project.json["vcs"] + git bundle 跨机器导出导入 + P7 commit 链）；
知识库挪入 <ws>/knowledge/（随工作区 git 入库，promote 后
sync_to_global 回流全局库）；fill 平台补齐落点约束到
crate/src/external_interfaces.rs（骨架预置 mod）；agent 调用前后
隔离 commit（agent_pre/agent_post 成对，仅工作区仓——两仓 commit 流
独立，目标 OS 只按既定点提交，不为 agent 调用加点）；工作区仓
.gitignore（台账/exports 排除，与 pathspec 双保险）；接线层测试
tests/test_vcs_wiring.py（seam/隔离性/panic/answers/loop/P2/P4）。

遗留：
- 真实迁移轮 e2e 校准（首个跑完 P0→P7 的迁移验证 commit 粒度/
  分支切回/bundle 往返的实战表现）。
- resume 时目标仓被人切走分支且树脏 → 该仓 commit 跳过只告警；
  是否需要更强的保护（如自动 stash）待定。
-（已闭）2026-09-04 事故：commit_target 旧兜底 Path("") 落 CWD，
  测试把工具仓误提交 8 条 solve[d1]——已 reset 撤销并加护栏
  （绝对路径校验/拒工具仓/_git 拒空路径；回归 test_vcs E8-E11）。
- vcs commit 消息的 i18n/前缀规范化（当前自由文本 + Porter-Phase
  trailer）。
