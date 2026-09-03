# driver_migration_tool 工具报告

> 本文件是工具的**完整开发报告**，面向 0 上下文的接手 agent：读完本文 +
> README.md + 代码 + docs/ 五份子系统/模块文档，即可理解工具全貌并继续开发。
> 生成：2026-09-03 全量（HEAD `189ff11`）；2026-09-04 增补（M10 git 管理
> 模块 + 知识库挪家 + fill 落点约束 + docs 分层 sub-systems/modules）。
> 方法：4 路并行全读 porter/ 源码（47 个 .py 文件，14908 行）+ 17 skills
> + 4 份 docs + TODO.md + 44 条提交史 + 旧审计报告交叉核对 + 131 测试绿。
> 增补轮核对：porter/ 54 个 .py 文件 15876 行；docs/ 5 份（sub-systems/×3 +
> modules/×2，新增 vcs.md）；164 测试绿 + 真实 git/agent 冒烟。

---

## 0. 阅读指引

- **想用工具** → 读 README.md（用法/依赖/样例）。
- **想理解为什么这么设计** → 读本文第 2 节（设计原则）+ 第 4 节（历史）。
- **想定位某段代码** → 读本文第 5 节（文件级现状），按子包找。
- **想接手某个子系统/模块** → 读 docs/sub-systems/<名>.md 或
  docs/modules/<名>.md（规范）+ 本文第 5 节（实现）。
- **想接手 git 管理模块（vcs）** → 读 docs/modules/vcs.md + 本文 §4 M10 / §5.2。
- **想知道还有什么没做** → 读本文第 6 节 + TODO.md。
- **本文与 docs/ 的分工**：docs/（sub-systems/ 与 modules/ 两层）是各
  子系统/模块的**规范**（协议/schema/限制），
  本文是工具的**全貌**（历史/架构/文件级实现地图/跨子系统关系）。

---

## 1. 工具是什么、最终目的

**一句话**：把 Linux 内核驱动（C）迁移为任意目标 OS 原生驱动（现状：
安全 Rust 组件）的**自动化流水线工具**。

**执行模型**（三要素）：
1. **确定性 Python 编排**（`porter/`，14908 行，零第三方依赖）——流水线
   推进、机器复核、文件写盘、状态机管理；
2. **判断性 opencode agent 调用**（17 个 skill 文件）——类别识别、源码
   分析、映射判断、代码翻译、失败求解等"需要判断"的任务；
3. **机器复核**（双信号判定：退出码 + 日志特征）——agent 一切输出经
   机器校验，不信任未验证的 agent 主张。

**关键设计**：工具自身**零目标 OS 硬编码于数据层**——所有构建/启动/测试
命令由 P0 阶段 agent 探明后写进工作区 `runner.json`，后续相位数据驱动消费。
这使得 P0/P1 层可复用于任意目标 OS/驱动；P2b 骨架起为 Asterinas 专属
（详见第 7 节通用性）。

**最终目的**：开发者提供 Linux 驱动源码 + 目标 OS 树 + 自由资料，
工具自动完成：环境探明 → 拆分策略 → API 映射 → 骨架生成 → 逐模块迁移
→ 模块验收 → 系统验收 → 终态报告，人工只在特定关口介入（审核/决策）。

**首个实验**：e1000→Asterinas，18 轮全流程已完成，验证了方法论可行性。
工具是该方法论的通用化实现。

---

## 2. 核心设计原则（8 条，理解工具设计哲学的钥匙）

1. **定界=模块划分，放 P1**：对 Linux 源码按功能划分模块，模块逐步迁移、
   后继模块只依赖已迁模块+胶水；MVP=模块前缀+模块内范围，P1 末人工一次审定。
2. **测试二分法**：模块验收（依赖闭包=自身+已迁+胶水+既有设施；累积回归）
   vs 系统验收（按用户可见能力组织；含 deferred 判据清偿）。测试内容设计
   just-in-time：P1 草稿→P4 落地→P5 验收→P6 收口。
3. **类别=标签集合，失败友好**：仅选模板（加速器非承重墙）；`--category`
   人工覆盖；不可判定回落通用模板并警告；仅"非内核驱动"硬停。
4. **自由资料驱动 OS 适配**：开发者提供自由资料（文档/笔记/CI 配置，形态
   不限，可省略）→ agent 自行阅读资料+目标 OS 源码树，提取出最小执行契约
   `runner.json`（cmd/超时/成功失败特征/日志位置/设备注入），与真实探测
   交织修正（≤3 轮）。工具无 OS 专属代码。
5. **SKILL vs 知识库**：SKILL=行为指令（每轮全量注入，漏执行会出事故的
   放这）；知识库=事实资料（条目化、带 scope 元数据与命中计数；三层注入）。
6. **知识沉淀配置化**：总配置决定新知识入库策略；默认硬性人工审核。
7. **agent 固定 opencode 非交互**；确定性动作用脚本（探测执行/门禁/脚手架）。
8. **零预设产物**：不预灌历史经验、不预生成 OS profile/知识库骨架/记忆
   文档/state 等任何"待填充"结构——各产物的形态等其真实消费者出现。

---

## 3. 架构总览

### 3.1 包结构

```
porter/
├── main.py              # CLI 总入口（18 子命令分发 + 路由校验）
├── config.json          # 仓级运行时配置（routing/checkpoints/kb/panic/self_diagnosis/vcs）
├── common/              # 跨阶段共用
│   ├── agent.py         #   opencode 非交互调用抽象（PORTER_MODEL 可配；32K 输出帽修复；
│   │                    #   首尾挂 vcs agent_pre/post 隔离点）
│   ├── symbol.py        #   C 源码符号静态扫描（P1 依赖图/P2a spine 共用；v2.2 语句分类版）
│   └── vcs.py           #   git 管理模块（两 repo commit/分支/baseline/bundle/台账）
├── env/                 # P0 专属
│   ├── inputs.py        #   T1 输入解析（脚本）
│   ├── category.py      #   T2 类别识别（agent）
│   ├── extract.py       #   T3 环境信息提取（agent 多轮×探测交织×人工升级）
│   ├── probe.py         #   探测执行（build/boot/boot_with_device，双信号）
│   └── gate.py          #   T5 门禁（脚本，机器可检）
├── divide/              # P1 专属
│   ├── strategy.py      #   拆分策略（agent 读 Linux 源码→strategy.md）
│   ├── run.py           #   模块划分编排（索引预建+按文件分配+机械展开）
│   ├── index.py         #   定义索引+归属强制+机械展开（纯脚本）
│   ├── fragments.py     #   物理抽取代码片段（纯脚本）
│   ├── resolve.py       #   依赖解环（agent 搬运循环+守恒校验）
│   └── deps.py          #   依赖图计算/环检测/粒度护栏（纯脚本；resolve 的无 agent 版）
├── bootstrap/           # P2 专属 + 知识子系统
│   ├── mapping.py       #   P2a 引导映射（agent 分批+机器校验+增量合并）
│   ├── extract_spine.py #   P2a 主轴 API 提取（纯脚本）
│   ├── skeleton.py      #   P2b 全局骨架生成（目标 OS 专属模板：Asterinas 起步）
│   ├── pregen.py        #   P2c 探针预生成（风险主张前置验证）
│   ├── run.py           #   P2 入口编排（2a→2b→2c→验收）
│   ├── kb.py            #   知识库骨架：目录模型+域注册表+薄 INDEX+通用晋升
│   ├── knowledge.py     #   maps 域收成/晋升
│   ├── gaps.py          #   gaps 域收成/检索
│   ├── runbook.py       #   runbook 域收成
│   ├── candidates.py    #   随机知识探查（四类钩子+去重闸）
│   └── review.py        #   知识审核面（CP5 材料+分类+晋升/拒绝）
├── loop/                # P3-P7 + gates + errorloop
│   ├── run.py           #   垂直循环推进（P3→P4→P5 ×N，拓扑序，断点重入）
│   ├── state.py         #   loop_state.json 状态机
│   ├── surface.py       #   P3(M) 使用面提取（外部符号四分类）
│   ├── criteria.py      #   判据 schema 校验 + L0-L4 复核执行器
│   ├── p3.py            #   P3(M) 增量映射+gap 分类+判据草案+探针
│   ├── p4.py            #   P4(M) fill 统一+切片迁移+轮末冒烟
│   ├── p5.py            #   P5(M) 模块级验收 L1/L2/L0/L3+deferred+求解挂载①
│   ├── p6.py            #   P6 系统验收（聚合/execute/finalize-l4/defects+求解挂载②③）
│   ├── p7.py            #   P7 终态报告+上游补丁台账
│   ├── probes.py        #   高风险探针机器（生成/同步/判定/共享生命周期）
│   ├── ut_verify.py     #   unit_test 配置机器烟测（"agent 说"→"机器验"）
│   ├── gates.py         #   人工关口账本（两车道+一账本+panic 统一入口+应用器注册表）
│   ├── routing.py       #   三级分流路由（rules/agent/human）
│   ├── errorloop.py     #   错误处理模块核心：知识辅助 agent 求解循环
│   ├── diagnose.py      #   升级报告生成（六字段 schema，编排器生成零 agent）
│   └── events.py        #   观测地基兼容门面（re-export porter.log，旧调用点零改动）
└── log/                 # log 子系统（统一观测框架，纯静态零 agent）
    ├── core.py          #   record() 唯一写入口（双 sink + 上下文戳 + 派生助手）
    ├── store.py         #   events.jsonl 读写 + 进程级 bind
    ├── console.py       #   console sink（[porter] 行渲染 + 级别阈值）
    ├── query.py         #   查询/run 登记/上下文接续/timeline
    └── snapshot.py      #   失败即快照（不可变证据束）
```

