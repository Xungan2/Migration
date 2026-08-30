# driver_migration_tool

高度自动化的驱动迁移工具：把 Linux 内核驱动**迁移（安全重写）**到任意目标 OS。
形态：调用非交互 agent（opencode）的脚本流水线 + SKILL 文件 + 分层知识库。
人工介入被压缩到特定环节的标准接口（决策队列 / 文档审核 / 知识审核）。

> 首个实验（e1000→Asterinas，18 轮全流程）已完成并验证了方法论可行性；
> 本工具是该方法论的通用化实现。设计细节见各阶段 SKILL 与 `reports`。

## 总体形态

```
driver_migration_tool/
├── porter/              # 编排器（Python 标准库，零第三方依赖）
│   ├── main.py          #   总入口：python3 porter/main.py <phase> <args>
│   ├── common/          #   跨阶段共用脚本（多阶段使用者进此）
│   │   ├── agent.py     #     opencode 非交互调用抽象（PORTER_MODEL 可配；
│   │   │                #       内置 32K 输出帽修复：注入
│   │   │                #       OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX）
│   │   └── symbol.py    #     C 源码符号静态扫描（P1 依赖图/P2a 使用面共用）
│   ├── env/             #   P0 专属：目标环境接入与验证
│   │   ├── inputs.py    #     T1 输入解析（脚本）
│   │   ├── category.py  #     T2 类别识别（agent）
│   │   ├── extract.py  #     T3 环境信息提取（agent 多轮×探测交织×人工升级）
│   │   ├── probe.py     #     探测执行（build/boot/boot_with_device，双信号；
│   │   │                #       P2+ 验收复用）
│   │   └── gate.py      #     T5 门禁（脚本，机器可检）
│   ├── divide/          #   P1 专属（拆分策略→模块划分→依赖解环）
│   └── bootstrap/       #   P2 专属（引导映射编排 + 骨架生成器）
├── skills/              # SKILL：agent 行为指令（每轮注入，指令性、精瘦）
├── examples/            # 资料束样例（Asterinas；模拟开发者提供的自由资料）
├── knowledge/           # 知识库（已沉淀；条目化、人审入库）
│   ├── splits/          #   拆分域
│   │   └── strategies/  #     策略样例（= strategy.md 产物原样；
│   │                    #       INDEX.json 目录 + README 沉淀规范）
│   └── maps/            #   API 映射域（P2 起；= mapping.md/json 产物原样，
│                        #     <驱动>@<目标OS> 命名；同名晋升=版本更新替换，
│                        #     活文档；消费铁律见其 README）
├── temp/                # 未沉淀知识暂存区（run_strategy 自动写入样例草稿；
│                        #   p2-map 自动写入/刷新映射草稿；
│                        #   人工审后 p1-promote / p2-promote 晋升入 knowledge/）
└── migrations/          # 迁移项目工作区（运行时生成）
```

目录分界规则：**多个阶段共用** → `porter/common/`；**单阶段专属** → 该阶段子目录（env/ 为 P0；未来阶段实现时再建各自目录）。

## 阶段流水线（设计定稿；P0 已实现）

