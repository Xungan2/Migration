# driver_migration_tool

把 Linux 内核驱动（C）**迁移（安全重写）**为任意目标 OS 原生驱动（现状：
安全 Rust 组件）的自动化流水线工具。

形态：确定性 Python 编排（零第三方依赖）× 判断性 opencode agent 调用
（17 个 skill）× 机器复核（双信号判定：退出码 + 日志特征）。人工介入被
压缩到特定环节的标准接口（决策队列 / 文档审核 / 知识审核）。

> 首个实验（e1000→Asterinas，18 轮全流程）已完成并验证了方法论可行性。
> [`tool-audit-report.md`](./tool-audit-report.md) 是完整工具开发报告——
> 记录了这个工具的**所有开发细节**（历史里程碑/架构/文件级实现地图/
> 已知限制/未做项），0 上下文接手时先读它。子系统/模块规范见
> [`docs/`](./docs/)（sub-systems/ + modules/ 两层）。

---

## 依赖与搭建

### 必备依赖

| 依赖 | 用途 | 说明 |
|---|---|---|
| **Python ≥ 3.10** | 运行 porter（编排器） | 零第三方 pip 依赖（纯标准库）；实测 3.13 |
| **opencode CLI**（PATH） | 所有 agent 调用 | `opencode run --auto` 非交互模式；安装见 [opencode.ai](https://opencode.ai) |
| **bash** | 命令执行器 | runner.json 内命令经 `bash -c` 执行 |
| **git** | 目标树 VCS 基线 | P7 baseline diff；P0 记录目标树 commit/branch/dirty |

### 运行时依赖（由 P0 探测写入 runner.json，工具本体不直接调用）

- **docker** / **QEMU** 等：构建/启动/测试命令由 P0 agent 从开发者资料+
  目标 OS 源码树提取，写进工作区 `runner.json`，后续相位数据驱动消费。
  工具本体不硬编码任何 docker/QEMU 命令。
- **目标 OS 源码树**（可写）：P2b 骨架 + P4 迁移会写入目标树。
- **Linux 驱动源码树**（只读）：P1 切分 / P2a spine 提取的输入。

### 环境变量

| 变量 | 默认 | 作用 |
|---|---|---|
| `PORTER_MODEL` | `zhipu-ai/glm-5.2` | agent 模型 |
| `PORTER_LOG_LEVEL` | `info` | console 行级别阈值（debug/info/warn/error） |
| `PORTER_NO_AGENT` | 未设 | `=1` 关 agent 兜底（测试/守护闸门；错误处理降级档） |
| `PORTER_SELF_DIAGNOSIS` | 未设 | `=1` 强制开错误处理求解循环（测试惯例） |
| `PORTER_LIVE_AGENT_TEST` | 未设 | `=1` 启用 agent 模块 live 冒烟测试（真实模型调用：session 续接暗号回溯 + 指针 e2e） |
| `PORTER_TARGET_OS_ROOT` | 自动注入 | 目标树绝对路径，runner.cmd 中以 `${...}` 引用 |

---

## 总体流程框架

```
P0  环境门禁 → P1 拆分策略 → P2 引导映射+骨架 → P3-P5 垂直循环(×N 模块) → P6 系统验收 → P7 终态报告
```

全程**幂等断点重入**：产物存在即跳过，失败可重跑同命令从断点继续。
退出码约定：`0` 成功 / `1` 失败 / `2` 前置缺失 / `3` 需人工（= 存在 open
阻塞关口，把答案写入 `answers.md` 后重跑即续）。

### 各阶段概览

| 阶段 | 目标 | 解决什么问题 | 核心产物 |
|---|---|---|---|
| **P0** | 开发能力硬门禁 | 目标 OS 能否构建/启动/挂设备？单测机制是什么？ | `project.json` / `runner.json` / `P0/reports/` |
| **P1** | 拆分策略+模块划分 | 驱动源码怎么切分成可逐步迁移的模块？依赖序是什么？ | `P1/strategy.md` / `P1/modules/deps.json` |
| **P2** | 引导映射+全局骨架 | Linux API→目标 OS 映射表；零功能骨架入住目标树 | `P2/mapping.json` / 目标树 crate / `P2/reports/` |
| **P3-P5** | 垂直循环 ×N | 逐模块：分析→生产→验收 | `P3|P4|P5/<M>/reports/` / `loop_state.json` |
| **P6** | 系统验收 | 全局收口：聚合健康 / 执行重测 / L4 定稿 / 缺陷账本 | `P6/reports/health.json` / `defects.json` |
| **P7** | 终态报告 | 全产物聚合 + baseline diff + 补丁台账 | `P7/reports/final_report.json/.md` |

### 阶段细节

#### P0 环境门禁

- **T1 输入解析**（脚本）：校验驱动/目标树/资料，创建工作区+`project.json`。
- **T2 类别识别**（agent）：识别 pci/net/... 标签（选模板开关；`--category`
  人工覆盖；不可判定回落通用模板+警告）。
- **T3 环境提取**（agent ×3 轮 × 探测交织）：从资料+目标树提取 `runner.json`
  （build/boot/unit_test/inject_device 四项可执行能力）。探测为金标准——
  三项双信号全 PASS 才过。3 轮未成 → exit 3（answers.md → R4 答案整合轮）。
- **T5 门禁**（脚本）：project.json 完整 + runner 校验 + T3 三项全 PASS +
  unit_test 烟测。

#### P1 拆分策略

- **p1-strategy**（agent）：读 Linux 源码产出自由 Markdown 策略分析
  `strategy.md`，**人工审阅**（CP1 指纹绑定）。
- **p1-divide**（agent × 按文件 + 脚本）：索引预建→按文件 agent 分配→
  机械展开→物理抽取 → `P1/modules/<name>/`。
- **p1-resolve**（agent × ≤3 轮 + 脚本）：符号扫描→依赖图→环检测→agent
  搬运循环（守恒校验）→拓扑序 `deps.json`（循环输入）。3 轮败 → exit 3。

#### P2 引导映射+骨架

- **2a 引导映射**（agent 分批 + 机器校验）：主轴外部 API 提取→按域分批
  agent 映射→9 字段校验 + evidence 路径真实存在→增量合并 `mapping.json`。
- **2b 全局骨架**（脚本，目标 OS 专属模板）：在目标树新建 crate（no_std +
  deny(unsafe_code)）+ 空 probe + 探针宿舍 + ktest 位 + 栈接线桩。零驱动功能。
- **2c 探针预生成**（agent + 探测）：风险主张前置验证，住骨架 `probes.rs`
  （每次启动重跑=回归哨网）。P3 探针步骤退化为补新。
- **验收**：runner 双信号 + 组件日志特征 + 无 PROBE FAIL。

#### P3-P5 垂直循环（×N 模块，拓扑序）

按 `deps.json` 拓扑序逐模块走 P3→P4→P5，`loop` 命令自动推进：

- **P3(M) 分析**（agent + 脚本）：使用面四分类→增量映射→gap 四策略处置
  （bypass/fill/register-fill/human）→判据草案→探针补新。gap human → exit 3。
- **P4(M) 生产**（agent + 脚本）：fill 统一（平台补齐）→切片迁移（≤900 行/片，
  映射作数据注入只翻译不研究）→轮末快速冒烟（compile+boot 防毒化）。
  blocked 立即停；同签名连发 2 次=零进展早退。
- **P5(M) 验收**（脚本 + agent 补探）：L1 build / L2 boot 双信号 / L0 ktest
  同场 / L3 qemu.log regex + 累积回归 + deferred 登记/清偿。失败走求解循环
  挂载①；耗尽 → `p5.unsolved.<M>` 关口。deferred 无法清偿 → exit 3。

#### P6 系统验收

- **聚合模式**（默认，零重测）：汇总各模块 acceptance + deferred + 判据状态
  + defects → `health.json/.md`。
- **--draft-l4**：从 deferred 系统判据 + P3 e2e 材料生成 L4 草案。
- **--finalize-l4**：L4 定稿门（CP3 审批，指纹绑定；config 配 agent/human）。
- **--execute [--l4]**：一轮 build + SLIRP boot + ktest → 全判据重判 + deferred
  清偿。失败走求解循环挂载②；耗尽 → `p6.unsolved` 关口。
- **--defect-diagnose ID**：单缺陷求解循环挂载③ → 四字段闭账 + CP4 债；
  耗尽 → `d1.unsolved.<did>` 关口。
- **defects 账本**：`--defect-add/close(四字段强制)/park/list`。

#### P7 终态报告

- **聚合**：P0→P6 全产物 + git baseline diff + crate/映射统计 + 补丁台账 →
  `final_report.json/.md`（数据驱动骨架，"结论与去向"节留人工撰写）。
- **CP4 缺陷闭账批审** + **CP5 知识沉淀提醒**（非阻塞）。
- **补丁台账**：`--patch-register` / `--patch-status`（planned|proposed|closed）。

---

## 子系统概览

### log 子系统（统一观测框架）

**目标**：把"翻目录找日志"变成"查一条结构化时间线"——工具跑的每一步
（相位推进/agent 调用/命令执行/判定结论/人工介入）都进同一份 append-only
事件流（`events.jsonl`）。纯静态实现（零 agent），永不抛异常。

**规范**：[`docs/sub-systems/log.md`](./docs/sub-systems/log.md)（五类文件格式/kind 注册表/命名/体积纪律/兼容策略）。

### 知识子系统（kb）

**目标**：把"每次迁移从零摸索"变成"站在上次迁移的肩膀上"——经验自动收集
成草稿、人工把关晋升、下次 agent 自己查着用。分区（base=工具随附、
temp+per-migration=工作区内，见「知识库选择」节）
× 六域（maps/gaps/runbook/splits/pitfalls/failures）。

**规范**：[`docs/sub-systems/knowledge.md`](./docs/sub-systems/knowledge.md)（目录模型/域注册表/薄 INDEX/
固定收成/随机知识探查/检索协议/晋升协议/CP5 审核）。

### 人工介入子系统（gates）

**目标**：把"人盯着工具"变成"工具排队等人"——工具能自己推进的就推进，
拿不准的先记下来攒着事后批量找你确认；实在推进不下去才停下。两车道
（panic 异常停车 / checkpoint 计划内批审）+ 三级分流（rules→agent→human）。

**规范**：[`docs/sub-systems/human-intervention.md`](./docs/sub-systems/human-intervention.md)（账本协议/
答案协议/应用协议/检查点协议/panic 协议/路由协议/关口 ID 目录）。

---

## 模块概览

### 错误处理模块（errorloop）

**目标**：失败的归责与求解——拿到失败信息，判定"这是谁的错"，并尝试解决
或给出正确处置。知识辅助的 agent 求解循环（≤3 轮 + 同签名早退 + 双信号复验），
三挂载（p5/p6/d1）+ unsolved 关口（attempts 在挂载点退役）。

**规范**：[`docs/modules/error-handling.md`](./docs/modules/error-handling.md)（核心流程/动作词表/
挂载点/failures 知识面/观测面事件族/实现地图）。

### git 管理模块（vcs）

**目标**：全程 commit 管理两个 repo（目标 OS 树 + 迁移工作区），记录
"每个 step 干了什么"，支撑迁移结果分析与优化；git bundle 跨机器可移植。
P0 登记目标树并行仓 baseline + 建 porter 分支（工作区仓同分支名）；
agent 调用前后隔离 commit（pre-agent/agent 成对，`diff` 即该次调用的
ws 产物）；P7 输出 commit 链并自动导出。两仓 commit 流互相独立
（目标 OS 只按既定点提交）；全程 best-effort，git 失败只告警不阻塞。

**规范**：[`docs/modules/vcs.md`](./docs/modules/vcs.md)（两仓模型与 commit 点地图/分支管理/
commit 语义/台账与 P7 链/bundle 可移植/配置/实现地图）。

### agent 调用模块（agent）

**目标**：非交互 agent 调用的增强接口——`run_agent_seq`（split_long_op：
agent 段 × N + 外部静态段，编译/测试/启动等长操作时间不吃 agent 预算；
段间 opencode `--session` 会话续接，无信息损失，解析失败兜底交互式
transcript；静态结果指针化——完整输出落盘随 vcs 隔离 commit 入库，
消息只给 verdict + 文件路径）+ `run_agent_structured`（单发 + 字段
校验 + 反馈重试）。防打转不设轮数上限：同签名早退 + 上下文保证 +
总时间预算。P4 切片迁移已接线（真实重迁 os-probe + P5 判据级 55/55
验证）；`run_agent` 原样保留，存量调用点按覆盖地图分批迁移。

**规范**：[`docs/modules/agent.md`](./docs/modules/agent.md)（运行模型/phase 协议与 status 兼容/
段间接续/静态段与结果指针/预算与防打转/实现地图与老接口覆盖地图/定案记录）。

---

## 详细用法

### 子命令速查

```
# P0 环境门禁
python3 porter/main.py p0 \
    --linux-driver <驱动C源码> --target-os <目标OS树> \
    [--materials <资料…>] [--category <标签>] \
    --output-dir <工作区> --kb <new|use> <名>

# P1 拆分策略
python3 porter/main.py p1-strategy --output-dir <ws>   # 人工审阅 strategy.md
python3 porter/main.py p1-divide    --output-dir <ws>
python3 porter/main.py p1-resolve   --output-dir <ws>   # 出 deps.json
python3 porter/main.py p1           --output-dir <ws>   # 全流程直通（CP1 也拦）
# 或：导入外部 P1 交付物（替代 divide+resolve；机器复核——重建 modules/、
#     图重算环必须 0、deps 对账不一致以重算为准 exit 1）
python3 porter/main.py p1-import --output-dir <ws> \
    --plan <P1D_plan.json> [--deps <deps.json>] [--strategy <strategy.md>]

# P2 引导映射+骨架
python3 porter/main.py p2          --output-dir <ws> [--device-ids V:D[,V:D…]]
python3 porter/main.py p2-map      --output-dir <ws>   # 分步（断点重入幂等）
python3 porter/main.py p2-skeleton --output-dir <ws> [--device-ids …]
python3 porter/main.py p2-probes   --output-dir <ws> [--max-batches N]

# P3-P5 垂直循环
python3 porter/main.py loop --output-dir <ws> [--module M] [--max-modules N]
python3 porter/main.py p3   --output-dir <ws> [--module M]   # 分步
python3 porter/main.py p4   --output-dir <ws> [--module M]
python3 porter/main.py p5   --output-dir <ws> [--module M]

# P6 系统验收
python3 porter/main.py p6 --output-dir <ws>                      # 聚合健康
python3 porter/main.py p6 --output-dir <ws> --draft-l4           # L4 草案
python3 porter/main.py p6 --output-dir <ws> --finalize-l4        # L4 定稿门
python3 porter/main.py p6 --output-dir <ws> --execute [--l4]     # 执行重测
python3 porter/main.py p6 --output-dir <ws> --defect-add ID --title … --evidence …
python3 porter/main.py p6 --output-dir <ws> --defect-close ID --root-cause … --fix … --regression …
python3 porter/main.py p6 --output-dir <ws> --defect-park ID --rationale …
python3 porter/main.py p6 --output-dir <ws> --defect-diagnose ID

# P7 终态报告
python3 porter/main.py p7 --output-dir <ws>
python3 porter/main.py p7 --output-dir <ws> --patch-register GAP --title … --rationale …
python3 porter/main.py p7 --output-dir <ws> --patch-status GAP --to planned|proposed|closed

# 知识库审核/分类/晋升
python3 porter/main.py kb --output-dir <ws>                       # 列候选+CP5 材料
python3 porter/main.py kb --output-dir <ws> --classify            # agent 批量归类
python3 porter/main.py kb --output-dir <ws> --promote all         # 晋升
python3 porter/main.py kb --output-dir <ws> --reject <ID>

# 知识沉淀（固定知识晋升）
python3 porter/main.py p1-promote --output-dir <ws> --driver <名>
python3 porter/main.py p2-promote --output-dir <ws> --driver <名> [--target <名>]

# 人工关口 CLI（便利层）
python3 porter/main.py gate list   --output-dir <ws>              # 未答关口
python3 porter/main.py gate show   <id> --output-dir <ws>
python3 porter/main.py gate answer <id> --set 字段=值 --output-dir <ws>
python3 porter/main.py gate review --output-dir <ws>

# log 查询
python3 porter/main.py log --output-dir <ws> tail   [--kind K] [--subject S] [--module M] [-n N]
python3 porter/main.py log --output-dir <ws> runs
python3 porter/main.py log --output-dir <ws> show   <run_id>[-n 尾行]
python3 porter/main.py log --output-dir <ws> timeline [--module M]
```

### 知识库选择（P0 必填）

知识库物化在 `<ws>/knowledge/`（temp 草稿区在 `<ws>/knowledge/temp/`），
随工作区 git 统一入库；promote 后自动同步回全局库 `knowledge/<名>/`
（跨迁移复用素材）：

```bash
# 新建（缺省复制 base 工具随附知识 → <ws>/knowledge/）
python3 porter/main.py p0 … --kb new my-port
python3 porter/main.py p0 … --kb new my-port --kb-empty          # 建空目录

# 从全局库种子化（复用上次迁移的沉淀）
python3 porter/main.py p0 … --kb use asterinas
```

缺省 `--kb` → rc 2 + 打印选择指引（列既有全局库）。
（`--kb-git` 已退役：知识库不再放工具仓，兼容保留该参数但无效果。）

### git 管理（vcs）

规范：[`docs/modules/vcs.md`](./docs/modules/vcs.md)。两个 repo 全程 commit 管理（best-effort，失败只告警不阻塞）：

- **目标 OS repo**：P0 扫描并行 git 仓（嵌套只管最外层）记 baseline +
  建 porter 分支（`--os-branch` 指定须全新，缺省自动生成；工作区仓同
  分支名）。P2 骨架 / P4 每模块 / P6 execute / 求解修码后 commit。
- **工作区 repo**：P0 时 `git init`（写 `.gitignore` 排除台账与导出物）。
  阶段末 / loop 每模块 done / **每次 agent 调用前后**（pre-agent 与
  agent commit 成对，`git diff <pre>..<post>` = 该次调用的 ws 产物）/
  exit 3 停车 / answers 消费后 commit（知识库随工作区入库）。
  两仓 commit 流互相独立：目标 OS 只在既定点提交。

```bash
python3 porter/main.py p0 … --os-branch porter/e1000-round2   # 可选
python3 porter/main.py vcs export --output-dir <ws>   # 导出 bundle 集 → <ws>/exports/
python3 porter/main.py vcs import --bundle <f.bundle> --repo <git仓路径> \
    [--branch porter/e1000-...]                        # 新机器接回 commit 链
```

- P7 末自动导出；`final_report` 含 commit 链（哪次 commit 改了什么，
  台账 `<ws>/vcs_commits.json`）。
- 导入端要求同一起点 commit（目标 OS 的 baseline 一致），hash 完全保留。
- 配置：`porter/config.json` 的 `vcs` 节（`enabled` 总开关；
  `identity` git 身份兜底）。`PORTER_VCS=0` 强制全关。

### 人工介入操作

工具停下时（exit 3）生成待办清单 `human_questions.md`（每条带填空表格）。
在 `answers.md` 照表格填几行，重跑同一条命令即恢复：

```markdown
## @<关口id>
strategy: bypass
rationale: e1000 单队列，MVP 无消费方
```

也可用 `gate answer` 命令行代填。所有问题登记进 `gates.json` 台账，你只
负责表态，改文件由工具代做。

### 退出码

| 码 | 含义 | 操作 |
|---|---|---|
| 0 | 成功/推进完成 | — |
| 1 | 失败 | 查日志 |
| 2 | 前置缺失 | 先跑前置阶段 |
| 3 | 需人工 | 填 `answers.md` → 重跑同命令 |

### 测试

```bash
python3 -m unittest discover -s tests    # 152 例，~0.15s
```

---

## 使用样例

### 端到端最小跑法（e1000→Asterinas）

```bash
cd driver_migration_tool

# P0 环境门禁（真实 agent 调用 + 真实探测）
python3 porter/main.py p0 \
    --linux-driver /path/to/linux/drivers/net/ethernet/intel/e1000 \
    --target-os    /path/to/asterinas \
    --materials    examples/asterinas-materials/notes-build.md \
    --materials    examples/asterinas-materials/notes-device.md \
    --materials    examples/asterinas-materials/ci-snippet.md \
    --output-dir   migrations/my-e1000-port \
    --kb           use asterinas

# P1 拆分策略
python3 porter/main.py p1-strategy --output-dir migrations/my-e1000-port
# → 人工审阅 migrations/my-e1000-port/P1/strategy.md
python3 porter/main.py p1-divide    --output-dir migrations/my-e1000-port
python3 porter/main.py p1-resolve   --output-dir migrations/my-e1000-port
# 外部完成的 P1 成果可直接导入（重建 modules/ + deps 对账），替代上面两步：
# python3 porter/main.py p1-import --output-dir migrations/my-e1000-port \
#     --plan <P1D_plan.json> --deps <deps.json> --strategy <strategy.md>

# P2 引导映射+骨架+探针+验收
python3 porter/main.py p2 --output-dir migrations/my-e1000-port

# P3-P5 垂直循环（全自动 ×N 模块）
python3 porter/main.py loop --output-dir migrations/my-e1000-port

# P6 系统验收
python3 porter/main.py p6 --output-dir migrations/my-e1000-port --draft-l4
# → 人工审阅 L4 草案
python3 porter/main.py p6 --output-dir migrations/my-e1000-port --finalize-l4
python3 porter/main.py p6 --output-dir migrations/my-e1000-port --execute --l4

# P7 终态报告
python3 porter/main.py p7 --output-dir migrations/my-e1000-port
```

### 断点重入示例（exit 3 → answers.md → 重跑）

```bash
$ python3 porter/main.py loop --output-dir migrations/my-e1000-port
[porter] loop: P4(rx-ring) 失败 rc=1（attempts 3/3）
[porter] gates: panic loop.attempts.rx-ring-p4（待答关口 1 个）→ 详见 human_questions.md
$ echo $?
3

# 人：看 human_questions.md 表单，修好问题，填答案
$ cat >> migrations/my-e1000-port/answers.md <<'EOF'
## @loop.attempts.rx-ring-p4
note: 修复了 cargo 路径未导出导致的编译失败
EOF

# 重跑同命令——从断点继续，已成功切片不重做
$ python3 porter/main.py loop --output-dir migrations/my-e1000-port
[porter] gates: 关口已应用 loop.attempts.rx-ring-p4（attempts 清零）
[porter] loop: P4(rx-ring) …   ← 从断点继续
```

### 泊车绕行（某模块卡住，先做后续独立模块）

```bash
# rx-ring attempts 烧穿 exit 3 后，若 os-stats 的 deps 全部 done：
python3 porter/main.py loop --output-dir migrations/my-e1000-port --module os-stats
# → 只推进 os-stats 走完剩余相位即 exit 0（指针仍指向泊车模块）
```

---

## 目录结构

```
driver_migration_tool/
├── porter/              # 编排器（Python 标准库，零第三方依赖）
│   ├── main.py          #   总入口：python3 porter/main.py <phase> <args>
│   ├── config.json      #   仓级运行时配置
│   ├── common/          #   跨阶段共用（agent 调用抽象 / C 符号扫描 / vcs git 管理）
│   ├── env/             #   P0 专属（输入解析/类别/环境提取/探测/门禁）
│   ├── divide/          #   P1 专属（拆分策略→模块划分→依赖解环）
│   ├── bootstrap/       #   P2 专属（引导映射+骨架+探针）+ 知识子系统
│   ├── loop/            #   P3-P7 + gates + errorloop
│   └── log/             #   log 子系统（统一观测框架，纯静态零 agent）
├── skills/              # SKILL：agent 行为指令（17 个，每轮注入，指令性精瘦）
├── docs/                # 规范文档（sub-systems/=子系统，modules/=模块）
│   ├── sub-systems/     #   log / knowledge / human-intervention
│   └── modules/         #   error-handling / vcs
├── knowledge/           # 全局知识库（base 工具随附 + <name>/ 跨迁移复用素材）
│                        # 本次迁移的 kb 与 temp 草稿在 <ws>/knowledge/ 下
├── examples/            # 资料束样例（Asterinas；模拟开发者提供的自由资料）
├── refs/                # 参考数据（QEMU/libslirp 源码副本等）
├── tests/               # 测试（131 例，协议的行为级定义）
├── migrations/          # 迁移项目工作区（运行时生成，.gitignore）
├── tool-audit-report.md # 完整工具开发报告（所有开发细节：历史/架构/文件地图/未做项）
└── TODO.md              # 全局待办（已商定但不做在本轮）
```

完整工具开发报告（历史/架构/文件级实现地图/未做项）见
[`tool-audit-report.md`](./tool-audit-report.md)。
