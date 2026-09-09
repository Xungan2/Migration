# exp-mono 流程优化——交接文档

写作日期：2026-09-09。执行者：负责 exp-mono 流程优化的同学。
范围：**流程与编排层**——知识沉淀、任务拆分、人工介入规范化、测试指标。
验证强度（gate 判据/两级编译/溯源）不在本文，见姊妹篇
`docs/research/exp-mono-gate-hardening-handoff.md`（下称"硬化篇"）；两篇互补，
硬化篇管"验证得对不对"，本文管"跑得顺不顺、经验留没留下、人闲不闲得下来"。

诊断证据来自本机 `migrations/spinor-mono-e2e-20260908/ws-spinor-mono/`
（gitignored，文件在本机磁盘），该工作区将随本文一并移交，是全部分析的
样例底料。时间轴中所有时刻/时长核实于 `exp-mono/logs/` 文件时间戳、
`exp-mono/ledger.json`、`/tmp/opencode/spinor-mono/exp_*.log`（porter 各次
运行的完整 stdout，本机保留）。

---

## 1. 任务背景

### 1.1 exp-mono 与本次样例

exp-mono（`porter/exp/mono.py`）是单体模块迁移实验子命令：一个迁移者
agent 一次迭代迁一个模块（研究+翻译+测试+记账一体），经四段复合静态
gate（① 产物守卫 ② 构建 ③ 启动 ④ 单测）验证后 pass 并 commit 目标树。
2026-09-08 夜至 09-09 晨，spi-nor 迁移在该流程上真跑了 4 个模块
（nor-defs / nor-caps / nor-regs / nor-ids），**4/12 pass**，累计：
源 1927 行 → Rust 5869 行，91 个 `#[ktest]`，词典 72 条，泊车 3 条，
目标树 6 个 commit。porter 运行发起 15 次（其中 1 次被人工中止），
25 个 agent 段，人工介入 11 处（详见第 5 章）。

**结论先行：流程能跑通，产出质量可接受（测试全绿、映射可溯），但——**
知识沉淀停留在工作区内部（跨迁移零复用）、单模块任务粒度过大（研究段
占 60-96% 时长）、人工介入密度过高（平均每模块 2.75 次）。这三点正是
本次优化的对象。

### 1.2 样例工作区速览（第 4 章有完整导览）

```
migrations/spinor-mono-e2e-20260908/ws-spinor-mono/
├── project.json / runner.json / runner.md      # 手工组装的 P0 产物
├── P1/{strategy.md, scope.json, modules/, reports/}   # spi-nor-final P1 交付物拷入
├── P2/reports/scaffold_manifest.json + P2/logs/       # 手工骨架（origin: manual）
├── exp-mono/
│   ├── ledger.json          # 4 模块记账（注意：每模块只保留最后一次 run 的条目）
│   ├── mapping-notes.md     # JIT 词典，72 条平铺
│   ├── parking.md           # 泊车 3 条
│   ├── report.md            # （4/12 时未生成——全 order 完成才写）
│   └── logs/                # 段级 prompt/日志 + gate 四段日志（注意跨 run 覆盖）
└── 目标树 /tmp/opencode/spinor-mono/asterinas（分支 porter-spi-nor-mono）
```

---

## 2. 三大优化方向（需求原意收录 + 实证 + 建议）

以下三个方向为用户原述需求（收录大意），每条附本 session 的实证与
可考虑的落点。**落点是建议不是结论**，方案由执行者设计评审。

### 2.1 方向一：知识沉淀规范化

**需求原意**：API 映射、编译问题解决等知识需要规范的沉淀机制。

**现状实证（本 session）**：

1. **词典（mapping-notes.md）质量高但生命周期止于工作区**。72 条，格式
   `符号 | verdict(equivalent/adapt/helper/bypass) | 目标用法 | 证据 file:line`，
   证据全部指向目标树真实源码（agent 亲读核实，EXP-mono-migrate 纪律二）。
   样例（工作区 `exp-mono/mapping-notes.md`）：

   ```
   - enum spi_nor_read_command_index (core.h:43-67) | adapt | usize 常量 SNOR_CMD_*
     （C 仅作数组下标；且 -Dwarnings 下 enum 前缀 lint 致命） |
     kernel/core/comps/spi-nor/src/defs.rs:371-392 + tools/clippy_check.sh:71
   ```

   问题：(a) **不进知识库**——kb（`knowledge/` 子系统，协议见
   `docs/sub-systems/knowledge.md`）有完整的"agent 起草 + 人工晋升 /
   固定知识 + 随机知识 / 目录自取"协议，但 exp-mono 的词典完全绕开它，
   `kb_dir=spinor-empty` 全程为空且 mono.py 零 KB 引用；(b) **跨迁移零
   复用**——同驱动重跑或同目标 OS 第二个驱动（如 spi-nor 之后的
  下一个）时，"ostd 无 MTD 子系统""bit()/genmask 自持 helper"这类
   结论要全部重新付一遍研究成本；(c) 无分节——72 条平铺（dm-zero 轮
   先例是按模块分节的），跨模块检索靠条目自述。
2. **编译问题的解法散落在 gate 修复段里，无沉淀**。四轮 gate 修复
   （S2/S3/S4 段）里 agent 解决的编译错误（如 `-Dwarnings 下 enum 前缀
   lint`、`//!` 文档注释位置 E0753 先例）只存在于段日志，下一模块撞
   同样的错误时未必避免（事实上 ids 轮又撞了钩子契约问题，见下）。
