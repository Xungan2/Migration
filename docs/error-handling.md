# 错误处理模块（errorloop）

> 状态：§15 重设计落地（2026-09-03 定案，直接生效）。规范以本文为准，
> 代码与本文冲突时以代码为准并回改本文。实现地图见 §8。

## 1. 定位与问题域

错误处理模块负责**失败的归责与求解**：拿到失败信息（判据 FAIL / 红项 /
defect），判定"这是谁的错"，并尝试解决或给出正确处置。它是**消费模块**
（比子系统低一级）：读 log 子系统（证据源）、读写 knowledge 子系统
（failures 域 = 签名源 + 回流目标）、经 gates 子系统停车（unsolved 关口
+ 决策债）。

错误分类（归责回路，与 failures 域条目的归责字段同口径）：

| 回路 | 错误实质 | 处置动作 |
|---|---|---|
| infra | 环境/基础设施错 | rerun（幂等）/ fix-runner |
| criteria | 量尺错（判据/测试期望/文档断言） | fix-criteria（须证据，阶段末审计） |
| migration | 迁移代码错 | fix-code（双信号复验） |
| attribution | 账挂错 | rehang（改挂真实消费者） |
| platform | 目标 OS 平台缺口且禁改 | park（泊车 + 上游登记） |

## 2. 核心流程：知识辅助的 agent 求解循环

```
失败 → 快照（先于一切，log 子系统）→ 证据包（log.query 组装）
  → ≤3 轮 agent 求解：
      检索 failures/pitfalls 知识（轮 1 全量 INDEX 注入；后续轮自主
      再检索不重注——上轮总结含"查过何条、为何不匹配"）
      → 参考知识或自行分析（知识只是参考起点，不匹配当前形态时
        果断自行分析）→ 动作词表 verdict → 编排器确定性执行
      → 双信号复验 → 签名比对（同签名连发 2 次 = 零进展早退）
  → 解决：知识回流候选（kb candidates → CP5）+ solved
  → 耗尽/早退/escalate/no-agent：六字段升级报告 + unsolved 关口
```

设计原则：

1. **判定/执行分离**：agent 只判定与改码（fix-code 在其工作目录直接
   修目标树）；一切正本写盘（runner.json / criteria.json /
   deferred.json / platform_patches.json / defects.json）由编排器
   确定性执行——agent 不动手改正本。
2. **处处有界**：3 轮硬上限 + 同签名早退 + 单轮 1200s——任何机器
   路径耗尽即降级到报告+关口，永不空转。
3. **签名防碎改动**：签名 = (subject, 规范化 detail, 规范化日志尾)
   哈希；规范化去 ANSI / 路径→basename / 时间戳→TS / 独立数字→N
   （标识符与错误码内的数字保留）——行号漂移等碎改动不翻转签名。
4. **人拿结构不拿裸现场**：耗尽终态的关口 context 带 acceptance/
   health 报告 + 升级报告（已排除假设清单在内）+ 快照。

## 3. 动作词表（verdict 契约，skills/solve.md 为准）

| 动作 | 执行方 | 效果 |
|---|---|---|
| fix-code | agent（改目标树） | 编排器只复验 |
| fix-runner | 编排器 | fix.runner_patch 合入 runner.json |
| fix-criteria | 编排器 | 改判据 expr / close_stale 闭账——**证据强制**（源码 file:line 或日志原文对照 quote≥16），立即执行 + **决策债**（`<source>.criteria-fix.<subject>`，阶段末 CP digest 批审，无人工前置闸） |
| rerun | 编排器 | 幂等重跑（复验即重跑） |
| rehang | 编排器 | deferred 改挂 fix.to |
| park | 编排器 | platform_patches 登记 + defect add+park |
| escalate | — | 立即转人工（报告 + 关口） |

防量尺作弊（criteria 撤闸后的补偿）：证据门槛前置 + auto_fixed 档案
留痕 + 决策债高亮——"改量尺让它过"无证据会被拒，有证据会被审。

## 4. 挂载点

| 挂载 | 位置 | 失败单元 | verify | 耗尽关口 |
|---|---|---|---|---|
| ① | p5（`_solve_failures`） | 模块级失败集（全体 FAIL 判据） | 重跑 `_judge_core`（build+boot+ut 全量） | `p5.unsolved.<M>`（retry，rc 3；**attempts 在此挂载点退役**——rc 3 不烧） |
| ② | p6 execute（`_execute_judge` + solve） | 红项集（FAIL + 未清偿 deferred） | 重跑判定核心（纯判定无副作用） | `p6.unsolved`（retry，rc 3） |
| ③ | d1（`p6 --defect-diagnose <ID>`，人手按需） | 单个 defect | build+boot 双信号 | `d1.unsolved.<did>`（retry，rc 3） |