### 3.2 相位流水线

```
P0  环境门禁    T1 输入解析→T2 类别识别(agent)→T3 环境提取(agent×3轮×探测)→T5 门禁
P1  拆分策略    strategy.md(人工审)→模块划分→依赖解环→deps.json 拓扑序
P2  引导映射    2a 主轴API映射→2b 全局骨架(写目标树)→2c 探针预生成→验收
P3  分析(M)     使用面→增量映射→gap分类→判据草案→探针补新
P4  生产(M)     fill统一→切片迁移(≤900行/片)→轮末冒烟
P5  验收(M)     L1/L2/L0/L3+累积回归+deferred+求解挂载①
P6  系统验收    聚合健康 / --execute 重测 / --finalize-l4 定稿门 / defects 账本 / 求解挂载②③
P7  终态报告    聚合+git baseline diff+crate统计+补丁台账+知识沉淀提醒
```

退出码约定（全工具一致）：0 成功 / 1 失败 / 2 前置缺失 / 3 需人工（= 存在
open 阻塞关口）。全程幂等断点重入：产物存在即跳过，失败可重跑同命令从断点继续。

### 3.3 数据流（关键文件谁写谁读）

| 文件 | 写者 | 读者 | 语义 |
|---|---|---|---|
| `project.json` | P0 T1(+vcs hook) | 全相位 | 项目身份真值源（驱动/目标OS/类别/VCS基线/kb_dir；M10 增 `vcs` 节=分支+各仓 baseline） |
| `runner.json` | P0 T3 | P2+/P5/P6 | 机器可执行命令（build/boot/unit_test/inject_device） |
| `P1/strategy.md` | P1S agent | P1D/P1R/CP1 | 拆分策略（人工审阅；CP1 指纹绑定） |
| `P1/modules/deps.json` | P1R | loop | 模块拓扑序（循环输入） |
| `P2/mapping.json` | P2a+P3(M) | P3/P4/探针 | API 映射真值源（9字段+domain，evidence 机器校验） |
| `loop_state.json` | loop run.py | loop | 循环状态机（order+每模块 phase/attempts） |
| `deferred.json` | P5/P6 | P5/P6 | deferred 判据登记（消费者落地当轮清偿） |
| `gates.json` | gates.panic | 全相位 | 人工关口账本（唯一事实源；human_questions.md 是渲染产物） |
| `events.jsonl` | log.record | log.query/errorloop | 事件流（append-only，真值源） |
| `vcs_commits.json` | vcs commit 成功 | P7 commit_chain | commit 台账（repo/phase/hash/msg/time；git-log 可重建，不自提交） |
| `failure-snapshot-<n>/` | log.snapshot | 升级报告/gates | 失败现场不可变证据束 |
| `defects.json` | P6 CLI | P6/P7/CP4 | 缺陷账本（四字段强制） |
| `platform_patches.json` | P3/P4 | P7 | fill/register-fill 登记（上游补丁素材） |
| `criteria.json` | P3 agent | P5/P6 | 模块判据草案（L0-L4） |
| `acceptance.json` | P5 | P6/P7 | 模块级验收结果 |
| `l4_criteria.json` | P6 draft_l4 | P6 execute | L4 系统判据（定稿后 CP3 审） |

---

## 4. 开发历史（9 个里程碑）

### M1 P0 环境门禁（`7f7206c`）

**做了什么**：实现 P0 全流程——T1 输入解析（脚本校验驱动/目标树/资料）、
T2 类别识别（agent 小任务，识别 pci/net/... 标签）、T3 环境提取（agent
多轮×真实探测交织，产出 runner.json）、T5 门禁（三项双信号全 PASS 才过）。

**为什么**：工具零目标 OS 硬编码的前提是"OS 差异数据化"——agent 从开发者
资料+目标树提取出机器可执行的 runner.json，后续全数据驱动。探测为金标准
（agent 声明须经真实运行复核）。

**当前形态**：T3 ≤3 轮自动（R1-R3），未成则 exit 3 人工（answers.md → R4
答案整合轮）。unit_test 烟测双道（P0 门禁第一道 + P5 补探第二道）。runbook
知识在 T5 过后自动收成入 temp。

### M2 P1 拆分流水线（`b1002ff`）

**做了什么**：P1-strategy（agent 读 Linux 源码产出自由 Markdown 策略分析，
人工审阅）、P1-divide（索引预建→按文件 agent 分配→机械展开→物理抽取）、
P1-resolve（符号扫描→依赖图→环检测→agent 搬运循环→拓扑序 deps.json）。
样例库三分区（base/知识库目录/temp）+ p1-promote 晋升。

**为什么**：单次大 agent 调用撞思考上限零产出（教训）→ 改为按文件小调用。
策略是咨询性产物（零 schema 契约），质量靠人工审阅 + divide 客观校验
（覆盖 diff / 依赖无环 / 粒度护栏）双把关。解环用守恒校验（片段行集不变量）
机器证明。

**当前形态**：strategy.md 存在即放行（CP1 指纹绑定审计）；p1 全流程直通
也拦 CP1（修 H5）。resolve 3 轮败 → exit 3（人工编辑 plan 重跑）。

### M3 P2 引导映射+骨架（`5a21886` + `e02721e`）

**做了什么**：P2a 引导映射（extract_spine 提取主轴外部 API → agent 分批
映射 → 机器校验 9 字段 + evidence 路径真实存在 → 增量合并 mapping.json）、
P2b 全局骨架（在目标树新建 crate + 接线点补丁，零驱动功能）、P2c 探针
预生成（风险主张前置验证，住骨架 probes.rs = 回归哨网）。maps 知识沉淀
（temp 草稿 → p2-promote 人工晋升，同名=版本更新替换）。

**为什么**：映射是后续迁移的"词典"，质量是根基 → 机器校验 evidence
file:line 真实存在。骨架是"入住仪式" → 零功能但可编译可启动，验证
OS 接线正确。探针贵且长寿命 → 前置到流程头部稳定阶段一次付清。

**当前形态**：骨架模板 Asterinas 专属（crate 布局/依赖/组件模型/init.rs
接线桩）。验收 = runner 双信号 + 组件日志特征 + 无 PROBE FAIL。CP2 映射
抽审默认关（下游机器验证兜底）。

### M4 P3-P5 垂直循环（`90dcd0c` → `d9fb59f` 重构）

