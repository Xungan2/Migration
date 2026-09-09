# exp-mono 验证强度硬化——交接文档

写作日期：2026-09-09。执行者：负责本轮 exp-mono 修复的同学。
范围：**工具侧（driver_migration_tool）框架改造**。不含任何目标 OS 环境的修复、不含任何迁移重跑。

本文所有代码结论核实于 test 分支 `07c0bbd`；exp-mono 全系列提交只存在于 test 分支
（main 上一个都没有），**请在 test 分支上工作**。诊断证据来自本机
`migrations/spinor-mono-e2e-20260908/ws-spinor-mono/`（gitignored，但文件在本机磁盘上，
下文路径均可直接查看）。

---

## 1. 问题背景

### 1.1 exp-mono 与 gate 演进

exp-mono 是单体模块迁移实验子命令（`porter/exp/mono.py`）：一个迁移者 agent 一次迁一个
模块，每模块经一个复合静态 gate 验证，四段全绿才 pass 并 commit 目标树。gate 于
2026-09-08 由三段（产物守卫 + 编译 + 单测）改造为四段（产物守卫 + 编译 + **启动** +
单测），提交 `1703701`。gate 的执行逻辑：`porter/exp/mono.py:308-326`（`_make_gate`），
其中启动段无条件串行于编译之后、单测之前（mono.py:316-321）。

### 1.2 UDK 事故（动机）

用户在另一台机器的 UDK（HarmonyOS）环境跑了一次 spi-nor 迁移，结果不符合要求：

- **实际发生的**：每个模块只通过了**模块级编译**和**单元测试**；镜像级编译和启动都没跑。
- **UDK 环境特性**（用户原述）：
  - 编译分两级：**模块级**（快）与**镜像级**（慢，但只有镜像级能编出可启动镜像）；
  - 启动 = 用**当前代码新做镜像级编译**得到的镜像 + QEMU 启动；
  - 那边的 runner.json 是当地 agent 生成并测试过的。

直接诱因无法从本机确证（可能是那台机器的工具版本早于 `1703701` 的三段代码，也可能
是 runner.json 把模块级命令注册进了 build 槽位——若 runner 缺 boot 键，四段代码会在
gate 中途 KeyError，`porter/common/agent.py:744-751` 将其按静态段失败处理，模块不可能
pass，故"pass 了但没启动"与"四段代码 + 缺 boot 键"互斥）。**但归因不是本任务的重点**：
两种诱因框架都无法预防、无法发现、事后无法追责——这是要修的东西。

### 1.3 根本定性

exp-mono 的验证强度是**口头契约，不是机械保证**。runner.json 写什么就跑什么、缺什么
就少什么，gate 组成与各段结果不入账，验证产物（ledger/report）无法回答"这个 pass 是
在什么强度下拿的"。

---

## 2. 任务目标（= 本任务的需求，用户原意收录）

**每模块迁移的 pass 判据**：

1. **编译（两级递进）**：先用**模块级编译**作为快速测试条件——大部分编译错误在这一层
   解决、便宜地反馈给 agent；模块级通过之后再测**镜像级编译**（必过，产出含当前代码
   的可启动镜像）。这样减少镜像级编译的调用次数。
2. **单元测试**。
3. **启动**：用当前代码新镜像级编译出来的镜像 + QEMU 启动，且包含两层含义：
   - **驱动自启动**：驱动框架/组件初始化成功（有判据可验，不是"内核起来了就行"）；
   - **设备注入后驱动和设备的最简（但是全面的）交互**：注入设备后驱动探测到设备的
     特征命中、驱动初始化失败特征不命中。

**硬性要求**：这些约束必须由框架**机械保证**（schema 校验、执行序、判据命中、产物
核对），并在验证产物中**可追溯**——不允许任何一项因数据面缺省或版本偏差而静默降级。

---

## 3. 诊断（基于 `migrations/spinor-mono-e2e-20260908/ws-spinor-mono/` 实证）

### 3.1 本地实跑健康项（先说清楚：本地没有"没启动/非镜像级"症状）

| 检查项 | 结果 | 证据 |
|---|---|---|
| 镜像级编译 | 真跑了 | `exp-mono/logs/exp_<模块>_build.log` ×4：`make kernel` 全量内核镜像构建输出 |
| 启动 | 真跑了 | `exp-mono/logs/T3_exp_<模块>_boot.log` ×4：真 QEMU 启动（UEFI → OS → 驱动组件初始化日志俱在） |
| 四段 gate 生效 | 是 | `exp-mono/logs/MOD_nor-ids_S1_static.log` 含"四段全绿：产物守卫通过；构建成功；启动成功（含全部已迁移代码）；单测通过…" |