3. **契约锁死是知识沉淀缺位的典型恶果（泊车 #3，必读）**：
   nor-defs（第一模块）确立的类型契约
   `SpiNorSet4byteAddrModeFn = fn(&mut SpiNor, bool) -> i32`（defs.rs:522）
   没有预留 transport 依赖；到 nor-ids 轮，winbond/micron-st 的
   set_4byte_addr_mode 实现需要 transport+bouncebuf 才能执行寄存器 IO，
   而"前轮产物只追加不可改"纪律使契约无法回修——agent 只能把实现
   落成模块自由函数、钩子位暂存 None 并留注释壳（micron_st.rs:347 /
   winbond.rs:383），泊车待契约修订。**并且它预判了后继模块的同类
   冲突**（qe 的 quad_enable 将撞同一契约）。这条泊车是"模块间共享
   知识/契约需要版本化与回修机制"的最强实证：单体 agent 顺序迁移时，
   每个模块只看见"当前词典+已迁产物清单"，没有一处系统性记录
   "已定契约、约束了什么、未来谁会撞"。

**可考虑的落点**：

- 词典进 kb：把 mapping-notes.md 定位为 kb"固定知识（API 映射）"的
  草稿源——每模块 pass 后自动收成候选，CP 检查点人工晋升（协议现成，
  缺的只是 exp-mono 的进料点）；verdict 词表对齐 kb 的 maps 域。
- 编译问题沉淀：gate 修复段的失败→修复对（error 摘录 + 解法 + 证据）
  自动收成"随机知识"候选。
- 契约登记表：跨模块共享的类型/钩子签名在词典或独立文件里单列
  （"契约"类条目：签名、确立者、已知约束、待回修项），后继模块 prompt
  显式注入；配合"回修窗口"机制（允许在泊车记录在案时对前轮契约做
  受控修改——这需要放宽"只追加"纪律并配守卫，方案由执行者定）。

### 2.2 方向二：大任务拆分与小 agent 协作（handoff）

**需求原意**：判断哪些交给 agent 的大任务实际上可以拆分，交给几个
独立的、或通过 handoff 交接的小 agent 完成。

**现状实证（时长结构，数据见第 5 章各模块）**：

| 模块 | S1 主段（研究+翻译） | gate 修复段合计 | 记账补齐段 | S1 占比 |
|---|---|---|---|---|
| nor-defs (glm-5.2) | ~855s | 44+42+45=131s | 74s | 87% |
| nor-caps (glm-5.2) | ~598s | 6+71+25=102s | 30s | 85% |
| nor-regs (flash) | 2400(耗尽)+~1133s | 91+64=155s | 291s(含修复) | ~93% |
| nor-ids (flash) | ~2302s | ~110s | 47+82+112+140=381s | ~96% |

S1 主段 prompt 171-217 行（skill 全文 + 平台事实 + 模块规格 + **词典
全文** + 泊车全文 + 依赖产物清单），会话 token 峰值 12 万-26.7 万
（随词典/已迁产物增长）。**单体 agent 在一个 session 里串做四种性质
迥异的工作**：

1. **研究/映射**（读 Linux 规格 + 读目标树找等价物 → 词典条目）——
   只读、可离线、产物是知识（词典/泊车/契约），**跨模块可复用**
   （regs 研究出的寄存器原语结论 ids/qe 都要用，但 ids 只能经词典
   摘要间接获得，regs 读过的目标树代码线索全部丢失）；
2. **翻译**（写代码）——吃研究产物，与 1 强耦合但可分离；
3. **gate 修复**——机械性强（编译错误→改），当前以"静态段失败回灌
   同 session"实现（S2-S4 段 prompt 仅 3-4 行），这已是小任务形态；
4. **记账**（done JSON 六字段，声明面核对）——纯机械，却成为最频繁
   的停车原因（见 2.3）。

**可考虑的落点**：

- **研究/映射独立成任务**：一个"映射研究 agent"按模块簇（如
  regs+ids+qe 共用的寄存器层）产出词典增量+契约提案，翻译 agent 消费
  词典干活。研究产物经 handoff 传递——handoff 基建已在
  （`porter/handoff/` + `docs/sub-systems/handoff.md`：TaskSpec /
  execution / handoff.md 生命周期、provider 证据私存），exp-mono 接入
  即可。收益：研究去重（跨模块共享一次研究）、翻译 agent 的 prompt
  与 token 显著变小（flash 模型的 S1 惨剧主要来自研究+长上下文）、
  研究产物可独立人审（知识沉淀的进料点也在这里，与 2.1 咬合）。
- **记账预检工具化**：done JSON 的声明面核对
  （`_declaration_problems`）可以前置为**提交前预检**——agent 输出
  done 前由工具/小程序跑一遍同样的核对并回灌（"以下 3 项未挂
  tests/untested、tests 数组第 5/7 条重名"），把停车消灭在 session
  内。这是零风险高收益项。
- **骨架（P2 scaffold）数据驱动化**：本 session 骨架纯手工（crate 四
  文件+接线 6 处+三绿验证+manifest，约 1.5h 人工）。接线 6 处对同一
  目标 OS 是**纯重复劳动**（dm-zero 与 spi-nor 两轮的 diff 形态完全
  同构），可由工具按 manifest 模板生成；probe_channel/gen_rules 等
  crate 内纪律已在 manifest 里数据化，生成器消费它即可。