**做了什么**：方案 A 相位重构——P3(M) 分析（使用面四分类→增量映射→gap
四策略处置→判据草案→探针补新）、P4(M) 生产（fill 统一→切片迁移≤900行→
轮末冒烟）、P5(M) 验收（L1 build/L2 boot/L0 ktest/L3 log_pattern+累积
回归+deferred）。loop 命令按拓扑序自动推进，断点重入，四种 exit 3 停车。

**为什么**：原 P4 一步到位太大 → 拆为生产(P4)+验收(P5)两相位，P4 专注
生产只留防毒化闸门，P5 专注判据级验收。切片≤900 行（32K token 教训）。
同签名连发 2 次 = 零进展早退（防 agent 空转）。

**当前形态**：gap human → exit 3（answers.md 表单作答）；attempts 烧穿
（3次/模块/相位）→ exit 3；deferred 无法清偿 → exit 3。泊车模块可
`--module` 绕行后续独立模块。P5 首次自动补探 unit_test 机制（reviewed:false）。

### M5 P6/P7 系统验收与终态报告（`2e4d943` + `b2e3f53`）

**做了什么**：P6 聚合模式（零重测全景健康）、execute 模式（一轮 build+
SLIRP boot+ktest → 全判据重判+deferred 清偿）、L4 判据定稿门（--draft-l4
生成器→--finalize-l4 审核门，指纹绑定）、defects 账本（四字段强制：发现/
根因/修复/回归证据）。P7 终态报告（P0→P6 全产物+git baseline diff+crate
统计+补丁台账）。

**为什么**：模块验收(P5)是依赖闭包视角，系统验收(P6)是用户可见能力视角。
L4 = 驱动内核自测（kthread 组件打 `L4 <id> PASS|FAIL` 行）。defects 四字段
强制防止"修了不知根因"。

**当前形态**：P6 为哨兵 `__P6__` 的 owner（deferred 到 P6 才可清）。
--defect-diagnose 已并入求解循环（含修复+验证+闭账）。P7 人工撰写区收口。

### M6 人工介入子系统 gates（`465c915` + `599cf7d`）

**做了什么**：全部人工介入点收敛为 `gates.json` 关口条目；两车道
（panic 异常停车 / checkpoint 计划内批审）；三级分流路由（rules→agent→human）；
答案协议 `## @<gate_id>` 表单；应用器注册表（人只表态，工具代改正本）；
决策债（自动答掉的攒批审，限额 30）；panic 统一入口（快照+账本+渲染+exit 3）。

**为什么**：把"人盯着工具"变成"工具排队等人"——工具能自己推进的就推进，
拿不准的先记下来攒着事后批量找你确认。统一账本防散落；人只表态防手改出错。

**当前形态**：23 个关口（12 panic + 9 checkpoint + 2 求解耗尽）。硬路由
必人四点（env_gate/strategy/l4.finalize/cp5.promote）。工作区 routing.json
覆写（仅 routing.gates/default 两键）。规范见 docs/sub-systems/human-intervention.md。

### M7 知识子系统 kb（`37796f5` → `08963fb`）

**做了什么**：三区模型（base 工具随附 / temp 草稿区 / 知识库目录 per-migration）；
六域注册表（maps/gaps/runbook/splits/pitfalls/failures）；薄 INDEX（file+desc+hits）；
固定知识收成（P2 末 maps+gaps、P0 末 runbook、P1 末 splits）；随机知识探查
（四类钩子→候选账→去重闸→CP5 审核→分类→晋升）；kb_face 检索注入（目录自取，
不强制内容注入）；hits 旁车（.hits.json，晋升时折叠入 INDEX）。

**为什么**：把"每次迁移从零摸索"变成"站在上次迁移的肩膀上"——经验自动收集、
人工把关、下次自取。信任分层：相位任务可见 temp+已审（草稿带标注）；自动
应答只见已审（未审不参与自动行为）。

**当前形态**：p0 `--kb` 必填显式选择（new/use）。failures 域 2026-09-03 归位
（base=通用逻辑形态 8 条 + lineage=环境特定签名 4 条）。规范见 docs/sub-systems/knowledge.md。

### M8 log 子系统（`cb6082b` → `66fa913`）

**做了什么**：`porter/log/` 统一观测框架——record() 双 sink（console 人读 +
events.jsonl 机读）；schema v1.1 只增不改（附加字段 phase/module/step/attempt/
level/run_id/ref）；run 登记 + prompt 归档 + context_block 上下文接续 API；
`porter log` CLI（tail/runs/show/timeline）；快照束（失败即抢救，>5MB 裁剪）；
print 全量收编（284 处经 codemod 统一 console_line）。

**为什么**：把"翻目录找日志"变成"查一条结构化时间线"——工具跑的每一步都进
同一份 append-only 事件流。纯静态实现（零 agent），永不抛异常（观测面不能打断
流水线）。分层在最底层（不 import 任何相位模块）。

**当前形态**：porter/loop/events.py 为兼容门面（re-export，旧调用点零改动）。
错误处理模块经 log.query 组装证据包 + 轮间上下文接续。规范见 docs/sub-systems/log.md。
真实 e2e 验证待下次迁移轮（TODO #2 残余项）。

### M9 错误处理模块 errorloop（`3fcc897` 旧 §15 → `73db803`+`450fbd2`+`53e86a6`+`bba8a38`+`189ff11` 重设计）

**旧 §15（已删）**：triage.py 规则引擎 R1-R9 + diagnose 深诊 run_diagnosis +
--defect-fix 会话 + skills triage/diagnose/defect-fix + knowledge/failures.md。

**重设计（2026-09-03 定案）**：知识辅助的 agent 求解循环——
- 失败 → 快照 → 证据包（log.query 组装）→ ≤3 轮 agent 求解（轮 1 全量
  failures+pitfalls INDEX 注入；同签名连发 2 次 = 零进展早退）→ 动作词表
  verdict → 编排器确定性执行 → 双信号复验 → solved 或耗尽；
- 七动作词表（fix-code/fix-runner/fix-criteria/rerun/rehang/park/escalate），
  判定/执行分离（agent 只判定与改码，正本写盘归编排器）；
- 三挂载（p5 挂载①/p6 execute 挂载②/d1 挂载③）+ unsolved 关口（attempts
  在挂载点退役）；
- criteria 修正撤人工闸改决策债审计（证据门槛前置 + 阶段末 CP 审计）；
- 旧机器删除，六案例 fixture 迁移为 test_replay 新契约；diagnose 瘦身为
  报告面（generate_escalation_report 六字段 schema，零 agent）。

**为什么**：旧 §15 规则引擎硬编码 e1000/QEMU 事实，换驱动需扩规则；深诊
会话越出运行模型。重设计用 agent 求解 + 知识库签名检索替代规则引擎，
用 ≤3 轮有界循环替代会话，用 unsolved 关口替代审核门。

**当前形态**：config `self_diagnosis.enabled=true` 缺省开（直接生效）；
熔断关 = 回退旧人工路径；PORTER_NO_AGENT=1 = 降级档（只出报告+关口）。
规范见 docs/modules/error-handling.md。首个真实迁移轮的实测校准待做（TODO #3 残余项）。

### M10 git 管理模块 vcs（2026-09-04）

**做了什么**：`porter/common/vcs.py`——两 repo 全程 commit 管理：
目标 OS 仓（P0 扫并行 git 仓记 baseline + 建 porter 分支
`--os-branch`/自动生成；P2 骨架 / P4 每模块末 / P6 execute / 求解
fix-code 修码后提交）+ 工作区仓（P0 `git init` 同分支名 + 写
.gitignore；阶段末 / loop 模块 done / **agent 调用前后**（pre-agent/
agent 成对隔离，diff 即该次调用 ws 产物）/ exit 3 停车 / answers
消费后提交）。台账 `vcs_commits.json` → P7 commit 链（git-log -z
重建回退）；git bundle 跨机器导出导入（`porter vcs export|import` CLI
+ P7 末自动导出）。配套三件：知识库挪入 `<ws>/knowledge/`
（3 repo→2 repo；`--kb use` 全局库种子化、promote 后 sync_to_global
回流）；fill 平台补齐落点约束到 `crate/src/external_interfaces.rs`
（骨架预置 mod）；docs 分层 sub-systems/ + modules/（新 vcs.md）。