即：四段 gate 代码本身在本地工作正常。问题在**判据面与保证面**——换一个环境（两级
构建、agent 生成的 runner、旧版本工具副本）就退化，而框架毫无抵抗力和察觉力。

### 3.2 五个结构性缺口（各带证据）

| # | 缺口 | 证据与细节 |
|---|---|---|
| G1 | **boot 判据只有内核级三信号** | runner 的 `boot.success_pattern` = `"Successfully booted."`（内核级）；驱动组件初始化锚点未进 gate 判据——当时靠**人工 grep** boot 日志核验。若驱动根本没起来而内核正常，gate 照样绿。 |
| G2 | **注入交互段不存在** | mono.py 从不调用 `probe_boot_with_device`（`porter/env/probe.py:213-248` 现成实现，含驱动级判定 `_judge_driver` probe.py:169-210）；runner 无 `inject_device` 时静默略过，无任何声明。参考工作区 runner.json meta 注明 "absent by design"（无可注入设备模型，用 crate 内 mock 替代）——这是合法的数据面决策，但 gate 层面应当显式入账而非静默。 |
| G3 | **验证强度无溯源** | `exp-mono/ledger.json` 四模块 pass 条目只有 rounds/agent_sec/files/tests 等，**无 gate 组成、无各段结果、无工具版本**；`report.md` 无验证强度节。pass 跑没跑 boot 只能翻 logs/，日志一清就死无对证。 |
| G4 | **build↔boot 因果无约束** | runner schema 只有一个 build 命令槽（`porter/env/probe.py:77` 取 `runner["build"]["cmd"]` 原样执行）；框架不知道 boot 启动的是哪个文件、不知道它是否由 build 产出、不核对它是否含当前代码。UDK 两级构建从这里穿透：模块级命令注册进 build 槽，镜像永远不重建。 |
| G5 | **前置校验缺失** | `run_exp_mono`（mono.py:660-676）只查四个文件存在，不查 runner.json 有没有 `build`/`boot`/`unit_test` 节及必需子键；缺键要到 gate 中途才 KeyError（报错不透明）；退化命令（不真启动但回显特征串的 boot.cmd）可一路绿灯。 |

另附一个顺带发现（可另立事项）：`project.json["t3_frozen"]` 冻结指纹只在
`porter/env/extract.py:962` 写入，全仓无运行时消费方——"修改即关口报错"目前只是注释
里的愿景。

### 3.3 缺口与事故的对应关系

- UDK"镜像级编译没跑" → G4（无产物声明与新鲜度核对）+ G5（无前置校验）；
- UDK"启动没跑（或跑了旧镜像）却 pass" → G4 + G5 + 版本偏差不可见（G3）；
- "驱动自启动 / 注入交互"在任何环境都未被 gate 断言 → G1 + G2；
- 事后归因困难、需要人翻日志 → G3。

---

## 4. 修复计划建议

字段命名均为**建议**，执行者可在评审后调整，但语义与强制等级应保留。

### F1 前置能力校验 + gate 组成横幅

`run_exp_mono` 读入 runner 后立即逐节校验，缺项逐条列出、rc 2 拒跑：

- `build.cmd`（完整层，必填）、`build.timeout_full_sec`；`build.fast_cmd`（快速层，可选，
  见 F2）；
- `boot.cmd` / `boot.success_pattern` / `boot.panic_pattern` / `boot.uses_artifacts`（F2）
  / `boot.driver_init_pattern`（F3）；
- `unit_test.cmd` 或 `unit_test.driver_scope_cmd`；
- `inject_device` 可选；存在时须有非空 `example_args`。

开跑前打印**本次验证强度横幅**：gate 组成（各段有无，含注入轮 present/absent）+
工具 commit 短哈希。让操作者在启动时刻就看到契约，而不是事后翻日志。

### F2 两级编译支持 + 镜像新鲜度

- **schema**：`build.fast_cmd`（可选）= 比完整构建便宜的子集编译（UDK 语境的"模块级"）；
  `build.cmd`（必填）= 完整构建，**必须产出 boot 所耗产物**（UDK 语境的"镜像级"）。
  单级构建环境不填 fast_cmd，行为不变——两层是可选优化，不是必配。
- **gate ②段执行序**：fast_cmd（若有）失败 → 以快速层错误短路回 agent（不烧完整层
  时间）；通过 → 跑完整层 cmd。这正是"大部分编译错误在模块级解决"的落地。
