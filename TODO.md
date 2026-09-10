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
docs/legacy/knowledge.md §3.2）。

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

## 11. 老接口调用点分批迁移到 agent 模块新接口

现状：M11（2026-09-04，`db78fea`）交付 run_agent_seq（split_long_op：
agent 段×N+外部静态段/session 续接/总预算/同签名防打转/结果指针化）+
run_agent_structured，并接线 P4 `_step_migrate`（真实重迁 os-probe +
P5 判据级 55/55 验证）。存量 19 处 `run_agent` 调用点未迁移
（覆盖地图见 docs/legacy/agent.md §4）。

要做：分批替换——🟡 机械可换 9 处（P0 T5 烟测反馈/P1D/P1R/P2a/
P3×3/P4 fill/P5 补探/P6 draft-l4 → run_agent_structured）；
🟢 原生场景 ~~3~~ 2 处（~~P0 T3 环境提取~~——**已迁移 2026-09-05**
，但走的是 **P2b 直连形态**（`_opencode_json_runner` 专用循环 +
文件即信号）而非 run_agent_seq：T3 v2 四 session 重构定案不引入
phase 协议，故 done_key 与中止通道两缺口对 T3 不再需要；剩 探针
FAIL 回炉/errorloop 求解轮 → run_agent_seq 或同款直连形态，
errorloop 的 `_prev_context` 手拼上下文可被 session 续接直接替代）。
前置框架缺口：
- **done_key 通用 done 识别**（🟡 批次缺口，T3 已不需要）：fill 的
  `{patch_summary,…}`、P5 补探的 `{cmd,…}` 无 phase/status 键，
  `_parse_phase` 会误判不可解析——加参数"```json 块含此键即 done，
  再走 gen_schema"。
- **不可重试中止通道**（仅 fill 三重验证进 seq 时需要）：boot 日志
  不可得今天是 exit 3 停车语义，静态段需能表达"infra 中止"而非
  可重试失败（如专用异常 → outcome 映射）。
- 多操作菜单（statics 封闭集合+窄参数）已与用户设计定案、搁置待
  需求（docs/legacy/agent.md §5 定案 9）。

## 12. 设备注入的命令侧自包含（qemu_args.sh 案的最终解法方向）

背景：2026-09-05 P2b 校准发现 asterinas 树内 qemu_args.sh 不消费
EXTRA_QEMU_ARGS（接缝缺口，校准时靠手工补树侧钩子收尾——侵入目标树
不理想）。用户定案方向：**不改目标树脚本，改 runner 命令侧**——
boot.cmd 自带一个正确的启动脚本（或直接内联完整 QEMU 参数生成），
绕开 qemu_args.sh，使设备注入自包含于命令、不依赖树侧钩子。

连带设计题：inject_device 的 env 机制是否整体退役、改显式命令内注入
（`<DEVICE_ARGS>` 文本替换 / 命令内 `VAR=` 赋值前缀）；legacy/runner-discover
skill 加"消费点核实"铁律（grep 启动脚本链给 file:line——消费是静态
可验证事实：变量名须出现在消费点代码，或存在动态枚举）。

入手点：runner 契约（validate_runner / P0 skill）→ probe/p6 注入
路径 → 存量 runner 迁移。坑知识见
knowledge/asterinas/pitfalls/asterinas-qemu-args-no-inject-hook.md。

## 13. P2b 扣下待验结论（ANSI strip / 拓扑行）与解析器残留

2026-09-05 session 化改造定案：以下两项加固**故意不编码**，留作新
P2b 重跑实验（见 #14）的"自发现"观测点——

**[已验结案 2026-09-05 rerun2]** 两项自发现全部发生且 agent 自行绕过，
工具侧加固暂不回填（agent 的解法=选 post-ANSI 子串作特征，比工具侧
_strip_ansi 更根本；但 `_verify` 加 strip 仍是廉价鲁棒性，见下）：