**为什么**：此前目标树与迁移产物零 commit 管理（P0 只记 baseline，
中间全是未提交改动），事后无法回答"哪一步改了什么"；commit 即流水线
叙事，直接服务迁移结果分析与优化。跨机器可移植 = 保 hash 的
git bundle（导入端同起点 commit）。与用户逐点定案的关键约束：两仓
commit 流**互相独立**（目标 OS 不为 agent 调用加点）；分支/baseline
记工作区级 project.json（工具级 config 只放全局项）；嵌套仓只管顶层。

**当前形态**：best-effort 永不阻塞（git 失败只告警）；`vcs.enabled=
false`/`PORTER_VCS=0` 全跳过；旧工作区回落单仓兜底；identity `-c`
兜底容器无 git 身份。测试 test_vcs.py（22，mock git，含 CWD 误提交
回归）+ test_vcs_wiring.py（11，接线 patch 断言 + 真 git 隔离语义；
约定不执行真 run_agent）+ 真实 git bundle 往返冒烟（hash 保留实测）。
开发期事故（已修复+回归）：commit_target 旧兜底 Path("") 落 CWD，
测试把工具仓误提交 8 条 solve[d1]（已 reset 撤销；护栏=绝对路径+
拒工具仓+_git 拒空路径，见 docs/modules/vcs.md §3.3）。
规范见 docs/modules/vcs.md。首个真实迁移轮校准待做（TODO #10）。

---

## 5. 现状：每个文件做什么（文件级实现地图）

> 按 5 子包 + log + main 组织。每文件一段：职责 / 关键公共函数 /
> 与其他模块关系 / 已知限制。依据：源码 docstring + 4 路 explore agent 勘察。

### 5.1 porter/main.py — CLI 总入口

**职责**：argparse 18 子命令分发（+`vcs export|import`）+ 启动路由配置校验（warn-only）。
**关键公共函数**：`main(argv)` 入口；`cmd_p0`~`cmd_p7`/`cmd_loop`/`cmd_gate`/
`cmd_kb`/`cmd_log`/`cmd_vcs` 各命令处理器；`_p0_kb_decision`（纯校验，
物化延后到 T1 后 select_kb(ws=…)）/`_kb_dir_for_promote`/`_kb_sync_and_commit`
（promote 收尾：sync_to_global + ws commit）/`_p2_context`/`_loop_module`
共用校验助手。p0 新增 `--os-branch`（vcs 分支）。
**关系**：import 全部 5 子包；是唯一入口。
**限制**：无总编排命令（p0→p7 逐段手动跑——设计使然，README 应明示推荐跑法）。

### 5.2 porter/common/ — 跨阶段共用

#### agent.py
**职责**：opencode 非交互调用的最小封装。
**关键公共函数**：`load_skill(name)` 读 skill 正文；`run_agent(prompt, workdir,
log_stem, model, timeout_sec, task)` 调 `opencode run --auto`，输出落 `.log`、
输入归档 `.prompt.md`，记 agent_start/end 事件；首尾挂 vcs
`agent_pre`/`agent_post` 隔离点（ws 取 `log.store.bound()`）；
`extract_json(out)` 从混有
工具转录的输出里提取 moves JSON。
**关系**：所有 agent 调用的唯一入口；lazy import porter.log.core 记事件。
**限制**：注入 `OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX=131072` 绕 opencode
32K 静默钳制；模型默认 `zhipu-ai/glm-5.2`，`PORTER_MODEL` 可覆盖。

#### vcs.py
**职责**：git 管理模块核心（两 repo commit/分支/baseline/bundle/台账）。
**关键公共函数**：`hook_p0()` P0 登记（`register_repos` 扫并行仓+嵌套坍缩、
建/校验分支、init 工作区仓+写 .gitignore）；`commit_workspace`/
`commit_target` 语义封装（路径→归属仓映射 / status 捕获；幂等空变更
跳过；Porter-Phase trailer）；`agent_pre`/`agent_post` 调用隔离（仅 ws
仓）；`commit_chain()` 台账优先+git-log 回退；`export_all`/
`import_bundle`（bundle 保 hash）；`enabled(kind)` 配置开关。
**关系**：接线 10 处（gates.panic/process_answered_gates 两集中点、
agent.run_agent 唯一入口、阶段末/模块末挂点、errorloop fix-code 后）；
P7 消费 commit_chain 与自动导出。规范见 docs/modules/vcs.md。
**设计**：best-effort 永不阻塞；identity/gpgsign/hooksPath `-c` 注入；
台账与 exports 不自提交（:(exclude) pathspec + .gitignore 双保险）。

#### symbol.py
**职责**：C 源码符号静态扫描（v2.2 语句分类版）。为依赖图提供准确的
定义/引用数据。
**关键公共函数**：`scan_file(path) -> (defs, refs, protos)`；`scan_module_dir(mdir)
-> ({symbol:[file:line]}, ref_set)`。
**关系**：P1 resolve + P2a extract_spine + P3 surface 共用。
**限制**：无预处理器（`#if 0`/`#ifdef` 两分支都扫）；DEFINE_SPINLOCK 等
宏的"定义语义"不识别（按引用处理）。零 porter import（自包含）。

### 5.3 porter/env/ — P0 专属

#### inputs.py
**职责**：T1 输入解析——校验驱动/目标树/资料，创建工作区+project.json。
**关键公共函数**：`validate()` 三要素校验；`target_os_baseline()` 记 VCS 基线；
`init_workspace()` 建工作区+project.json（幂等：拒非空已存目录）。
**关系**：P0 第一步；project.json 是全相位身份真值源。
**注记（M10）**：`target_os_baseline()` 与 project.json["vcs"] 并存——前者
仍是 P7 baseline_diff 的输入；分支/仓登记/commit 归 vcs.hook_p0。

#### category.py
**职责**：T2 类别识别——agent 小任务，识别 pci/net/... 标签。
**关键公共函数**：`identify_category(linux_driver, workdir, override)` 返回
类别结果（`--category` 人工指定优先；不可判定回落通用模板+警告；仅"非
内核驱动"硬停 exit 3）；`write_result()` 写回 project.json。
**关系**：P0 第二步；类别=选模板开关（加速器非承重墙）。

#### extract.py
**职责**：T3 环境信息提取——agent 多轮×真实探测交织×人工升级，产出 runner.json。
**关键公共函数**：`validate_runner(r)` 字段级最小契约校验（build/boot/
inject_device 各字段）；`extract_env(ws, target_os, materials, categories)`
主循环（R1-R3 自动，R4 答案整合；MAX_AUTO_ROUNDS=3）。
**关系**：P0 核心；调 probe 执行探测；调 kb 注入 runbook 面（起点假设）；
失败 → panic gate `p0.t3.extract`（exit 3）。
**限制**：T3 失败轮不落盘 T3_R{n}.json（H20）；非阻塞备忘覆盖阻塞版 questions
（H20，低优先级）。

#### probe.py
**职责**：探测执行器（纯脚本，读 runner）。
**关键公共函数**：`probe_build()` build 双信号（退出码 0 + success_pattern）；
`probe_boot()` boot 双信号（退出码 0 + success_pattern + 无 panic_pattern）；
`probe_boot_with_device()` 设备注入后 boot；`probe_development()` 三项顺序探测。
**关系**：P0 T3 + P2 验收 + P5 L1/L2 + P6 execute 共用。
**关键语义**：命令经 shell 消费 `PORTER_TARGET_OS_ROOT`（脚本不做文本替换）；
inject_device 双机制（env=合并环境变量 / cmd=追加参数）。

#### gate.py
**职责**：T5 门禁（纯脚本，机器可检）。
**关键公共函数**：`run_gate(ws)` P0 退出门（project.json 完整 + runner 校验
+ T3 三项全 PASS + unit_test 烟测）。
**关系**：P0 最后一步；FAIL → panic gate `p0.t5.env_gate`（硬路由必人）。

### 5.4 porter/divide/ — P1 专属