- **产物声明**：`boot.uses_artifacts`（必填）= boot 实际启动的产物路径列表（相对目标
  树根或绝对路径）。
- **新鲜度核对**（gate ③段 boot 成功后）：每个产物**存在** 且
  `mtime(产物) ≥ max(mtime(driver_home ∪ 接线文件))`。违反 → boot 段 FAIL，报错用
  通用语义（"boot 所耗产物比已迁移代码旧——build.cmd 必须产出启动所耗产物，或
  boot.cmd 自含完整构建"）。语义 = **"启动的镜像必须含当前代码"**，不拘泥它由哪一段
  产出（boot.cmd 自含重建的形态天然通过）。模块级构建 + 旧镜像这一族失败模式（UDK
  实际发生的）被机械堵死。
- 终局 `_terminal` 的 boot 同样核对。

### F3 驱动级判据接入

- **驱动自启动**：`boot.driver_init_pattern`（必填）= 驱动框架/组件初始化成功的日志
  逐字子串（数据面从目标树驱动源码的打印语句核实）。gate ③段在内核三信号之外追加：
  该特征必须命中，否则 boot 段 FAIL 并注明"驱动未自启动"。可选配
  `driver_init_fail_pattern`（命中即 FAIL）。
- **注入交互**：`runner["inject_device"]` 存在时，gate 在裸 boot 之后追加
  `probe_boot_with_device(..., check_driver=True)`（阻断）：`driver_success_pattern`
  命中 ∧ `driver_fail_pattern` 不命中。特征的**区分度**（注入轮命中 ∧ 裸轮不命中）
  由 P0/T3 冻结时差分校验负责，gate 只消费已验特征——不在 gate 里重复差分（省一次
  boot）。缺 inject_device → 记 `inject=unconfigured`，**显式入账，永不静默**。

### F4 验证强度溯源

- `_make_gate` 改结构化收集：闭包把每段 `{fast_build/build/boot/inject/ut: {ok, detail}}`
  写入外带 dict；模块 pass 后写入 `entry["gate"]`（含 describe、各段明细、
  `tool_commit`——运行时 `git rev-parse --short HEAD` 取工具仓短哈希，best-effort）。
- `report.md` 增"验证强度"节：每模块各段结果行 + 头部声明本次 gate 组成（含
  inject=unconfigured 这类显式降级）。
- 跳过已 pass 模块（mono.py:714）时：若旧条目无 gate 戳或戳与当前不符 → 醒目警告
  （"该 pass 产自不同验证强度/工具版本"）。

### F5 文档与数据面同步

- `skills/P0-env-extract.md`：boot/build schema 增补新字段（`fast_cmd`、
  `uses_artifacts`、`driver_init_pattern`）及语义要求（"build.cmd 必须产出 boot 所耗
  产物"）——T3 生成的 runner 从源头带齐。
- `skills/EXP-mono-migrate.md`：gate 描述同步（两级编译 + 驱动判据 + 注入轮）。
- `README.md` exp-mono 节：新字段、两级构建场景说明、溯源字段含义。
- 本地参考工作区 `migrations/spinor-mono-e2e-20260908/ws-spinor-mono/runner.json` 补
  新字段值（数据面更新，非工具硬编码，见下节红线）：`uses_artifacts` = `make run_kernel`
  实际启动的镜像路径（从目标树 Makefile 核实）、`driver_init_pattern` = 驱动组件初始化
  锚点（从 boot 日志/驱动源码核实逐字子串）。

### F6 测试

扩展 `tests/test_exp_mono.py`（现有四段 gate 测试骨架可复用），新增用例：

1. 前置校验 rc 2 三态（缺 boot 节 / 缺 uses_artifacts / 缺 unit_test）；
2. 两级编译：fast_cmd 失败短路（完整层不被调用）、fast_cmd 缺省时行为不变；
3. 新鲜度两例（产物比代码旧 → FAIL；产物新 → PASS）；
4. 驱动自启动判据 MISS → boot 段 FAIL；
5. 注入轮两例（配置 → probe_boot_with_device 被调且阻断；缺省 → unconfigured 入账）；
6. 溯源两例（ledger gate 字段、report 验证强度节）；
7. 陈旧强度跳过警告一例。

全部用合成 fixture。跑全量 `pytest tests/` 回归。

---

## 5. 硬编码红线（重点强调，评审时逐条过）

**定义**：在代码、skill、静态 prompt 里写只能在特定环境跑起来的内容。这会毁掉工具的
通用性——工具的哲学是"OS 差异数据化"，环境事实一律走数据面（runner.json 等），工具
本体零目标 OS / 目标语言假设（mono.py 模块 docstring 开头就是这么声明的）。