1. ANSI 边界假 MISS：**rerun2 r2 实际触发**（lib.rs 已修好、行已打出，
   但 r2 patterns 仍为前缀风格 `e1000: xxx`，撞上日志里前缀与正文间
   的 ANSI 转义 → 0 命中）→ 白烧一整轮回炉轮；r3 自行改选消息子串
   （避开前缀）后命中。**[定案 2026-09-05：暂缓，登记待办]** 回填
   方案已备好：`_verify`（scaffold.py:215）count 特征前先过
   `_strip_ansi`（porter/env/probe.py:62 现成函数）+ 一个跨边界特征
   单测。依据 = 全工具 ANSI 洗涤矩阵盘点（2026-09-05）：boot 双信号
   判定（probe.py:133-137）/ 单测 smoke（ut_verify）/ P5 判据注入 /
   P6 / 失败签名（_static_sig、errorloop）**全部已洗**，唯 scaffold
   `_verify` 特征 count 原样——P2 重构新写的判定点没跟上既有惯例，
   属补齐而非新发明；无假阳性风险（strip 只删转义码）。
2. 注入接缝自发现：**rerun2 r3 完整复现预期发现链**（无 skill 提示、
   空 KB）——读 verify 证据 → 追启动命令链 make run_kernel→OSDK.toml
   →tools/qemu_args.sh → grep EXTRA_QEMU_ARGS 消费点确认无（给了
   OSDK.toml:12 / qemu_args.sh:226-227 证据）→ 树侧挂 e1000（net02
   后端）。拓扑说明行**不需要**——agent 自己走到了。

另记：extract_json 嵌套围栏 bug（非贪婪围栏正则被 JSON 字符串内嵌
``` 截短；r1_R1 实录 3930/12908 字符）仍存在于 ~20 处非 P2b 调用点
（P3/P4/P5/P6/env/divide/routing/review/mapping）；P2b 已改文件
输出免疫。修法已验证：```json 围栏后从 `{` 起做字符串感知花括号
配平扫描（处理转义与字符串内花括号/反引号）。

## 14. 新 P2b 重跑校准实验（session 化验证）——已执行 PASS（2026-09-05）