#### strategy.py
**职责**：P1 拆分策略编排（agent 读 Linux 源码→strategy.md，人工审阅）。
**关键公共函数**：`run_strategy(ws, driver_root)` 一次 agent 调用产出
strategy.md（幂等：存在即复用）+ 草稿入 temp + 知识报告；`promote_sample()`
p1-promote 晋升；`sample_partitions()` 三分区表。
**关系**：P1 第一步；strategy.md 是 P1D/P1R 的输入；CP1 指纹绑定。
**设计**：零 schema 契约（策略是咨询性产物），质量靠人工审阅+divide 客观校验。

#### run.py
**职责**：P1 divide 编排（索引预建+按文件分配+机械展开）。
**关键公共函数**：`run_divide(ws, driver_root)` 主入口（strategy.md 必须存在；
按文件 agent 调用=SKILL+strategy 全文+文件索引切片；校验失败带反馈重试 1 次；
expand→P1D_plan.json+审计；fragments 物理抽取）。
**关系**：P1 第二步；调 index.build_index/render_slice/validate_decision/expand
+ fragments.extract_modules。
**限制**：plan 先写后抽（H6：plan 存在即 return 0，若 modules/ 缺失则
p1-resolve 裸崩——靠 resolve 守恒校验兜底）。

#### index.py
**职责**：定义索引+归属强制+机械展开（纯脚本，零 porter import）。
**关键公共函数**：`build_index(driver_root)` 逐文件扫描出有序定义清单；
`call_order()` .c 先 .h 后；`assignable_symbols()` 需 agent 分配的符号；
`render_slice()` 渲染文件索引切片给 agent；`validate_decision()` 校验单文件
agent 输出（whole_file XOR assignments，全覆盖）；`expand()` 合并→P1D_plan.json。
**设计**：归属规则机器强制（注册宏→被引用符号模块；前向声明→被声明函数模块；
chunk→相邻条目模块）。

#### fragments.py
**职责**：按拆分方案物理抽取代码片段（纯脚本，零 porter import）。
**关键公共函数**：`extract_modules(ws, driver_root, plan)` 致命检查（dest
重名/src 不存在/区间越界）→ 写 P1/modules/<name>/ + module.json。
**不变量**：片段内容=原文行区间逐字拷贝；include 块自动复制到每个抽出文件头部。

#### resolve.py
**职责**：P1 依赖解环编排（agent 搬运循环+守恒校验）。
**关键公共函数**：`run_resolve(ws, driver_root, strategy_path)` 主入口
（符号扫描→依赖图→环检测→无环则拓扑序 deps.json；有环则 agent moves→
机器校验片段存在+守恒→应用→重新抽取→重复；MAX_ROUNDS=3 败 → exit 3）。
**关系**：P1 第三步；deps.json 是 loop 的输入。
**守恒证明**：每轮应用前后，片段展开为单行后的 (src,行号) 多重集完全一致。

#### deps.py
**职责**：依赖图计算/迁移序/环检测/粒度护栏/报告（纯脚本）。
**关键公共函数**：`compute_deps(ws)` 扫 P1/modules → deps.json + report.md。
**关系**：resolve.py 的无 agent 版（两者都写 deps.json；resolve 是 agent 驱动版）。
**注意**：audit 标为"死文件（被 resolve 取代）"——仍存在但 resolve 是主路径。

### 5.5 porter/bootstrap/ — P2 + 知识子系统

#### mapping.py
**职责**：P2a 引导映射编排（agent 分批小调用+机器校验+增量合并）。
**关键公共函数**：`run_map(ws, driver_root, target_os)` 主流程（spine_api→
按域分批≤35→agent→9 字段校验+evidence 路径真实存在→重试≤3→增量合并
mapping.json→渲染 mapping.md→mapping_report.md→draft_knowledge）。
**关系**：P2 第一步；mapping.json 是 P3/P4/探针的"词典"。
**限制**：BATCH_SIZE=35, MAX_TRIES=3, AGENT_TIMEOUT_SEC=900。

#### extract_spine.py
**职责**：P2a 主轴 API 提取（纯脚本，零 agent）。
**关键公共函数**：`run_extract(ws, driver_root)` 扫 P1 模块→外部符号=refs−defs
→内核头文件倒排索引→每符号定域→spine_api.json（幂等：存在即跳过）。
**关系**：mapping.py 前置；用 common.symbol.scan_file。

#### skeleton.py
**职责**：P2b 全局骨架生成（目标 OS 专属模板：Asterinas 起步）。
**关键公共函数**：`run_skeleton(ws, target_os, device_ids)` 写 crate 四件
（Cargo.toml+lib.rs+probes.rs+external_interfaces.rs 平台补齐宿舍——M10
fill 落点约束的宿主）+ 补丁 5 接线点（根 Cargo.toml/Components.toml/
kernel/core/Cargo.toml/driver/mod.rs/net/iface/init.rs）→ skeleton_manifest.json。
**关系**：P2 第二步；骨架=零驱动功能的"入住仪式"。
**限制**：模板 Asterinas 专属（详见第 7 节）；DEFAULT_DEVICE_IDS=`0x8086:0x100e`；
marker 幂等（H11：init.rs 锚点缺失仅⚠+return 0，重跑被 manifest 短路——
设计如此，修复需人工删文件）；members 只插一处不插 default-members（H16）；
VENDOR_ID 只取 ids[0]（H17，多厂商设备 ID 静默错误）。

#### pregen.py
**职责**：P2c 探针预生成（风险主张前置验证）。
**关键公共函数**：`run_pregen(ws, target_os, max_batches)` 预计算全模块使用面→
目标集=并集∩(risk∈{med,high}∪low-confidence)−已探 claim→共享探针生命周期→
pregen_report.md。
**关系**：P2 第三步；P3 探针步骤退化为补新。调 loop.probes.run_probe_lifecycle。

#### run.py
**职责**：P2 入口编排（2a→2b→2c→验收）。
**关键公共函数**：`run_p2(ws, driver_root, target_os, device_ids)` 全流程
（mapping→skeleton→pregen→_acceptance：build+boot+组件日志特征+无 PROBE FAIL；
验收过后 vcs 双 commit：目标树 manifest 路径+工作区）。
**关系**：P2 总入口；调 env.probe 验收。

#### kb.py
**职责**：知识库骨架——目录模型+域注册表+薄 INDEX+通用晋升（知识子系统核心）。
**关键公共函数**：`DOMAINS` 域注册表（maps/gaps/runbook/splits/pitfalls/failures）；
`kb_face(ws, domains, include_temp)` 组装检索注入面（总纲+目录）；`kb_dir_for(ws)`
解析本次迁移的知识库目录（M10 起优先 `<ws>/knowledge/`，旧布局回落）；`kb_ws_dir`/
`temp_root`/`domain_temp(domain, ws, kb_dir)` 工作区化路径（temp 随 ws git
入库）；`validate_kb_arg()`；`select_kb(mode, name, empty, git_ignore, ws)` p0
--kb 物化（new=复制 base→ws / use=全局库种子化→ws；ws 缺省=旧全局布局）；
`sync_to_global()` promote 后回流全局库；`record_consulted()`
hits 旁车计数；`promote_entries()` 通用晋升；`load/save_index`/`upsert_entry`/
`bump_hits` 薄 INDEX 助手；`fold_sidecar_hits` 晋升时折叠旁车。
**关系**：知识子系统依赖根（knowledge/gaps/runbook/candidates/review/mapping
都 `from . import kb`）。规范见 docs/sub-systems/knowledge.md。

#### knowledge.py
**职责**：maps 域收成/晋升。
**关键公共函数**：`draft_knowledge(ws)` 工作区映射表→temp/maps（幂等覆盖）；
`promote_map(driver, kb_dir, target)` temp→知识库目录 maps/（同名=版本更新
替换，hits 取两侧较高）。
**关系**：P2 末+每轮 P3 末刷新；p2-promote 人工晋升（temp 已 ws 化：
`domain_temp("maps", ws/kb_dir)`）。