- 拆分的**代价**要评估：handoff 交接有开销（文档/校验/新 session
  冷启动——本 session 实测 opencode 新会话启动延迟约 4 分钟，见
  6.4.1），拆得过细会吃掉收益。建议从"研究/翻译分离"这一个切口
  开始实验，用本工作区数据做对照组。

### 2.3 方向三：人工介入减少与规范化

**需求原意**：减少需要人工介入的部分，或让人工介入方式更规范（参考
人工介入子系统规定的两种介入方式）；一旦 agent 不能自己完成，参考
P0 的 agent 循环退化成类似交互式的方式。

**两种介入方式**（`docs/sub-systems/human-intervention.md`，README
集成指引同源）：**checkpoint**（计划内检查点批审：固定介入点 +
answers.md 应答协议）与 **panic**（计划外异常停车：panic 信号表 +
应答后续跑）。P0 的 T3 探测已有现成的"关口-应答"循环先例
（agent 耗尽→同 session 总结→关口提问→人答→种子续跑，
AGENTS_2 T3 v2 节）。

**本 session 人工介入全景（11 处，第 5 章逐条有现场细节）**：

| # | 时刻 | 类别 | 问题 | 消除/规范化路径 |
|---|---|---|---|---|
| 1 | 22:08 | 环境 | 模型名裸写 `GLM-5.3-flash` 报 server error | 可消除：启动时模型名校验（provider 前缀检查/试连） |
| 2 | 22:24 | 模型 | flash 902s 零产出预算耗尽 → 换 glm-5.2 | 半可消除：预算耗尽时自动 --session 续跑一次；换模型决策关口化（checkpoint：报告产出进度，人选"续/换/停"） |
| 3 | 22:25 | 操作 | 用户中止同步阻塞跑 | 已消除：改 nohup 后台+轮询（操作面教训，不是工具缺陷） |
| 4 | 23:10 | 记账 | defs decl-mismatch 20 项 → 续跑 | 可消除：见 2.2 记账预检 + 报错增强（报逐条+重名） |
| 5 | 23:34 | 诊断 | caps S1.log 34min 不存在（虚惊） | 观测面：agent 段日志段末落盘 → 流式落盘或心跳文件 |
| 6 | 00:05 | 记账 | caps decl 1 项 → 续跑 | 同 #4 |
| 7 | 01:43 | 预算 | regs 2400s 耗尽（半成品已落） → 续跑 | 可消除：预算耗尽自动检查代码增量>0 即自动续跑（当前需人判断"值得续"） |
| 8 | 02:38 | 记账 | regs decl 5 组 → 续跑 | 同 #4 |
| 9 | 04:07 | 预算 | ids S4 修复段 3s 被累计预算杀 | 可消除：预算按段下限保护（修复段不该被主段吃光预算）；或预算耗尽即关口化 |
| 10 | 04:15-04:46 | 记账 | ids decl **四连停**（13 项→27vs16→17vs16→PASS），第 4 次人工对照定位**重复声明**根因 | 双消除：报错增强（重名/逐条 diff 机械化）+ skill 增补"tests 不得重名、须与 #[ktest] 一一对应" |
| 11 | 贯穿 | 流程 | Cargo.lock 副产物越界隐患（骨架期处置） | 可消除：产物守卫对"构建系统锁文件"类副产物的显式策略（数据面声明，非白名单硬编码） |

**规律**：11 处中 6 处是同一类（声明面记账），全部可由"预检+报错
增强"压缩到 ~0；2 处预算类可自动续跑或段级保护；2 处环境/观测类是
独立小修；真正的"判断型"介入只有 #2（换模型）一处，适合关口化。

**交互式退化（需求第三句）**：当前 exp-mono 的停车（rc 1）只会打一行
日志，人介入的方式是 shell 层重启命令——这正是本次介入"影响工具
独立性"的形态。建议对齐 P0 先例：停车时渲染
`human_questions.md`（本次为什么停、可选项、建议），人答
`answers.md`（verdict + 参数），重跑时编排器按答案行动（续跑/换模型/
跳过/改数据面）。已有基建：gates.py 的关口登记/指纹/answers 协议
（CP1/exp-accept.plan 都在用），exp-mono 停车点接入即可。这样每次
介入留下结构化记录（何时/何事/人给了什么决定），也是可分析的
数据（介入密度本身成为流程质量的度量）。

### 2.4 测试指标：代码覆盖率（含讨论点）

**需求原意**：单元测试加上代码覆盖率指标；**豁免（untested）的单元
是否算入覆盖率由执行者与用户讨论**。

本 session 数据：91 个 `#[ktest]` 执行式测试 + 16 个声明豁免
（defs 3 + ids 13，全部带理由与源行号锚，`ledger.json` untested 字段
可查）。当前 gate 判据只有"测试执行通过 + 标记计数一致"，**无任何
覆盖度量**——一个 800 行模块用 3 个测试和一个 100 行模块用 3 个测试，
在账面上无法区分。

讨论点两面论据（留给执行者与用户定夺，本文只摆事实）：