| 阶段 | 职责 | 状态 |
|---|---|---|
| P0 | 开发能力硬门禁（编译/启动/设备挂载）+ 类别识别 + unit_test 烟测（agent 探明单测机制须附 smoke_cmd 并实跑过；门禁真跑复核——机制主张"agent 说"变"机器验"）。设备核心检索（原 T3d）已后移——归入未来"依赖分析补充流程"（约 P1 后，落点随该流程设计确定；现阶段优先驱动代码本体迁移） | ✅ 本仓 |
| P1 | ① 拆分策略（agent 读 Linux 源码产出自由 Markdown 策略分析 strategy.md——零 schema 契约，每次人工审阅放行；样例库 knowledge/splits/strategies/，样例 = strategy.md 产物原样，INDEX.json 路由 + 按需读全文；run_strategy 自动草稿入 temp/，产出 reports/P1-knowledge.md 价值判定，P1 后人工决定并用 p1-promote 沉淀）② 模块划分与依赖 DAG（策略指导下物理切分）③ MVP 门禁（人工审定范围）。进行中：strategy 已实现并实测 | 进行中 |
| P2 | **引导映射 + 全局骨架**【一次性】：2a 引导映射（生命周期主轴系统级设施全景：注册枚举/MMIO/DMA/中断/锁上下文/内存设施/子系统对接 7 域 + 换思路裁定 + 接线清单；agent 分批小调用，evidence 源码核实铁律机器化）→ 2b 全局骨架（目标 OS 专属模板：crate + 空 probe + 探针宿舍 + ktest 位 + 栈接线桩 + 全部接线点；零驱动功能）→ **2c 探针预生成**（全模块使用面并集 ∩ 高风险映射 − 已探 claim，≤5 条/批；贵且长寿命的验证前置到流程头部的稳定阶段，P3 探针步骤退化为补新；残余 FAIL 降级 gap 留消费者模块处置）→ 验收（build/boot 双信号 + 组件日志特征 + 无 PROBE FAIL 行） | ✅ 本仓 |
| P3+P4 | **单一垂直循环 ×N**（循环序 = P1 deps.json 拓扑序）：P3(M) 增量映射（脚本提取 M 的外部 API 使用面四分类：跨模块/已映射/噪音/真缺失 → 只补缺，knowledge/maps 消费侧 INDEX 路由+域过滤注入"仅提示"）→ gap 处置分类（bypass/fill/register-fill/human 四策略）→ 判据草案 criteria.json（strategy §5 机器化，L0-L4）→ 高风险探针（≤5 条/批生成，住骨架，runner 双信号判定，FAIL 有界改判）；P4(M) fill 统一阶段（strategy=fill 的平台加法式补齐：新增 API 禁改默认 + 专属探针 + platform_patches.json 登记，失败回退 bypass）→ 迁移（文件×≤900 行切片，映射表作数据注入只翻译不研究）→ 分层验收 L0-L4（L0=runner unit_test 节机制中立、L3=qemu.log regex、累积回归）+ deferred 登记（消费者落地当轮清偿）。人工关口：连续直通，仅 gap human/验收超界/deferred 无法清偿时 exit 3 介入（answers.md 承接） | ✅ 本仓（e2e-test-retry hw-defs 首切片验证） |
| P5 | P5a 预实测（验证能力探测：替身载波建环境基线→真实驱动偏离归因）→ P5b 系统验收（按能力组织，含 deferred 判据清偿 + 循环残余） | 设计定稿，待实现 |
| P6 | 终态报告（已迁/未迁/不适用完整度清单）+ 知识沉淀（形态随知识库设计定）+ 上游补丁提取（VCS baseline diff） | 设计定稿，待实现 |

## 核心设计决策（讨论定稿记录）

1. **定界=模块划分，放 P1**：对 Linux 源码按功能划分模块，模块逐步迁移、
   后继模块只依赖已迁模块+胶水；MVP=模块前缀+模块内范围，P1 末人工一次审定
2. **测试二分法**：模块验收（依赖闭包=自身+已迁+胶水+既有设施；累积回归）
   vs 系统验收（按用户可见能力组织；含 deferred 判据清偿）。测试内容设计
   just-in-time：P1 草稿→P4 落地→P5 收口；验证能力探测在 P5a（非 P0）
3. **类别=标签集合，失败友好**：仅选模板（加速器非承重墙）；`--category`
   人工覆盖；不可判定回落通用模板并警告；仅"非内核驱动"硬停