#### gaps.py
**职责**：gaps 域收成（gap 处置+fill 成败）。
**关键公共函数**：`draft_gaps(ws)` gap_decisions+fill 成败→temp/gaps/<ns>/<api>.md
（幂等）；`prior_entry(kb_dir, api)` 文件名存在性检索（"这个 API 以前 fill
失败过吗"=查文件名，零内容解析）；`sanitize_api(api)` API 名→安全文件名。
**关系**：与 maps 同点收成（p3._refresh_drafts）；P4 fill 前调 prior_entry
（检索面同 ws 化）。
**设计**：一 API 一文件（文件名即 API 名）。

#### runbook.py
**职责**：runbook 域收成（runner.json→temp/runbook）。
**关键公共函数**：`draft_runbook(ws)` 三节（build/boot/unit_test）→
temp/runbook/<target>/<topic>.md（幂等）。
**关系**：P0 末（T5 过后）+ P5 unit_test 回填后收成；T3 R1 注入（起点假设标注）。
（temp 已 ws 化。）

#### candidates.py
**职责**：随机知识探查（四类钩子+去重闸+候选账）。
**关键公共函数**：`record_candidate()` 经去重闸入账；`record_from_gate()`
类 1 钩子（gate 应答 note/rationale）；`record_lessons()` 类 4 钩子（agent
自报 lessons）；`suggest_class(gate_id)` 关口 id→建议类；`load_candidates()`/
`remove_candidate()`。
**关系**：四类钩子挂 gates/p6/p7/p4/probes；候选账
<ws>/knowledge/temp/candidates/<ns>.json（M10 起随 ws git 入账）。
**设计**：候选不进任何注入面（分类进子目录后才可检索）；去重=draft 规范化 sha1 前 16 位。

#### review.py
**职责**：知识审核面（CP5 材料）+分类+晋升/拒绝。
**关键公共函数**：`build_cp5_material(ws)` CP5 备审材料（候选队列+temp 草稿
清点+KB 健康报告）；`classify_candidates(ws, ids)` 一次 agent 批量归类；
`promote_candidate(ws, cid, to)` 候选→域条目+INDEX 行；`reject_candidate()`。
**关系**：P7 末 CP5 触发；classify 调 agent（PORTER_NO_AGENT=1 跳过退人工 --to）。

### 5.6 porter/loop/ — P3-P7 + gates + errorloop

#### run.py
**职责**：垂直循环推进（P3→P4→P5 ×N，拓扑序，断点重入）。
**关键公共函数**：`run_loop(ws, module, max_modules)` 主入口（按 deps.json
拓扑序逐模块走 P3→P4→P5；四种 exit 3 停车：gap human/attempts 烧穿/deferred
无法清偿/blocked；泊车+绕行；债限额软停；FM 首模块审；全完→CP3 指针；
模块 done 后 vcs commit_workspace——两处，含绕行模式）。
**关系**：P3-P5 总编排；MODULE_BUDGET_SEC=3600（超时 panic）。

#### state.py
**职责**：loop_state.json 状态机（断点重入）。
**关键公共函数**：`class LoopState` 管理 order+modules{phase,attempts}；
`parse_answers(ws)` 解析 answers.md `## <key>` 节；`consume_answers(ws, keys)`
消费即移除。
**关系**：loop run.py 的状态持久化；MAX_ATTEMPTS=3。

#### surface.py
**职责**：P3(M) 使用面提取（外部符号四分类）。
**关键公共函数**：`extract_surface(ws, driver_root, module, force)` →
surface.json（cross_module/mapped/missing/noise + #include + 使用位置）。
**关系**：P3 第一步；与 extract_spine 互补（并集 vs 模块视角）。
**限制**：force 参数死置 False（H15：无调用方传 True→永不刷新；P3 有活映射
对账补偿但 stats/gaps 报告失真）。

#### criteria.py
**职责**：判据 schema 校验 + L0-L4 复核执行器。
**关键公共函数**：`validate_criteria(raw, module)` schema 校验；`baseline_criteria(module)`
基线 L1 compile+L2 boot；`check_unit_test(output, names)` ktest 判定；
`check_log_pattern(log_text, pattern)` qemu.log 正则。
**关系**：P3 草案→P5 复核→P6 重判共用。零 porter import（纯 stdlib）。

#### p3.py
**职责**：P3(M) 增量映射+gap 分类+判据草案+探针。
**关键公共函数**：`run_p3(ws, module, order)` 7 步流水线（surface→answers
消费→missing 映射→gap 分类→criteria 草案→探针→收尾刷新草稿+report）。
**关系**：P3 入口；gap human → exit 3（panic_gap_gates）。
**限制**：exit-3 路径已刷新草稿（H18 已修复）。

#### p4.py
**职责**：P4(M) fill 统一+切片迁移+轮末冒烟。
**关键公共函数**：`run_p4(ws, module, order)` 三步（fill→migrate→quick_smoke）。
**关系**：P4 入口；blocked 立即停（H13 已修复）；同签名连发 2 次=早退
（SAME_SIG_REPEAT=2）；MAX_LINES_PER_SLICE=900；成功末 vcs commit_target
（crate+接线文件）。fill 落点约束（M10）：平台补齐一律写
crate/src/external_interfaces.rs，仅接线文件例外。
**限制**：fill 回退不闭环（H7：fell-back 只写 fill.json，不回写 gap_decisions
→迁移 prompt 仍渲染 fill 指令——部分修复：迁移阶段新撞 gap 会回写）。

#### p5.py
**职责**：P5(M) 模块级验收 L1/L2/L0/L3+累积回归+deferred+求解挂载①。
**关键公共函数**：`run_p5(ws, module, order)` 验收流水线（unit_test 回填→
L1 build→L2 boot→L0 ktest→L3 log_pattern+累积回归→deferred 登记/清偿→
求解挂载① `_solve_failures`→`p5.unsolved.<M>` 关口）；`acceptance_path()`。
**关系**：P5 入口；_judge_core 被 errorloop 复用；_solve_failures 调
errorloop.run_solve_loop。
**限制**：GLOBAL_SENTINEL=`__P6__`（P6 是 owner；P5 循环不可清）。

#### p6.py
**职责**：P6 系统验收（聚合/execute/finalize-l4/defects+求解挂载②③）。
**关键公共函数**：`run_p6()` 总分发；`aggregate()` 聚合健康（零重测）；
`execute(l4)` 一轮 build+SLIRP boot+ktest→全判据重判+deferred 清偿+求解挂载②；
`draft_l4()` --draft-l4 生成器（H1 已修复）；`finalize_l4()` 审核门（CP3
指纹绑定）；`diagnose_defect(did)` 挂载③（求解+闭账+CP4 债）；`fix_defect()`
重定向垫片。defect 账本：`add/close(四字段强制)/park/bump`。
**关系**：P6 入口；_execute_judge 被 errorloop 复用。
**限制**：SLIRP 设备参数硬编码 e1000 回落（H14：DEFAULT_EXEC_DEVICE_ARGS
含 e1000，真工作区 runner 只有 `net` 键时靠硬编码撑）；execute 定义重复
（第一个被第二个覆盖，第一个是死代码——audit #19）。

#### p7.py
**职责**：P7 终态报告+上游补丁台账。
**关键公共函数**：`run_p7(ws)` 聚合（P0→P6 全产物+git baseline diff+crate
统计+补丁台账→final_report.json/.md，M10 起含 commit_chain 节）；`run_p7_cli()`
CLI 分发（patch-register/
patch-status/默认聚合）；`baseline_diff()` git diff+ls-files；`crate_stats()`；
`mapping_stats()`；补丁台账 `register_patch`/`set_patch_status`。
（cmd_p7 末：vcs commit_workspace + export_all 自动导出→<ws>/exports/。）
**关系**：P7 入口；CP4 缺陷债批审入口；CP5 知识沉淀提醒（非阻塞）。
**设计**：final_report 是数据驱动骨架，"结论与去向"节留给人工撰写。

