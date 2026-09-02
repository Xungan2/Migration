# TODO（driver_migration_tool）

> 全局待办。按"已商定但不做在本轮"的原则记录；每条含背景与入手点。

## 1. 工作区级配置覆写通用机制

现状：仅 `routing.gates` / `routing.default` 支持工作区 `routing.json`
覆写（routing.py `load_routing`）；`checkpoints` / `policy_file` /
`panic` 阈值只认仓级 `porter/config.json`。

要做：设计统一的两级配置访问器（仓级默认 + 工作区覆写 + 特异性合并），
把 `_cp_config`、`debt_limit`、`policy_path` 等读取方全部收编。后续其他
功能（预计）也有工作区级覆写需求——做成通用机制而非逐功能补。

## 2. 工具 log 子系统重建（用户规划）——核心已完成

本轮已落地：`porter/log/` 统一框架（record 双 sink：console+events.jsonl；
v1.1 附加字段 phase/module/step/attempt/level/run_id/ref；run 登记 +
prompt 归档 + context_block 上下文接续 API；`porter log` CLI；
快照单文件 >5MB 裁剪钳制；loop/events.py 转 re-export 门面零破坏）。
规范 = `docs/log.md`（目录框架/五类格式/kind 注册表/命名/体积纪律）。
试点 print 收编：loop/run.py + env/probe.py（byte 兼容）。

**残余项（后续轮）**：
- print 全量收编：剩余 ~270 处（main.py 62 / p6.py 36 / p3.py 19 /
  strategy.py 18 / …），按文件分批，规范见 docs/log.md §8；
- P4 重试的 err_info / ut_verify.feedback_block 改走
  log.query.context_block（行为等价迁移）；
- 域事件族补 phase/module 戳（routing/gates/candidates 的
  append_event 调用点逐步加参）。

## 3. §15 失败自诊重设计

现状：`self_diagnosis.enabled=false`（整体 bypass，2026-09-02 用户决策
——该块未做明白）。bypass 的已接受代价：失败无自动分诊 → 走 attempts
（3 次）→ panic 停给人；`--defect-diagnose` / `--defect-fix` 休眠。

重设计要点：
- triage 五回路规则的账本化（gates 关口）与去 e1000/QEMU 硬编码；
- b_class / escalation 门的 legacy md 写盘收编（triage.py:510 /
  diagnose.py:210 两处，bypass 下不可达，重启用时先转账本）；
- **空日志/静默控制台回路优先重想**——bypass 后空日志会烧 attempts
  （probes.py `_note_empty_log` 已按新语义标注两种根因假设）；
- events 观测层保留不动（见第 2 条的关系）。

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
pitfalls 与工作区 platform_patches 两处漂移；§15 失败签名
（knowledge/failures.md）是事实上的第六类（域外待归位）。无家可归
的新知识会被错分或流失。

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