**禁止出现的位置**：`porter/` 全部代码、`skills/` 全部文档、静态 prompt 模板、默认值、
报错文案、测试 fixture。

**禁止内容清单**（示例性，不限于此）：

- **OS 侧**：`make kernel` / `make run_kernel` / `qemu` 等具体命令；`"Successfully
  booted."` 等具体特征串；asterinas 路径 / UDK 命令形态 / docker 镜像名；对"镜像级 =
  某种特定文件后缀/路径"的臆断。
- **驱动侧**：spi-nor 组件锚点（如 skeleton claimed / mock flash 之类）、JEDEC 字节、
  8086 设备号、dm 表名等任何具体驱动的值。

**环境事实的唯一合法通道 = 数据面**：

- runner.json（及其新字段）的值由 P0/T3 agent 从目标树/资料探明，或由人工撰写——
  工具只消费声明；
- 参考工作区 runner.json 里的具体值（镜像路径、驱动锚点串）是**数据**，可以存在
  （gitignored 工作区内），但绝不进工具代码/skills/prompt；
- 工具只做 **OS 中立的机制校验**：路径存在性/可解析性、mtime 数值比较、子串命中、
  schema 形状检查、执行序编排；
- 报错文案只描述通用语义。"两级构建（快速层/完整层）"可作为**场景举例**出现在用户
  文档里，但代码不得对任何具体两级构建体系做特判；
- 字段命名用通用语义（`fast_cmd`/`uses_artifacts`/`driver_init_pattern`），不要把
  "模块级/镜像级"这类特定环境术语写进 schema 语义；
- 单测一律合成 fixture（假树、假命令、假特征串），不引用任何真实 OS/驱动值。

**房内成文先例**：`skills/P0-env-extract.md` 末尾"中立声明"节——skill 刻意不含任何
目标 OS 的实值样例。本任务的一切改动延续这条线。

---

## 6. 验证方式

1. 单测：`pytest tests/`（当前全量 401 绿，改完应保持全绿 + 新增用例）；
2. 数据面冒烟：本地参考工作区 runner.json 补齐新字段后，对已 pass 模块跑一次 gate
   侧核对（不重迁、不派 agent，只验证新判据在真实数据上工作：freshness PASS、
   driver_init_pattern 命中）；
3. 溯源自查：跑完后检查 ledger/report 出现 gate 组成与各段结果。

## 7. 边界与非目标

- 不修 UDK 环境、不重跑任何迁移、不管 UDK 那边已产出的结果如何处置；
- 残余风险如实声明：伪造特征串的退化命令（Goodhart 面）单靠机制无法根除——缓解 =
  F4 溯源让这类 pass 在产物上无处遁形 + T3 冻结人审；这与 TODO #12（注入判定
  Goodhart 史）同哲学；
- `t3_frozen` 无运行时校验是独立问题，本任务只记录不修。

---

## 参考索引

| 对象 | 位置 |
|---|---|
| 四段 gate 组装 | `porter/exp/mono.py:297-331`（`_make_gate`） |
| exp-mono 入口（前置校验落点） | `porter/exp/mono.py:656-682`（`run_exp_mono`） |
| 已 pass 跳过逻辑 | `porter/exp/mono.py:713-716` |
| gate 异常按失败处理 | `porter/common/agent.py:744-751` |
| build 执行器 | `porter/env/probe.py:77-98`（`probe_build`） |
| boot 执行器（内核三信号） | `porter/env/probe.py:110-159`（`_boot_once`） |
| 驱动级判定（现成，未被 exp-mono 消费） | `porter/env/probe.py:169-210`（`_judge_driver`） |
| 设备注入 boot（现成，未被 exp-mono 消费） | `porter/env/probe.py:213-248`（`probe_boot_with_device`） |
| runner schema 与"完整构建"语义 | `skills/P0-env-extract.md:92-95, 144-177` |
| t3_frozen 写入点 | `porter/env/extract.py:959-967` |
| 四段 gate 提交 | test 分支 `1703701`（exp-mono 系列仅存于 test 分支；当前基线 `07c0bbd`） |
| 参考工作区 | `migrations/spinor-mono-e2e-20260908/ws-spinor-mono/`（gitignored；runner.json、exp-mono/ledger.json、exp-mono/logs/、exp-mono/report.md） |
| exp-mono 现有测试 | `tests/test_exp_mono.py`（586 行，含四段 gate 用例） |