#### probes.py
**职责**：高风险探针机器（生成/同步/判定/共享生命周期）。
**关键公共函数**：`run_probe_lifecycle()` 共享生命周期（生成→同步→build→
编译回炉→boot→判定→FAIL 有界改判→降级 gap）；`boot_and_log()` 共享 boot
双信号+日志获取；`filter_risky()` 筛选；`sync_probes_rs()` 整体再生成
probes.rs（确定性幂等，手改会被覆盖——有意设计）；`collect_sections()`
聚合全部探针节；`known_claims()` 跨注册表去重；`judge()` 判定。
**关系**：P2c pregen + P3 补新 + P4 fill 探针共用；boot_and_log 被 P4/P5/P6 共用。
**限制**：collect_sections 跳过 current_module 的 P3/P4（H8：P4(M) fill 的
sync 会把 M 自己的 P3 探针临时挤出——设计取舍：当前模块用 current_reg_path
避免竞态，流程中止则回归哨网缺员）；_log_face 三态（file/stdout/empty/missing，
H9 已修复）。

#### ut_verify.py
**职责**：unit_test 配置机器烟测（"agent 说"→"机器验"）。
**关键公共函数**：`verify_output(output, success_pattern, fail_pattern)` 纯
判定（去 ANSI+success 在+fail 不在）；`run_and_verify(cmd, cwd, env, timeout,
log_path, success_pattern)` 跑一次+断言；`feedback_block()` 失败反馈块；
`smoke_unit_test_config()` 烟测一个 ut 配置（smoke_cmd 优先）。
**关系**：P0 门禁第一道（env/gate.py）+ P5 补探第二道共用。
**设计**：双道烟测（教训：agent 声称"结果打 stdout"是错的，"ok"被 ANSI 包裹）。

#### gates.py
**职责**：人工关口账本（两车道+一账本+panic 统一入口+应用器注册表）。
**关键公共函数**：`class GateLedger` 账本管理（load/save/find/open_blocking/
pending_review/add/note/mark）；`panic(ws, spec, evidence)` 统一 panic 入口
（登记+路由自动应答尝试+快照+聚类检测+渲染+return 3/0；入口挂 vcs stop
commit——13 处 exit 3 全覆盖）；`process_answered_gates()`
消费 answers.md @ 节（校验→记账→应用；applied>0 挂 vcs answers commit——
8 处入口全覆盖）；`render_human_questions()` 唯一
写者；`checkpoint_run()`/`strategy_checkpoint()` CP1 指纹绑定；应用器注册表
（_apply_retry/_apply_decision(gap/deferred)/_apply_approval(指纹)/_apply_fact/
_apply_memo）；`self_diagnosis_enabled()` 熔断总开关。
**关系**：全相位 exit 3 统一入口；routing 是自动应答链；规范见
docs/sub-systems/human-intervention.md。
**设计**：history append-only；账本写盘原子（tmp+rename）；CLUSTER_THRESHOLD=3。

#### routing.py
**职责**：三级分流路由（rules/agent/human——谁第一个应答）。
**关键公共函数**：`route_for(gate_id, ws, routing)` 层链解析（硬路由>键覆盖>
内置默认>default）；`maybe_auto_answer(ws, ledger, gate)` 人前自动应答
（rules→agent，仅 kind=decision）；`consult_policy()` rules 层（policy.md
agent 解释+命中留痕）；`agent_answer()` agent 层（gate-answer skill+KB 检索，
仅已审）；`debt_count()`/`debt_limit()` 债计数/限额。
**关系**：gates.panic 调 maybe_auto_answer；HARD_HUMAN_IDS 四点必人。
**设计**：自动应答仅 kind=decision；PORTER_NO_AGENT=1 两层全跳过。

#### errorloop.py
**职责**：错误处理模块核心——知识辅助的 agent 求解循环。
**关键公共函数**：`run_solve_loop(ws, failure, verify, cfg)` 主入口（≤3 轮
agent 求解+同签名早退+动作词表执行+verify 回调+知识回流候选+耗尽升级报告）；
`failure_signature(subject, detail, log_text)` 规范化哈希签名（去 ANSI/路径→
basename/时间戳→TS/独立数字→N，标识符与错误码内数字保留）。
**关系**：三挂载（p5._solve_failures / p6._execute_judge+solve / p6.diagnose_defect）
调用；调 log.query 组装证据包+轮间上下文；调 kb 注入 failures+pitfalls 面；
调 diagnose.generate_escalation_report 生成升级报告；fix-code 动作后 vcs
commit_target（每次修码一条，三挂载全覆盖）。
**限制**：MAX_ROUNDS=3, SAME_SIG_REPEAT=2, AGENT_TIMEOUT_SEC=1200。
规范见 docs/modules/error-handling.md。

#### diagnose.py
**职责**：升级报告生成（错误处理模块的报告面）。
**关键公共函数**：`generate_escalation_report(ws, source, subject, symptom,
triage_verdicts, diagnosis, cfg)` 编排器生成（零 agent）六字段 schema
（symptom/env_snapshot/excluded[{hypothesis,evidence,ref}]/experiments/
remaining/reproduce + evidence_files 全指不可变快照 + signature_candidates
→ kb 候选账）。
**关系**：errorloop 耗尽终态调用；调 log.query 读事件流；调 candidates 记签名候选。
**历史**：原 run_diagnosis（2 轮深诊）+ build_context_pack 已被 errorloop 吸收。

#### events.py
**职责**：观测地基兼容门面（re-export porter.log，旧调用点零改动）。
**公共面**：append_event/bind/bound/unbind/read_events/tail_events/note_agent_start/
note_agent_end/note_cmd_start/note_cmd_end/take_failure_snapshot（11 个 re-export）。
**关系**：14 个存量调用点经此门面导入；新代码应直接 import porter.log。

### 5.7 porter/log/ — log 子系统（统一观测框架，纯静态零 agent）

规范见 docs/sub-systems/log.md。

#### core.py
**职责**：record() 唯一写入口（双 sink 分发+上下文戳+派生助手）。
**关键公共函数**：`record(kind, subject, summary, ...)` 双 sink（console+
events.jsonl）；`console_only()` 纯 console；`console_line()` 整行直打
（print 扫尾统一映射）；`ctx(**stamp)` 上下文戳（显式>ctx>bind）；`phase_begin`/
`phase_end`/`judge` 派生助手。
**设计**：一次调用双 sink；schema 只增不改；观测纪律（永不抛异常，字段截断 400）。

#### store.py
**职责**：events.jsonl 存储（机读 sink，真值源）。
**关键公共函数**：`bind(ws, mount)`/`unbind()`/`bound()` 进程级绑定；
`append_event(kind, ...)` append-only 写（v1.1 附加字段只增）；`read_events(ws)`/
`tail_events(ws, ...)`；`note_agent_start/end`/`note_cmd_start/end` 埋桩助手。
**设计**：未绑定=no-op（向后兼容）；_MAX_FIELD=400 截断。

#### console.py
**职责**：console sink（人读面）。
**关键公共函数**：`format_line(scope, text)` 渲染 `[porter] <scope>: <text>`；
`emit(scope, text, level)` 打印（级别门控）；`emit_line(line, level)` 整行直打。
**设计**：PORTER_LOG_LEVEL（debug/info/warn/error，缺省 info）级别阈值；永不抛异常。

#### query.py
**职责**：查询/run 登记/上下文接续 API（消费面）。
**关键公共函数**：`events(ws, kind_prefix, subject, phase, module, run_id, limit)`
结构化过滤；`runs(ws, subject, last_n)` agent 运行登记（agent_start/end 配对）；
`context_block(ws, subject, includes, tail_lines)` 上下文接续（拼 prompt 用）；
`timeline(ws, module, limit)` 浓缩时间线；`tail_text`/`tail_block` 尾部块。
**设计**：全部 events.jsonl 派生读（无独立账本）；永不抛异常。

#### snapshot.py
**职责**：失败即快照（不可变证据束）。
**关键公共函数**：`take_failure_snapshot(ws, source, subject, reason, runner,
target_os, extra_files, extra_env)` 判定 FAIL 第一时间抢救→
ws/failure-snapshot-<n>/（qemu.log/串口/判定输入/内核哈希 best-effort/QEMU 命令行/
criteria+mapping 状态+manifest.json）。
**设计**：内核镜像只存哈希不复制；mapping.json 超 2MB 只记 sha256；单文件>5MB
裁剪（头 1MB+尾 2MB，manifest 记 clipped:true）。