上一轮校准是 session 化改造**之前**的形态，需对重写后的 P2b 重跑：
fresh worktree 自基线 `36ae7fe10`（/tmp/opencode/cal/* 保留勿动），
拷 cal/ws 的 project.json/runner.json 改路径，无人工干预跑
`porter p2-scaffold`。判据：① 单 session 贯穿全部轮次（各段日志
sessionID 一致）；② 三信号全绿；③ 自发现观测（见 #13 两项）。
注意：DEFAULT_DEVICE_IDS 已退役（2026-09-05 去硬编码），重跑须显式
`--device-ids 0x8086:0x100e`（QEMU -device e1000 = 82540EM）。

**结果（/tmp/opencode/rerun2/ 保留备查，run2.log 为权威记录）**：
裸条件 = skill 删"设备注入扩展点"节（跑完已恢复）+ 空 KB（project.json
无 kb_dir）+ 无拓扑提示。rc=0，3 轮闭环：
- ① 单 session 贯穿 ✓（470 个事件同一 sessionID，r1/r2/r3 全续接）
- ② 三信号全绿 ✓（r3：build/boot/patterns×3 全 PASS，attempts=3）
- ③ 自发现 ✓✓✓（超出预期）：
  r2 自诊 Bootstrap 阶段日志早于 logger 就绪 → 改
  `#[init_component(kthread)]`（还核验了 PCI 设备排队重探语义）；
  r2 MISS 根因= ANSI 边界（见 #13.1）；r3 自发现注入接缝（见 #13.2）
  + patterns 改消息子串 → 收敛。
- 中途工具 bug 一枚（与 P2b 设计无关）：`---` 开头的续接消息被
  opencode CLI 当选项 → rc=1/0s 全灭 → 静态 panic 正确兜住；已修
  （`--` 分隔符，commit 97f78ee，live 验证）。首跑因此中断重跑。
- 轮时长：r1 发现 519s / r2 修订 282s / r3 修订 192s（session 续接
  的增量消息显著短于全量重发，符合设计预期）。

## 18. opencode session_id 跨进程持久化（T3 人工环升级 + P2b panic 恢复共用）

现状：session 只在进程内存里活着。两个消费方受限——
- T3 v2 人工环（extract.py）：耗尽 → exit 3 后进程结束，重跑时只能
  **新 session + 总结文件作记忆载体**（agent 总结外化了会话记忆，
  当前已可用）；持久化落地后可升级为 --session 续接原会话，省去
  记忆重建；
- P2b scaffold（scaffold.py）：session 解析不到 = RuntimeError 静态
  panic 终局；持久化 + t3_state 式状态文件可支持跨进程断点续跑。

要做：编排器把 session_id 随轮次状态落盘（T3 已有 t3_state.json 先例，
含 session_id 字段）；重跑入口检测在册 session 先试续接（续不动再
新开）。注意 opencode 会话存储随版本演进（~/.opencode/ 下按 id 索引），
升级需复验续接语义。

## 15. opencode stdin 消息通道的版本敏感跟踪

2026-09-05 定案：两个 runner（run_agent / _opencode_json_runner）的
消息一律经 stdin 传 opencode（argv 无消息元素）。依据 =
opencode run.ts `resolveRunInput` 的 `return piped` 分支（无位置参数
→ stdin 全文 = 消息，verbatim）。**该行为未写入官方文档**，属版本
敏感依赖——opencode 每次升级后应跑三检复验：① 纯 stdin 调用出正常
JSONL 事件流；② stdin + `--session` 续接记忆在；③ 消息 verbatim
（让 agent 复述首行，无引号包裹）。失败则回退 argv+`--` 方案
（97f78ee 保留在 git 历史可考）。

历史包袱备注：argv 路径除 `-` 开头被当选项（97f78ee 已修后仍存在的
引号包裹问题）外，还会给含空格消息包字面 `"` 并转义内部引号——
stdin 化后一并消除。

**加条（2026-09-05 用户定案）：工具绑定 opencode 版本**。stdin 三检
实证版本 = **1.18.28**（本机 `~/.opencode/bin/opencode`，`opencode
--version` 可查）。**[2026-09-05 复验]** 自更新已发生（本机现为
**1.18.29**）；三检迷你版复验**通过**（stdin 事件流含 sessionID /
--session 续接记忆携带（暗号回溯）/ verbatim）——通道未破，但恰是
"静默升级没人知道"风险的实录；OPENCODE_DISABLE_AUTOUPDATE 注入与
版本核对仍未实现，升级流程（装新版 → 三检 → 改钉住串）待落地时
将实证版本更新为 1.18.29。设计方向：① runner 启动时核对 `opencode
--version` 与仓内钉住的版本串，不符 → 警告（或按需硬失败），提示跑
三检；② runner env 注入 `OPENCODE_DISABLE_AUTOUPDATE=1`（官方文档化
变量），防 opencode 自更新静默改变 stdin 行为——版本敏感依赖的最大
风险就是"某天悄悄升级了没人知道"；③ 钉住的版本串随三检复验通过而
更新（升级流程 = 装新版 → 三检 → 改钉住串 → 提交）。commit id 钉法仅在
自编译场景可用，发行版以版本串为准。

## 16. 日志源接入的清理前置原则（ANSI 假 MISS 的泛化，2026-09-05 定案）

**原则（用户定案）**：每种日志接入工具消费时，必须**提前确定并登记
其清理/规范化方式**——而不是等某个判定点踩坑后逐个补。#13.1 的
ANSI 假 MISS 只是"清理未前置"问题类的**一个实例**，不是问题本身。

**现状盘点（日志源 × 脏数据 × 清理现状）**：

| 日志源 | 脏数据 | 清理现状 |
|---|---|---|
| 串口/boot（qemu.log） | ANSI 颜色码 + 屏幕控制序列（`\x1b[2J` 清屏/光标定位，rerun2 首行实证） | `_strip_ansi` 消费者各自 opt-in（10+ 处）；scaffold `_verify` 漏网（#13.1） |
| build 输出 | 潜在 ANSI（当前实测 0——是"管道非 TTY → cargo 不上色"的**偶然**，runner 命令未设 NO_COLOR，非契约） | 无显式清理 |
| agent 事件流 | NO_COLOR=1 已设但 opencode 不完全遵守（cal 格式化输出见 `[0m` 残留） | `_parse_events` 按 JSON 行解析，天然免疫 |
| 静态段/单测输出 | ANSI | 判定前 `_strip_ansi`（ut_verify 等） |
| 失败签名输入 | ANSI + 时间戳 + 路径 + 独立数字 | 语义级归一（`_static_sig`/errorloop）——比清洗深一层的既有先例 |

**设计方向**：
1. 把清理从"消费者 opt-in"改为"**日志源接入边界统一保证**"：
   `_recover_boot_log`/`boot_and_log` 等获取层返回规范化文本，或
   raw+clean 双视图（归档存 raw 保现场，判定用 clean）——新增判定点
   默认拿到干净日志，P2 重构式漏网（#13.1）结构性不再可能。
2. 新日志源接入清单加一栏"清理规格"：什么脏数据、什么函数、在
   哪个边界应用。
3. 分层原则：源边界只做**无损清洗**（删不可见控制序列）；语义级
   归一（时间戳/路径替换）仍属特定消费者（签名等），不得下推到源。
4. 顺手项：runner 命令 env 注入 `NO_COLOR=1`，把 build 日志的干净
   从偶然变契约。
5. 与 #13.1 的关系：#13.1 是单点回填；实施本条时在源边界
   （boot_and_log 层）做即可自然覆盖 #13.1——二择一，勿重复改。

## 17. runner 双产物与三级使用模型（2026-09-05 定案；T3 重构的契约基线）

**定案（用户拍板，T3 agent 循环重构按此对外契约实施）**：

- T3 产双产物：`runner.json` = 冻结的执行契约（通用形态 cmd + 判定
  特征 + 超时/日志控制 + 注入机制）；`runner.md` = 冻结的语境档案
  （选择依据/备选与被毙原因/变形方法/坑史/不确定项——含 net-user
  SLIRP、dm cmdline 式注入、unit_test 作用域收窄、增量构建档实测等）。
- **子集不变式**：runner.json ⊆ runner.md。实现 = agent 写单文件
  runner.md（尾部嵌完整 runner JSON 块）→ 机器分离落盘（先例：
  P1-strategy 双产物协议，divide/strategy.py）；子集按构造成立，
  可机器校验（runner.json ≡ md 尾块，手改漂移即可检出）。
- **后续使用三级**：① runner.json 通用形态可用且够 → 静态直用
  （现状消费方零改动）；② 不满足/需变形 → agent 参考 runner.md
  推导变体（用完即弃/记相位报告，**不回写**）；③ runner.md 也不够
  → kb 专用 **runner** 域沉淀解法（跨工作区），下次 T3 重跑注入为
  起点假设（现 T3 R1 注入 runbook 域的接口正好复用）。
- **冻结**：T3 探测循环内可变，`_finish` 后双文件定死；知识只准走
  ②/③ 正门，禁止侧门改写。_finish 时对双文件记指纹（project.json），
  后续相位消费前校验，改动即关口报错。

**消费侧分批改造清单（本条主体——接不上的先记后做，不阻塞 T3 重构轮）**：

| 存量行为 | 证据 | 违反点 | 改法方向 |
|---|---|---|---|
| errorloop fix-runner 改写 runner | `errorloop.py:269 _patch_runner` | 冻结 | 改登记 kb runner 候选 + 关口停车 → 人决定重跑 T3（重跑吃第③级知识）；或显式 override 层——方向待定案 |
| P5 unit_test 回填写 runner | `p5.py:104 _save_runner_ut`（verified/discovered_by/notes 即侧门字段） | 冻结 | T3 契约收紧为 unit_test 必填（至少 `mechanism:"none"`，不许静默缺课）；驱动级首验改第②级（按 runner.md 收窄方法派生），verified 记 P5 相位报告 |
| P6 SLIRP 参数硬编码 | `p6.py:51 DEFAULT_EXEC_DEVICE_ARGS`（e1000 专属串） | 第②级 | T3 产 `example_args["net-user"]`（`p6.py:458` 已在读，键产出即退役硬编码，消费侧零代码改动）；新形态派生读 runner.md |
| runbook 域与 runner 域关系 | draft_runbook 从 runner.json 反推（runbook.py） | 第③级 | 注册 kb "runner" 域（TODO #5 保证三处单点改动）；runbook 域建议被取代（待定案）；draft_runbook 改以 runner.md 为底稿 |
| scope_hint/notes 双源 | 唯一消费者 runbook.py:45,75（渲染） | 子集纪律 | 迁入 runner.md，runner.json 去知识字段 |
| confidence_notes 丢弃 | extract.py `_finish` 只落盘 envelope 的 runner 键，全工具零消费者 | 知识流失 | 进 runner.md 不确定项节（人工问题原料，答后回写） |
| meta.reviewed 可变位 | CP0 人审翻 runner 布尔 | 冻结 | 审阅记录改存关口账本 |
| timeout_inc_sec 虚设 | 唯一 build 超时消费 probe.py:83 恒用 full 档 | 契约虚设 | runner.md 记实测增量耗时；未来消费者（如 P4 快冒烟）启用时按 md 依据 |

与 #12 的关系：runner.md 的 boot/inject 节承载"消费点核实"证据
（变量被启动脚本链哪里消费，file:line）；#12 的命令侧自包含
（boot.cmd 内联完整参数）仍是独立方向。

**产出侧已落地（2026-09-05，T3 v2 四 session 重构）**：extract.py
重写为 build→boot→inject→unit_test 四 session 流水线（P2b 直连形态，
文件即信号），双产物 runner.json+runner.md（子集按节校验）+ 冻结
指纹（project.json["t3_frozen"]）；三级输入之① `--hints-dir`（intent
同款暂存）；耗尽→agent 总结→p0.t3.<cap> 关口→人答→新 session 种子
（总结+答案）+ 小额资源续跑；skill legacy/runner-discover.md 重写（零真实
OS 实例，中立铁律）。**上表消费侧清单不受影响，照旧分批。**

**入手点**：消费侧清单逐项分批。

## 19. T3 v2 ↔ handoff 框架接线（2026-09-06 f6eb2db 合流遗留）

**背景**：f6eb2db（durable handoffs）把 v1 T3 的 R1-R4 轮包成
`p0.environment.agent` handoff task（`_agent_attempt`）+ `p0.environment`
聚合 task（`extract_env` wrapper）。同期的 T3 v2（四 session 直连流水线，
定案"文件即信号、不走 phase 协议"，见 #11/#17）退役了全部 v1 路径；
rebase 合流（T3 v2 提交重放于 f6eb2db 之上）时 v1 包装层随冲突解整体
让位，v2 暂不发布任何 handoff 记录。

**现状**：
- 传统 CLI（`porter p0`）路径 v2 裸跑，上游各包装为条件式（execution
  在场才生效）空转，无碍；`porter/common/agent.py` 的
  `prepare_agent_prompt`/`record_provider` 对 v2 直连 session 同理空转。
- handoff-aware CLI 下 P0 execution 内 v2 不产 child task 记录——P0 阶段
  的 handoff 依赖链/断点复用对 T3 失明。
- `test_handoff_pipeline.py::test_environment_probe_failure_becomes_input_to_fresh_agent_session`
  已 skip（驱动的是 v1 轮次协议；skip 理由即指向本条）。

**要做**（接线定案后）：v2 四 session 发布 per-cap task（如
`p0.environment.<cap>`，probe 终验 success/failure 各自落 handoff）；
`extract_env` 在 execution 内注册 `p0.environment` 聚合 task；恢复并把
上述测试重写到 v2 形态；解除 agent.md「接线范围」P0 句的"暂不发布"标注。

## 20. inject 机制缺第三态（软件设备/设备常在）——dm-zero × asterinas T3 实验边界发现（2026-09-06）

**背景**（migrations/dmzero-t3-e2e-20260906/ws-dmzero，tier① hints 首验）：
dm-zero 是 device-mapper 软件 target，无 QEMU 硬件形态；"注入"语义 =
建表参数经内核 cmdline 送达。现行 inject_device 契约只有 env/cmd 两机制、
判定器只认"驱动特征差分 或 -device/model= 设备名 token"（extract.py
`_injection_evidence_check`）——软件设备两个都物理不存在：
- 树内无 dm 框架 → 无驱动 probe 行可锚（检索证据见 ws runner.md）；
- 无 `-device` 型号 → 无设备名 token；
- 内核不回显 cmdline（双轮归一化 0 差异实证）。

**本次的替代判据（已冻结进 ws runner，供后续同类场景先例）**：两段式
回执——`console=ttyS0` 注册参数消费锚（`Registered NS16550A as a console`
注入轮 1 命中/裸轮 0，ttyS0 拓扑下该行才进 qemu.log）+ 载荷
`dm-mod.create="<DEVICE_ARGS>"` 同通路送达（grub.cfg 宿主侧逐字可查，
人工复核辅助）。**载荷回显被证明结构性不可能**（GRUB `;`/`&&` 元字符
截断 multiboot2 行、内核 split_arg 引号保留 + 含点 token 静默丢弃、
busybox 词法三重绑定——法医矩阵见 ws 的 inject.md 坑史与 answers.md）。

**要做**：inject_device.mechanism 增第三态（如 `cmdline`/软件设备语义），
把"注册参数消费锚 + 宿主侧载荷证据"标准化为合法判定路径，替代逐工作区
人工裁决；树侧 cmdline 回显（dispatch init 一行 info!）作为上游提案另记。

## 21. 探测执行器超时不杀进程组 → 孤儿 QEMU 抓锁毒害后续轮（2026-09-06 实录）

**症状**：T3 inject 终验注入轮 600s 超时（rc=-1）后，`subprocess.run`
只杀了直接子进程 bash，容器内 cargo-osdk+QEMU 幸存，抓着
test/initramfs/build/ext2.img 写锁（`Failed to get "write" lock`）+
hostfwd 端口；同 ws 随后所有 boot 探测两连败（rc=2、qemu.log 缺失），
貌似回归实为环境毒害；`docker run --rm` 容器因内部 bash 未退而滞留
（docker ps 可见，宿主 kill EPERM 须 docker kill）。

**修法方向**：probe._run 用 `subprocess.Popen(start_new_session=True)` +
`os.killpg`（或 `process_group=` 参数）在 TimeoutExpired 时杀整个进程组；
或文档化"超时后检查 `docker ps` 清残留容器"为运行手册条目。

## 22. handoff 层相对路径 materials 解析按 workspace 拼接（2026-09-06 实录）

**症状**：`porter p0 --materials examples/asterinas-materials/x.md`（相对
路径，cwd=工具仓根）在 handoff 预检报 `not ready — required material is
missing: <ws>/examples/...`（handoff/core.py `_material_ref`：非绝对路径
一律 `self.workspace / path`）。f6eb2db 合流后首次暴露；改绝对路径即绕过。

**修法方向**：CLI 入口统一 `Path(...).resolve()` 后再交 handoff（main.py
materials/intent_file/hints_dir 全套），或 `_material_ref` 对相对路径先按
进程 cwd 解析、不存在再按 workspace。