- **豁免计入**的理由：豁免项有理由与源行号锚，是"已识别并裁定"的
  面，与"完全没看过"有质的区别；不计入会激励 agent 滥用豁免（豁免
  比写测试便宜）——虽然当前豁免必须给理由，但理由无人工审。
- **豁免不计入**的理由：覆盖率的本义是"被执行验证过的代码比例"，
  豁免恰恰是承认未验证；计入会使指标失去防御意义。
- **折中形态（供参考）**：双指标——`executed_coverage`（只算 tests）
  与 `identified_coverage`（tests + untested），豁免单列计数并抽样
  人审理由。豁免率本身（16/107≈15%）可作为 agent 质量信号。
- **接入的现实问题（需执行者调研）**：ktest 是内核态 QEMU 执行，
  覆盖率插桩需要目标侧工具支持（Rust coverage 工具链在 no_std/
  osdk test 环境的可用性未知）；退化方案是用结构化计数（迁移单元
  数/函数数/常量组数 vs 已测数）做"单元覆盖率"而非行覆盖率——
  migrated_functions 本身就是分母清单，机械可算。

### 2.5 测试相关的另一问题

用户已单独成文：`docs/research/exp-mono-gate-hardening-handoff.md`
（验证强度硬化：两级编译、驱动级判据、注入交互段、新鲜度核对、
溯源、前置校验）。**该文与本文同优先级推进**，执行者请先读它。

---

## 3. 硬编码红线（重点强调，评审时逐条过）

**这是必须独立成节强调的纪律**：工具的哲学是"OS 差异数据化"——
环境事实一律走数据面（runner.json / scaffold manifest / hints），
工具本体（porter/ 代码、skills/、静态 prompt 模板、默认值、报错
文案、测试 fixture）**零目标 OS / 零目标语言 / 零具体驱动假设**。

**禁止出现**：`make kernel`、`make run_kernel`、`Successfully booted.`、
docker 镜像名、asterinas/UDK 路径与命令形态；spi-nor 组件锚点
（skeleton claimed / mock flash / JEDEC 字节）、e1000 设备号、dm 表名
等任何具体驱动的值；"镜像级=某文件后缀"式臆断。**禁止位置**：
`porter/` 全部代码、`skills/` 全部文档、静态 prompt、默认值、报错
文案、测试 fixture（合成数据，不引用真实 OS/驱动值）。

**合法通道**：参考工作区 runner.json 里的具体值（镜像路径、驱动锚点、
命令）是**数据**，可以存在 gitignored 工作区内供参考，但绝不进工具
本体。工具只做 OS 中立的机制校验（存在性/mtime 比较/子串命中/schema
形状/执行序编排）。房内成文先例：`skills/P0-env-extract.md` 末尾
"中立声明"节。硬化篇 §5 有完整红线清单，本文收录并适用于流程改动
（例如：记账预检的报错文案不得出现 `#[ktest]` 之外的特定基质词——
marker 来自 manifest 数据面；handoff 的任务模板不得内嵌 asterinas
路径）。**本 session 的四段 gate 改动即按此纪律完成**（boot 段只消费
runner.boot 键，报错文案只引用通用语义），可作为代码侧样例。

**特别提醒一处现存违例风险**：`skills/EXP-mono-migrate.md` 与
`porter/exp/mono.py` 当前不含 OS 实值（已 grep 核实），但参考工作区
的 manifest（probe_channel.gen_rules 含 "Kthread 阶段""ostd::info!"
等 asterinas 形态描述）是数据面合法内容——执行者在改 manifest 生成
器或消费逻辑时，注意别把这类描述提升进工具本体。

---

## 4. 样例工作区导览（给执行者的读图指南）

`migrations/spinor-mono-e2e-20260908/ws-spinor-mono/` 各文件怎么读：