- solved → 挂载点继续正常收尾（p5 rc 0 / p6 all-green / d1 四字段
  闭账 + `p6.defect.fix.<did>` CP4 债）；
- parked / rehung → 也停 unsolved 关口（p5/p6）或直接返回（d1）——
  登记已完成，停人定夺；
- **熔断（self_diagnosis.enabled=false）**：挂载点退旧人工路径
  （p5 rc 1 → attempts→panic；d1 rc 2）。`PORTER_SELF_DIAGNOSIS=1`
  强制开（测试惯例）；`PORTER_NO_AGENT=1` = 降级档（只出报告+关口，
  零 agent 调用）。
- `--defect-fix` 已并入 `--defect-diagnose`（求解循环含修复/验证/
  闭账），入口保留为重定向垫片。

## 5. 知识面（failures 域）

- taxonomy 见 docs/knowledge.md §3.2（第六域，单点改动路径）；
- **按 lineage 归属**：通用逻辑形态（静默矛盾/空日志/ANSI 失配/
  复合分解/假缺陷/平台缺口形态等）在 `knowledge/base/failures/`；
  环境特定签名（docker 锁/osdk/QEMU 特征）在
  `knowledge/<lineage>/failures/`——跨迁移不污染；
- 条目四节：签名/判别/归责/建议动作；细节指针指向 pitfalls 全文；
- 消费：轮 1 `kb_face(ws, ["failures", "pitfalls"])` 注入（INDEX 薄
  目录 + 铁律）；kb_consulted 记账（两域）；
- 回流：解决案例（hook=solve-loop）与升级报告签名候选
  （hook=escalation）→ temp/candidates → CP5 审核晋升 failures 域。

## 6. 观测面（log 子系统事件族）

| kind | 产生点 | 用途 |
|---|---|---|
| errorloop_round | 每轮起 | 轮次轨迹 |
| errorloop_end | 循环终态 | status + actions 清单 |
| escalation | 报告生成 | 升级报告索引 |
| snapshot / judge / agent_start / agent_end | 既有族 | 证据与复验流 |

## 7. 与旧 §15 的关系（迁移注记）

旧机器（triage.py 规则引擎 R1-R9 / apply_verdict / diagnose 深诊
run_diagnosis / --defect-fix 会话 / skills triage·diagnose·defect-fix /
knowledge/failures.md）已于 2026-09-03 删除；幸存改造件 =
`diagnose.generate_escalation_report`（证据组装改 log.query，签名候选
改走 kb candidates）。六案例回归基线 = tests/test_replay.py（fixtures
素材不变，断言为求解循环契约）。历史设计文档见 git history。

## 8. 实现地图与测试

| 模块 | 职责 | 关键公共面 |
|---|---|---|
| porter/loop/errorloop.py | 循环核心：签名/轮管理/动作执行/证据组装/prompt | `run_solve_loop`、`failure_signature`、MAX_ROUNDS/SAME_SIG_REPEAT |
| porter/loop/diagnose.py | 六字段升级报告（编排器生成，零 agent） | `generate_escalation_report` |
| porter/loop/p5.py | 挂载① + `p5.unsolved.<M>` 关口 | `_solve_failures`、`_unsolved_gate` |
| porter/loop/p6.py | 挂载② `_execute_judge` + 挂载③ d1 + CP4 债 | `execute`、`diagnose_defect`、`_close_fixed_defect` |
| skills/solve.md | 求解 skill（方法论/词表/证据纪律） | — |
| knowledge/{base,\<lineage\>}/failures/ | 签名知识 | INDEX 薄目录 |

测试：tests/test_errorloop.py（L1-L12：签名/动作/早退/降级/prompt
形态）、tests/test_replay.py（六案例回归基线）、tests/test_diagnose.py
（E1-E3/E7 报告面）、tests/test_mounts.py（M1/M1b/M2/M3 挂载端到端）、
tests/test_s15_bypass.py（A-D 开关/守卫/熔断/降级）。全部 mock /
PORTER_NO_AGENT，禁真调 agent。