---

## 6. 已知限制与未做项

### 6.1 TODO.md（10 条，已商定但不做在本轮）

| # | 项 | 状态 | 入手点 |
|---|---|---|---|
| 1 | 工作区级配置覆写通用机制 | 待做 | 仅 routing.gates/default 支持 ws 覆写；checkpoints/policy_file/panic 阈值只认仓级 |
| 2 | log 子系统真实 e2e 验证 | 残余项 | 下次真实迁移轮顺带：porter log tail/timeline/runs 核验现场形态 |
| 3 | 错误处理模块首个真实迁移轮实测校准 | 残余项 | 求解判定质量/轮数分布/kb_consulted 命中率/criteria 修正债审计实操 |
| 4 | p6 私有 boot 助手与共享版去重 | 待做 | _boot_and_log vs probes._recover_boot_log 近重复 |
| 5 | 知识分类子目录完备性 | 待做 | 复盘一次真实迁移候选检验五类覆盖度 |
| 6 | 固定知识差异化检索/使用设计 | 待做 | kb_consulted 遥测验证统一指针化后映射质量无回归 |
| 7 | ktest 静默案的无会话复演验证 | 待做 | 按时间轴复盘 ktest 案找证据面缺口 |
| 8 | corpus→base 通用化晋升 | 待做 | porter kb to-base 类命令（人工触发） |
| 9 | CP5 知识审核细节细研 | 待做 | approve/reject 是否上关口表单；健康报告阈值化 |
| 10 | vcs 真实迁移轮 e2e 校准 | 待做 | commit 粒度/分支切回/bundle 往返实战；resume 树脏仅告警无 stash |

### 6.2 仍存在的连续性漏洞（audit H 项，剔除已修复）

**已修复（不再列细节）**：H1（draft_l4 生成器）、H4（defect-fix 并入求解
循环）、H5（CP1 直通也拦）、H9（日志三态）、H10（boot.timeout_sec 校验）、
H13（blocked 立即停）、H18（exit-3 路径刷新草稿）、H20（T3 失败轮落盘，
部分修复）。

**仍存在**：

| # | 项 | 严重度 | 现状 |
|---|---|---|---|
| H2 | L4 自测代码无工具化生成 | 高 | grep porter/+skills 零生成器；新驱动到 P6 时 L4 断链 |
| H3 | P6a 预实测缺失 | 中 | 设计已并入 execute 模式（aggregate 零重测+execute 重测） |
| H6 | P1D plan 先写后抽假成功陷阱 | 中 | 靠 resolve 守恒校验兜底；plan 存在即 return 0 |
| H7 | fill 回退不闭环 | 中 | fell-back 只写 fill.json 不回写 gap_decisions（部分修复：迁移阶段新撞 gap 回写） |
| H8 | probes.rs 聚合挤掉当前模块他相位探针 | 中 | 设计取舍：current_module 用 current_reg_path 避免竞态；流程中止则哨网缺员 |
| H11 | skeleton 幂等短路吞锚点漂移 | 中 | marker 存在即跳过；init.rs 锚点缺失仅⚠+return 0（设计如此） |
| H14 | p6 SLIRP 设备参数硬编码 e1000 | 中 | DEFAULT_EXEC_DEVICE_ARGS 含 e1000；真工作区 runner 只有 `net` 键时靠硬编码撑 |
| H15 | surface force 参数死置 | 低 | 无调用方传 True→永不刷新；P3 有活映射对账补偿 |
| H16 | skeleton members 只插一处 | 低 | 不插 default-members（目标树自述应入 default-members） |
| H17 | VENDOR_ID 只取 ids[0] | 低 | 多厂商设备 ID 静默错误 |
| H19 | 跨模块探针降级无对账 | 低 | M2 降级某 claim 不回头改 M1 的 gap_decisions |
| H22 | P2 acceptance.json 无机器消费者 | 低 | 纯人读产物 |
| H25 | 分层倒置 | 低 | env 层反向 import loop 层（延迟 import+try 防御，未成环） |
| H26 | 无总编排命令 | 低 | 设计使然（p0→p7 逐段手动跑） |

### 6.3 其他已知限制

- **工作区覆写范围**：routing.json 仅覆写 routing.gates/default；其余键只认仓级（TODO #1）。
- **错误处理债审计面**：criteria 修正债 + 求解闭账债均为非阻塞 checkpoint 条目，
  只进 CP digest 批审，不即时打扰。
- **CP2 默认关**：依据 = e2e 实证（无映射人审跑通）+ 下游机器验证兜底。
- **FM 只对拓扑首模块生效**：`--module` 绕行路径不触发 FM。
- **自动应答 agent 调用成本**：每个 decision 类 panic 尝试 policy→agent 两层（各一次有界调用）。

---

## 7. 通用性现状（audit §8 更新）

### 7.1 结构性专属（换目标 OS / 换驱动必须改代码）

| 点 | 证据 | 说明 |
|---|---|---|
| **skeleton.py 骨架模板整体 Asterinas 专属** | crate 布局 `kernel/core/comps/<driver>`、`aster-*` 命名、依赖清单、Components.toml 组件模型、lib.rs 模板（OSTD/aster_pci API、`#[init_component]`、ktest 位）、init.rs 接线桩锚点写死 virtio 先例字符串 | 无第二目标 OS 抽象层 |
| 驱动 crate 路径写死 | p4.py/probes.py/pregen.py 四处独立硬编码 `target_os/kernel/core/comps/<driver>` | |
| probes.rs 文件格式+PROBE_ 日志约定 | skeleton.py + probes.py + bootstrap/run.py | 组件 init 调 run_all + PROBE_<name> PASS/FAIL 行 |
| p6 SLIRP 默认设备串含 e1000 | p6.py DEFAULT_EXEC_DEVICE_ARGS | H14 |
| skeleton 默认设备 ID | skeleton.py DEFAULT_DEVICE_IDS=`0x8086:0x100e` | 可 --device-ids 覆盖 |
| skills/P4-migrate 硬约束 | 忙等规则、trait 抽象、仪式测试——Asterinas 惯例 | 换 OS 需重写 skill |
| failures 域 lineage 签名 | knowledge/asterinas/failures/ 4 条环境特定签名 | 按 lineage 隔离，跨迁移不污染（设计如此） |

### 7.2 数据驱动面（干净，通用）

runner.json 全部命令/特征/设备参数（P0 探明）；symbol.py 通用 C 词法
（仅 Linux 方言假设）；P0 三 skill 机制中立（unit-test-discover 不预设框架）；
probe 双信号判定；category 标签集在 skill 层（改清单改 skill 不改代码）；
p1 索引 Linux C 方言正则（输入即 Linux，合理）。

### 7.3 通用性结论

P0/P1 层与数据面基本可复用于任意目标 OS/驱动；**P2b 骨架起（含 P4 写树、
探针宿主、P6 SLIRP、L4 形态）为 Asterinas+net 驱动定制**。第二目标 OS
的现实路径 = skeleton/probes 宿主模板化 + skill 集参数化。

---

## 附录：验证方法

- **静态**：4 路并行 explore agents（env+divide / bootstrap+symbol / loop 全 /
  log+common+main）逐文件全读，行号引用经抽查核对。
- **动态**：全套件 `python3 -m unittest discover -s tests` →
  `Ran 164 tests … OK`（131 存量 + test_vcs 22 + test_vcs_wiring 11）。
- **回归验证**：连续两次全量套件后 `git rev-parse HEAD` 稳定
  （CWD 误提交防护生效）。
- **vcs 冒烟（M10）**：真实 git 走 登记→commit→幂等→export→
  `git bundle verify`→clone 后 import→hash 保留；真 run_agent（opencode
  缺席自然 rc=127）验证 pre-agent/agent 成对隔离。
- **真工作区抽样**：migrations/e2e-test-retry（e1000 全流程已跑完）只读抽样
  runner.json/mapping.json/defects.json/gates.json 等。
- **交叉核对**：本报告与 2026-09-01 旧审计报告（HEAD `3fcc897`，58 测试）
  逐项核对，剔除已修复项，保留仍存在项并更新状态。