| 文件 | 是什么 | 怎么用 |
|---|---|---|
| `project.json` | 手工组装的 P0 工作区元数据：target_os 指向 `/tmp/opencode/spinor-mono/asterinas`、category=spi、kb_dir=spinor-empty、t3_frozen 指纹 | 看 `t3_frozen` 的自算方式（sha256 runner.json+runner.md）；`driver_name` 字段是身份层消费点 |
| `runner.json` | 三能力命令（build/boot/unit_test），与 dm-zero 冻结版逐键等价，**无 inject_device**（meta 里有 absent-by-design 声明） | 流程数据面样例；硬化篇 F2/F3 要在此补 fast_cmd/uses_artifacts/driver_init_pattern |
| `runner.md` | 手工改写的调用手册：三节环境级结论 + "注入不适用声明"（四条 QEMU 实测证据）+ 软件设备方案 | 理解"无设备轴"场景的 runner 形态 |
| `P0/inputs/hints/` | build/boot/unit_test 三份用户提示（tier①输入） | T3 语境下的最高权重输入样例 |
| `P1/*` | spi-nor-final 的 P1 交付物（12 模块 plan/deps/strategy/scope） | 模块规格来源；`P1/modules/<M>/` 的 .c/.h 是 exp-mono 的规格文件 |
| `P2/reports/scaffold_manifest.json` | 手工骨架 manifest：driver_home/integration/test_substrate/probe_channel/commit_paths/verified | **2.2 骨架数据驱动化的数据源样例**——gen_rules 已数据化描述了 crate 内纪律 |
| `exp-mono/ledger.json` | 4 模块记账 | **注意只保留每模块最后一次 run 的条目**（跨 run 覆盖，见第 7 章 Q1）；`snap_base/snap_end` 是增量守卫基线；`session_id` 是续跑入口 |
| `exp-mono/mapping-notes.md` | JIT 词典 72 条 | 2.1 的现状样例 |
| `exp-mono/parking.md` | 泊车 3 条 | **泊车 #3（契约锁死）必读** |
| `exp-mono/logs/MOD_<M>_S<n>.{prompt.md,log}` | 段级证据：S1 主段 prompt 171-217 行（看注入结构）；S2-S4 是 gate 修复段（prompt 仅 3-4 行=失败回灌）；`*_static.log` 是四段 gate 输出 | **注意跨 run 覆盖**：现存 S1 多为最后一次续跑轮（几分钟的记账段），真正的长研究段（flash 2300s）日志已被覆盖丢失——观测面缺陷实证（第 7 章 Q1） |
| `exp-mono/logs/exp_<M>_build.log`、`T3_exp_<M>_boot.log`、`exp_<M>_ut.log` | 四段 gate 的构建/启动/单测日志 | 硬化篇 3.1 的证据源；boot 日志含驱动组件初始化锚 |
| `/tmp/opencode/spinor-mono/exp_<M>_run<N>.log` | 每次 porter 运行的完整 stdout（本机 /tmp，不在工作区） | **时间轴重建的权威源**（段时刻/时长/停车原因），建议随工作区归档一份 |
| 目标树 `kernel/core/comps/spi-nor/` | 迁移产物：骨架 4 文件 + defs/caps/regs/ids(+winbond/micron_st) 7 个模块文件 | 产物质量样例；`git log --oneline` 看 6 个 commit 的粒度 |

---

## 5. 完整时间轴复盘（核心章）

时间均为 2026-09-08/09 本机时刻。每次 agent 调用标注：**prompt 构成、
运行时行为、结果/问题、知识库背景（KB/词典/泊车三行）**。
段编号 S1-S4 在**每次 porter 运行内**从 S1 重新计数（S1=主段或续接段，
S2-S4=gate 失败回灌修复段）；跨运行的同名段文件互相覆盖（见第 7 章）。

### 5.0 阶段 0：全人工准备（无 agent，09-08 白天～22:00）

| 步 | 做了什么 | 为什么 / 结果 |
|---|---|---|
| S0 | 改 `porter/exp/mono.py`：三段 gate → 四段（build 后插 `probe_boot` 阻断段，复用 runner.boot 三信号判定），GATE_DESC/报错文案/commit 文案同步；`tests/test_exp_mono.py` 增 2 用例（boot 红短路 ut / 四段全绿计数） | 用户需求"每模块验证加上启动"；测试 20/20、全量 401 绿。本改动未在工具仓提交，由用户整合为 test 分支 `1703701`（硬化篇 §1.1 同源） |
| S1 | 目标树：`git clone` asterinas 源仓 → `/tmp/opencode/spinor-mono/asterinas`，`checkout -b porter-spi-nor-mono 36ae7fe1`；用 runner.build 原样跑基线 build（rc=0，验证命令成立+预热缓存） | 与 dm-zero 先例同 baseline，可比 |
| S2 | P0 产物手工生成：runner.json（三能力，程序化 diff 证明与 dm 冻结版逐键等价）、runner.md 改写（inject 节替换为"不适用声明"+软件设备方案）、hints 三份、runbook 复制、project.json（t3_frozen 自算 sha256）、P1 整目录拷入 | dm 的 P0 产物含 dm 专属 inject 语义，spi-nor 无注入通路（spi-nor-test T3 四条实测），照抄会埋坑——这是"OS 差异数据化"的一次人工实践 |
| S3 | 骨架手工落树：crate 4 文件（Cargo.toml 最小依赖 / lib.rs 含 `#[init_component(kthread)]` 钩子+骨架日志 / probes.rs 探针宿舍 / mock_device.rs 软件 flash 桩）+ 接线 6 处（members/default-members/workspace-dep/Components.toml/core-dep/driver mod）；容器内三绿验证：build rc=0、boot 三锚日志各命中 1 次（组件日志 hvc0 拓扑可见性实证）、ktest smoke `skeleton_alive ok`；manifest 就位；**commit `cf459b5e5`** | exp-mono 前置四件套的最后一个手工件；mock 设备=无注入通路场景的"设备在场"数据面实现 |
| 介入#11 | 骨架 build 后 Cargo.lock 变脏 → 追加 commit `7658c97d3`（"lockfile after wiring"） | 不处置则后续模块的产物守卫判"越出白名单"（git 改动白名单=driver_home+commit_paths）；根因是接线改 Cargo.toml 后首次构建必更新锁文件，属构建系统副产物，应数据面声明或工具豁免（见第 7 章 Q5） |
| S4 | `PORTER_NO_AGENT=1 exp-mono --module nor-defs` 冒烟 | 零 agent 验证前置链：四件套校验、prompt 组装（443 行/预算 900s/起始标记 1=骨架 smoke）、ledger 记账 no-agent。注意该模式不落 prompt 文件（第 7 章 Q7） |

**阶段 0 知识库背景**：KB 空（kb_dir=spinor-empty，全程未变）；
词典空；泊车空。

### 5.1 nor-defs（443 行，order 首位，词汇表模块）——4 次运行