4. **自由资料驱动 OS 适配**：开发者提供自由资料（文档/笔记/CI 配置，形态
   不限，可省略）→ agent 自行阅读资料+目标 OS 源码树，提取出最小执行契约的
   runner.json（cmd/超时(全量+增量)/成功失败特征/日志位置/设备注入双机制），
   与真实探测交织修正（≤3 轮）；仍未解决则生成人工问题清单（exit 3），人填写
   answers.md 后重跑进入答案整合轮。runner 经人工审核（reviewed=false 标记）
   后即为一切构建/启动/设备注入的执行依据——OS 差异数据化，工具无 OS 专属代码。
   最小执行契约内嵌于 SKILL（P0-env-extract），不设独立 schema 文件
5. **SKILL vs 知识库**：SKILL=行为指令（每轮全量注入，漏执行会出事故的放这）
   知识库=事实资料（条目化、带 scope 元数据与命中计数；Tier1 任务域装配/
   Tier2 路由表指查/Tier3 自由检索三层注入；高频命中条目候选升格 SKILL）
6. **知识沉淀配置化**：总配置决定新知识入库策略；默认硬性人工审核
7. **agent 固定 opencode 非交互**；确定性动作用脚本（探测执行/门禁/脚手架）
8. **零预设产物**：不预灌历史经验、不预生成 OS profile/知识库骨架/记忆
   文档/state 等任何"待填充"结构——各产物的形态等其真实消费者出现（P1 起再定）。

## 用法

```bash
cd driver_migration_tool

# P0 全流程（真实 agent 调用 + 真实探测）
python3 porter/main.py p0 \
    --linux-driver  /path/to/linux/drivers/net/ethernet/intel/e1000 \
    --target-os     /path/to/asterinas \
    --materials     examples/asterinas-materials/notes-build.md \
    --materials     examples/asterinas-materials/notes-device.md \
    --materials     examples/asterinas-materials/ci-snippet.md \
    --name my-first-port
# --materials 可多次且可省略（agent 将仅凭目标 OS 源码树提取）

# 产物：
#   migrations/my-first-port/
#     project.json          身份+类别+VCS 基线（幂等真值源）
#     runner.json           机器可执行命令（待人工审核，reviewed=false）
#     reports/p0_report.md  门禁结论（人读）
#     reports/T3_*.json     T3 逐轮输出与探测结果（人工升级路径的历史回放源）
#     logs/                 agent 与命令原始输出（审计）
```

环境变量：`PORTER_MODEL`（默认 `zhipu-ai/glm-5.2`）。
退出码：0=P0 门禁通过；1=门禁未过/失败；2=参数错误；3=T3 需人工介入
（填写工作区 answers.md 后重跑，进入 R4 答案整合轮）。
agent 调用：T2 类别识别 1 次 + T3 提取/修正轮 1-3 次（+答案整合 1 次），
其余全部脚本执行。探测为金标准：三项双信号全 PASS 即通过；探测全绿时
agent 声明的剩余不确定项仅记为非阻塞备忘。

# P2：引导映射 + 全局骨架（须先跑过 p0/p1）