**背景注入**（S1 prompt，171 行）：EXP-mono-migrate skill 全文 +
平台事实（目标树路径/driver_home/源扩展名/登记约定 `mod {stem};`/
测试基质 `#[ktest]`）+ 模块规格（core.h 整文件）+ 依赖产物（无——
首位模块）+ **词典现文（空，"你是第一位迁移者，从零建词典"）** +
泊车现文（空）。

**知识库背景：KB 空；词典空→本模块建立；泊车空→本模块 +2 条。**

| run | 时刻 | 模型 | 过程 | 结果 |
|---|---|---|---|---|
| 1 | 22:08 | flash（裸名） | S1 段 1s 即死 | opencode 返回 `Unexpected server error`。**介入#1**：读 agent.py 确认 PORTER_MODEL 直传 opencode --model；裸跑两模型名对照实验（`zhipu-ai/glm-5.3-flash` 正常答、裸名报错）→ 定位=模型名缺 provider 前缀 |
| 2 | 22:09-22:24 | flash（带前缀） | S1 段 902s：agent 全程在读目标树做映射研究（当时会话观测其输出流：正在精读 ostd/src/error.rs 等；该段日志已被后续 run 覆盖，见第 7 章 Q1），**未写任何产物文件**即被 900s 预算杀（rc=-1） | 零产出。**介入#2**：判定 flash 在"研究+翻译一体"模式下 900s 连研究都做不完（443 行模块）；用户决策换 glm-5.2（**介入#3**：期间一次同步阻塞跑被用户中止——操作方式自此改为 nohup 后台+轮询） |
| 4 | ≈22:28-23:10 | glm-5.2（--session 续接 flash 的会话，复用其已读上下文） | S1 主段 ~855s（token 峰值 15.0 万）：研究+翻译一体——读 Linux core.h 规格与目标树，写 defs.rs（1208 行 Rust，类型展开为 C 的 2.7 倍），建词典（equivalent/adapt 逐条带证据），+2 条泊车（mtd_to_spi_nor 待 spi-drv 消解/SpiNor 平台绑定字段组合补齐——agent 自主 grep 核实 Asterinas 无 MTD 子系统）；四段 gate 首过失败→S2(44s)/S3(42s)/S4(45s) 修复轮各过→done | `seq=done` 但**声明面记账不完备停车**：20 个 migrated 单元（宏组/常量组/结构体组）未出现在 tests∪untested。**介入#4**：--session 续跑（编排器自动在续跑 prompt 注入"上次 decl_problems 须补全"） |
| 5 | 23:10-23:20 | glm-5.2（同 session） | S1 段仅 74s（补记账+重出 done JSON）→四段 gate 全绿（build 165s/boot 169s/UT ~160s） | **PASS**。commit `f96109d9`。ledger：tests=15/exempt=3/marker_delta=15 |

### 5.2 nor-caps（224 行，纯逻辑：hwcaps↔cmd 位映射）——2 次运行

**知识库背景：KB 空；词典含 defs 轮词条（caps 直接消费 SpiNorHwcaps 等）；泊车 2 条注入。**

| run | 时刻 | 模型 | 过程 | 结果 |
|---|---|---|---|---|
| 1 | 23:20-00:02 | glm-5.2 | S1 主段 ~598s（token 峰值 12.2 万）：研究+写 caps.rs（798 行）+词典追加 12 条（agent 在输出 JSON 里自述）；期间 **介入#5（诊断虚惊）**：S1 发出后 34 分钟无 S1.log 文件，疑似僵死——实为观测缺陷：opencode 子进程有约 4 分钟启动延迟（ps ELAPSED 反推 23:24:36 才起），且段日志段末才落盘；经 /proc/<pid>/fd/1 尾读确认 agent 活跃且已接近收尾。S2(6s)/S3(71s)/S4(25s) 修复轮后 done | `seq=done`，decl-mismatch 停车：仅 **1 项**（spi_nor_hwcaps2cmd 内部函数未挂）。**介入#6**：续跑 |
| 2 | 00:05-00:14 | glm-5.2 | S1 段 30s →四段 gate 全绿 | **PASS**。commit `746c1e3e`。tests=16/exempt=0 |

### 5.3 nor-regs（822 行，最大模块：SR/FSR 寄存器原语层）——3 次运行

**知识库背景：KB 空；词典含 defs+caps 词条；泊车 2 条。**

| run | 时刻 | 模型 | 过程 | 结果 |
|---|---|---|---|---|
| 1 | 01:03-01:43 | flash | S1 段 2401s 预算耗尽被杀（rc=-1）。**但非零产出**：目标树已落 regs.rs 825 行，S1 日志 88 次工具事件，被杀时正在收尾（最终 JSON 已在输出缓冲） | **介入#7**：人工检查半成品（git status + 工具事件统计）判定"值得续跑而非重跑"→ --session 续接。这个判断当前必须人做（工具在预算耗尽时只报 failed，不报产出进度） |
| 2 | 01:49-02:38 | flash（同 session） | S1 续接段（~1133s，token 峰值 23.7 万）：完成 regs.rs（最终 2179 行）+测试 43 个；S3(91s)/S4(64s) 修复轮后 done | decl-mismatch 停车：5 组 migrated 未挂 + 声明 40 测试 vs 标记增量 39。**介入#8**：续跑 |
| 3 | 02:48-03:04 | flash（同 session） | S1 段 245s（补记账）+S2 段 56s（小修）→四段 gate 全绿（build 115s/boot 106s/UT ~100s） | **PASS**。commit `cf22a292`。tests=43/exempt=0 |