```bash
# 全流程（2a 映射 → 2b 骨架 → build/boot+组件日志验收）
python3 porter/main.py p2 --output-dir migrations/my-first-port \
    [--device-ids 0x8086:0x100e]      # 缺省 QEMU 目标默认收敛

# 分步（断点重入，幂等）
python3 porter/main.py p2-map --output-dir migrations/my-first-port
python3 porter/main.py p2-skeleton --output-dir migrations/my-first-port \
    [--device-ids ...]

# 2c 探针预生成（存量工作区补跑入口；幂等：已探 claim 跨注册表去重，
# 断点重跑只补缺口；--max-batches 可先试跑）
python3 porter/main.py p2-probes --output-dir migrations/my-first-port \
    [--max-batches 3]
# 产物：P2/reports/probes.json（预生成注册表）+ pregen_report.md；
# 探针住骨架 src/probes.rs（每次启动重跑=回归哨网）；P2/logs/ 留痕。
# 残余 FAIL 降级 gap 后不做四策略处置——留给消费者模块的 P3(M)
# 带使用位置上下文处理。

# 产物（工作区 <ws>/P2/）：
#   mapping.json        映射真值源（P2a 起，P3 增量累积；条目 9 字段+
#                       domain，evidence 为目标树 file:line 机器校验）
#   mapping.md          人读渲染（域分节四列表 + 换思路 + 接线清单）
#   reports/spine_api.json     生命周期主轴外部 API 提取（域分组）
#   reports/mapping_report.md  映射增量报告（人工审阅关口 = 末尾报告）
#   reports/skeleton_manifest.json  骨架写入清单（目标树新建/接线点）
#   reports/acceptance.json    验收结果（build/boot/日志特征）
#   logs/               agent 与验收原始输出
# 骨架实体文件生成在目标 OS 树（comps/<driver>/ + 接线点改动），
# 验收 = P0 runner 双信号 + 骨架组件日志特征（manifest 内可查）。
# 知识沉淀（增量）：p2-map 末自动草稿入 temp/maps/；P2 末（首个沉淀点）
# 人工审阅 mapping.md 后晋升：
#   python3 porter/main.py p2-promote --driver e1000 --target asterinas
# （此后每轮 P3(M) 末自动刷新草稿，可再次晋升——同名=版本更新替换）
```

# P3/P4：垂直循环（须先跑过 p2；断点重入幂等）

```bash
# 全自动循环（P3(M)→P4(M) ×N，拓扑序推进；--max-modules 做切片验证）
python3 porter/main.py loop --output-dir migrations/my-first-port \
    [--module hw-defs] [--max-modules 1]

# 分步（单模块）
python3 porter/main.py p3 --output-dir migrations/my-first-port [--module M]
python3 porter/main.py p4 --output-dir migrations/my-first-port [--module M]

# 产物（工作区级）：
#   loop_state.json      循环状态机（order + 每模块 phase/attempts）
#   deferred.json        deferred 判据登记（消费者落地当轮清偿，残余归 P5）
#   platform_patches.json fill/register-fill 登记（P6 上游补丁素材）
#   human_questions.md   exit 3 人工关口问题（answers.md 承接，被消费节自动移除）
#   P3/<M>/reports/      surface.json（使用面四分类）/ gap_decisions.json
#                        （处置分类）/ criteria.json（判据草案）/
#                        probes.json（探针注册表）/ report.md
#   P4/<M>/reports/      fill.json / migration.json（切片清单）/
#                        acceptance.json（L0-L4 逐判据结果）/ report.md
# 退出码：0 推进完成；1 失败；2 前置缺失；3 人工关口（gap human 队列 /
#   模块验收 FAIL 超界（attempts≥3）/ deferred 无法清偿）——把答案写入
#   ws/answers.md（`## <linux_api>` 或 `## retry <module>[-p3|-p4]`）重跑即续。
# 验收分层：L1/L2=runner build/boot 双信号；L0=runner unit_test 节
#   （机制中立：P0 skill 探明且 smoke_cmd 实跑过；存量工作区由 loop 首次
#   自动补探回填并真跑烟测复核（第二道），无机制则 L0 判据自动转
#   deferred）；L3=qemu.log regex
#   （本模块 + 已 done 模块累积回归）；e2e 归 P5。
```

## 横切原则（实现与后续阶段必须遵守）

- **判据双信号**：退出码 + 日志特征，缺一不可（管道吞退出码的教训已内置）
- **agent 产物信任但验证**：一切 agent 输出（JSON/runner/检索）经机器校验
- **幂等推进**：各步产物存在即跳过，失败可断点重跑
- **人工介入接口**：T3 缺失问答（3 轮自动提取未果→
  human_questions.md→answers.md→R4）、runner.json 人工审核（reviewed=false→
  入库）、`--category` 人工指定、样例草稿晋升审阅（P1 整体完成后开发者
  据 reports/P1-knowledge.md 决定是否 `p1-promote` 沉淀）；后续阶段
  追加（MVP 决策 P1 末等）