### 5.4 nor-ids（438 行，三文件：manufacturers[]+winbond+micron-st）——5 次运行

**知识库背景：KB 空；词典含 defs/caps/regs 词条（ids 消费 regs 的寄存器原语结论）；泊车 2 条→本模块 +1 条（契约锁死，见 2.1）。**

| run | 时刻 | 模型 | 过程 | 结果 |
|---|---|---|---|---|
| 1 | 03:10-04:07 | flash | S1 主段 2302s（token 峰值 26.7 万，**全程最高**——三文件规格+已积累的词典与四模块已迁产物上下文）：写 ids.rs 439 + winbond.rs 628 + micron_st.rs 617 行，词典追加；S2/S3 修复轮过；**S4 段 3s 即被杀**——run_agent_seq 内 S1-S4 共享 2400s 预算，S1+S3 已耗 2400.2s，S4 一开即到限 | `seq=failed`（S4 是 UT 后修复段，差一步 done）。**介入#9**：续跑（新调用=新预算；这是预算语义的实测边界，见第 7 章 Q4） |
| 2 | ≈04:12-04:21 | flash | S1 段 47s（重出 done JSON） | decl-mismatch：13 项未挂（厂商结构/表/常量组） |
| 3 | ≈04:24-04:33 | flash | S1 段 82s | decl-mismatch：**新形态**——声明 27 测试 vs 标记增量 16（声明数>实际，agent 把表驱动测试按条目拆着声明） |
| 4 | ≈04:35-04:44 | flash | S1 段 112s | decl-mismatch：17 vs 16（收敛中）。**介入#10（诊断型）**：人工对照 ledger tests 声明与 ids.rs/winbond.rs/micron_st.rs 的实际 `#[ktest]` 函数名——**根因=done JSON 的 tests 数组有重复条目**（`read_id_spimem_matches_winbond_w25q256` 声明了 3 次、另两个各 2 次；去重后 14 ≤ 16 即可通过）。此对照完全机械可做，编排器报错只报数量差导致 agent 多绕两轮 |
| 5 | 04:44-04:55 | flash | S1 段 140s（去重后重出）→四段 gate 全绿 | **PASS**。commit `09f8278`。tests=16/exempt=13 |

### 5.5 汇总账

| 模块 | run 数 | agent 段数 | agent 秒（含耗尽轮） | 停车原因分布 | 最终墙钟（PASS 轮） |
|---|---|---|---|---|---|
| nor-defs | 4 次发起（1 环境败/1 中止后换模型） | 5 | 902(全损)+986+74=1962 | 模型名/预算耗尽/decl×1 | 568s |
| nor-caps | 2 | 5 | 700+30=730 | decl×1 | 526s |
| nor-regs | 3 | 7 | 2400(耗尽,产出经续跑兑现)+1288+291=3979 | 预算耗尽/decl×1 | 948s |
| nor-ids | 5 | 8 | 2400(耗尽,产出经续跑兑现)+47+82+112+140=2781 | 预算耗尽/decl×4 | 473s |
| **计** | **15 次发起** | **25** | **9452** | **11 处介入（6 记账/2 预算/1 环境/1 诊断/1 流程）** | — |

四段 gate 表现：**boot 段（本次改造对象）零误杀零漏杀**——工作区
留存的全部 `T3_exp_*_boot.log` 均 rc=0（99-190s/轮），无一例"驱动没
起来而内核正常"的假绿（本轮驱动锚以人工 grep 核验，机械化归硬化篇
F3）；gate 的失败全部发生在 build/UT 段（触发 S2-S4 修复轮，按设计
回灌 session 内修复）；记账失败（decl）发生在 gate 之后，由
--session 续跑收敛。

---

## 6. 数据汇总（供实验对照与回归基线）

- **进度**：4/12 模块（order：defs→caps→regs→ids→qe→spimem→io→erase→
  setup→params→scan→spi-drv）；剩余 8 模块共 1894 行。
- **产物**：Rust 5869 行（defs 1208/caps 798/regs 2179/ids 439+winbond
  628+micron_st 617）+骨架 4 文件；6 commit（骨架 2 + 模块 4）。
- **测试**：91 `#[ktest]`（1+15+16+43+16）+16 豁免（3+13）。
  豁免率 16/107≈15%。
- **词典**：72 条平铺（无模块分节），verdict 分布以 equivalent/adapt
  为主；证据全部指向目标树源码行。
- **泊车**：3 条（defs 轮 2 条：mtd_to_spi_nor、SpiNor 平台绑定字段；
  ids 轮 1 条：set_4byte_addr_mode 钩子契约锁死——波及后继 qe 模块）。
- **token**：S1 会话峰值 defs 15.0 万 / caps 12.2 万 / regs 23.7 万 /
  ids 26.7 万（flash 长会话随词典+产物积累单调上涨——任务拆分的
  动机数据）。
- **模型对照**（同流程同目标树）：glm-5.2 研究段 ~600-855s/模块、
  记账 1 轮收敛；glm-5.3-flash 研究段 2300-2400s/模块（≈3 倍）、
  记账最多 4 轮（重复声明 bug 为 5.2 未见形态）。flash 便宜但综合
  成本（含续跑轮次与人工盯守）未必占优——供选型参考。

---

## 7. 已知问题清单（本 session 实测发现，按影响排序）

| # | 问题 | 实证 | 建议方向 |
|---|---|---|---|
| Q1 | **段文件跨 run 覆盖**：每次 run_agent_seq 从 S1 重新编号，同模块多次运行的同名段文件互相覆盖；ledger 每模块条目同样只留最后一次 run | defs/regs/ids 的 run1 长研究段 S1 日志已丢失（现 S1.log 是最后续跑轮，仅 1-9 step）；run1 的 agent_sec/decl_problems 只能从 /tmp run 日志重建 | 证据按 execution 分目录（handoff 子系统的 provider 私存即此设计，`docs/sub-systems/handoff.md`）；ledger 改为 run 数组或追加历史 |
| Q2 | **decl-mismatch 报错信息量不足**：只报数量差与未挂清单，不做重名检测/名称对照 | ids 轮多绕 2 轮（27→17→16），根因（重复声明）由人工 diff 发现 | 报错机械化增强：tests 数组重名检测 + 声明名 vs 树上 marker 函数名 diff；skill 增补一句"tests 不得重名、须与标记一一对应"。零风险高收益 |
| Q3 | **记账停车占介入的 6/11**：done JSON 六字段对 agent 是系统性难点（4/4 模块全部经历） | 第 5 章各模块 | 提交前预检（2.2）：agent 出 done 前由工具跑同一核对并回灌 session 内修复 |
| Q4 | **预算语义**：同一次 run_agent_seq 内 S1-S4 共享预算，修复段可被主段吃光的预算在 3s 内杀掉（ids S4）；跨调用则重置 | 5.4 run1 | 预算按段设下限保护（修复段如 max(剩余,300s)）；或预算耗尽即关口化（进度报告+人选续/换/停） |
| Q5 | **构建系统副产物越界**：接线后首次构建更新 Cargo.lock，不在 commit_paths 白名单即触发产物守卫越界 | 介入#11 | 数据面声明"构建副产物豁免清单"（manifest 增字段），守卫消费；不要在代码里硬编码锁文件名 |
| Q6 | **agent 段日志段末落盘**：长段（2300s）期间无文件级进度，只能 /proc/fd 或 ps 推断 | 介入#5 虚惊 | 流式落盘或周期心跳文件（观测面，配合 nohup 轮询使用） |
| Q7 | **NO_AGENT 冒烟不落 prompt 文件**：只验证链路不验证注入内容 | S4 冒烟后 prompt 文件不存在 | NO_AGENT 模式也落 prompt.md（零成本） |
| Q8 | opencode 新会话启动延迟 ~4min（caps S1 实测 23:20:40 发出→23:24:36 子进程起），无日志解释 | 6.2 | 记录在案；执行者可测 opencode CLI 冷启动/网络归因；对 handoff 拆分方案的 session 数量成本要计入 |
| Q9 | `t3_frozen` 指纹无运行时消费（写而不校） | 硬化篇 §3.2 附注同源 | 归硬化篇管辖，此处只交叉引用 |

---

## 8. 建议的执行顺序（非约束）

1. **先读两份文档 + 样例工作区**（本文 + 硬化篇 + ws-spinor-mono 导览）；
2. 零风险速赢：Q2 报错增强 + Q7（半天级，立即降低记账介入密度）；
3. 与用户讨论并定：覆盖率口径（2.4）、豁免审计、拆分切口（建议
   研究/翻译分离起步）；
4. 中期：记账预检（Q3）、预算段保护（Q4）、停车关口化（2.3 交互式
   退化）、硬化篇 F1-F6；
5. 长期：词典/编译知识进 kb（2.1）、契约登记与回修窗口、骨架数据
   驱动化、任务拆分与 handoff 接入（2.2）；
6. 全程守住第 3 章红线；改完跑 `pytest tests/`（当前全量 401 绿）
   + 用本工作区数据做对照回归。

## 参考索引

| 对象 | 位置 |
|---|---|
| 姊妹篇（验证强度硬化） | `docs/research/exp-mono-gate-hardening-handoff.md` |
| 四段 gate 实现 | `porter/exp/mono.py:297-331`（`_make_gate`） |
| 声明面核对（报错增强落点） | `porter/exp/mono.py:56-107`（`_declaration_problems`） |
| 词典/泊车注入 | `porter/exp/mono.py:327-394`（`_module_prompt`） |
| 续跑基线（--session 语义） | `porter/exp/mono.py:496-519` |
| 人工介入协议 | `docs/sub-systems/human-intervention.md`（两种介入方式/关口/answers） |
| handoff 协议 | `docs/sub-systems/handoff.md`（TaskSpec/execution/provider 私存） |
| 知识库协议 | `docs/sub-systems/knowledge.md`（起草/晋升/固定+随机/目录自取） |
| P0 关口-应答先例 | AGENTS_2.md §4 "T3 v2 重构"节（2026-09-06） |
| 样例工作区 | `migrations/spinor-mono-e2e-20260908/ws-spinor-mono/` |
| 本 session 运行日志（时间轴权威源） | `/tmp/opencode/spinor-mono/exp_*.log`（建议随工作区归档） |
| 模型对照与成本数据 | 本文 §5.5/§6；ledger.json |
