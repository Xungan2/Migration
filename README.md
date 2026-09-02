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
| P1 | ① 拆分策略（agent 读 Linux 源码产出自由 Markdown 策略分析 strategy.md——零 schema 契约，每次人工审阅放行；样例库 base 与知识库目录的 splits/strategies/，样例 = strategy.md 产物原样，INDEX 路由 + 按需读全文；run_strategy 自动草稿入 knowledge/temp/，产出 reports/P1-knowledge.md 价值判定，P1 后人工决定并用 p1-promote 沉淀）② 模块划分与依赖 DAG（策略指导下物理切分）③ MVP 门禁（人工审定范围）。进行中：strategy 已实现并实测 | 进行中 |
| P2 | **引导映射 + 全局骨架**【一次性】：2a 引导映射（生命周期主轴系统级设施全景：注册枚举/MMIO/DMA/中断/锁上下文/内存设施/子系统对接 7 域 + 换思路裁定 + 接线清单；agent 分批小调用，evidence 源码核实铁律机器化）→ 2b 全局骨架（目标 OS 专属模板：crate + 空 probe + 探针宿舍 + ktest 位 + 栈接线桩 + 全部接线点；零驱动功能）→ **2c 探针预生成**（全模块使用面并集 ∩ 高风险映射 − 已探 claim，≤5 条/批；贵且长寿命的验证前置到流程头部的稳定阶段，P3 探针步骤退化为补新；残余 FAIL 降级 gap 留消费者模块处置）→ 验收（build/boot 双信号 + 组件日志特征 + 无 PROBE FAIL 行） | ✅ 本仓 |
| P3-P4-P5 | **单一垂直循环 ×N**（循环序 = P1 deps.json 拓扑序；方案 A 相位重构：P4=生产、P5=模块级验收）：P3(M) 增量映射（脚本提取 M 的外部 API 使用面四分类：跨模块/已映射/噪音/真缺失 → 只补缺，maps 域知识目录注入"仅提示"（INDEX + agent 自取））→ gap 处置分类（bypass/fill/register-fill/human 四策略）→ 判据草案 criteria.json（strategy §5 机器化，L0-L4；仪式型模块强制组件级假后端判据）→ 高风险探针（≤5 条/批生成，住骨架，runner 双信号判定，FAIL 有界改判）；P4(M) fill 统一阶段（strategy=fill 的平台加法式补齐：新增 API 禁改默认 + 专属探针 + platform_patches.json 登记，失败回退 bypass）→ 迁移（文件×≤900 行切片，映射表作数据注入只翻译不研究；寄存器访问经 trait 抽象，仪式类切片附组件级测试）→ 轮末快速冒烟（compile+boot 防毒化闸门）；P5(M) 模块级验收：L1 build / L2 boot 双信号 / L0 ktest 同场（单测+组件级）/ L3 qemu.log regex + 累积回归（已 done 模块 L0+L3 重跑）+ deferred 登记（消费者落地当轮清偿）→ P5/<M>/reports/acceptance.json（兼容读旧 P4 位置）。人工关口：连续直通，仅 gap human/验收超界/deferred 无法清偿时 exit 3 介入（answers.md 承接；泊车模块可 `--module` 绕行后续独立模块） | ✅ 本仓（e2e-test-retry 16/16 完成） |
| P6 | 系统验收（全局收口）：**聚合模式**（默认，零重测：acceptance/deferred/判据状态全景 → P6/reports/health.json/.md）+ **执行模式** `--execute`（一轮 build + SLIRP boot + ktest → 全判据重判 + deferred 清偿——P6 为哨兵 `__P6__` 的 owner，读取兼容旧 `P5`/`__P5__`）+ `--l4`（L4 判据判定：驱动内核自测打 `L4 <id> PASS\|FAIL` 行，boot 日志正则判定；判据定稿走 `--finalize-l4` 审核门，porter/config.json 配 agent/human，human 停车等 answers.md 放行）；defects.json 缺陷账本（发现/根因/修复/回归证据四字段强制） | ✅ 本仓（e2e-test-retry 全绿除泊车） |
| P7 | 终态报告（`p7` 聚合：P0→P6 全产物 + git baseline diff + crate/映射统计 → P7/reports/final_report.json/.md，人工撰写区收口）+ 知识沉淀（p2-promote 映射晋升同名替换；pitfalls 踩坑域在知识库目录）+ 上游补丁提案台账（--patch-register/--patch-status：proposed 附 P7/reports/patches/ 提案文档；存量 planned 评估后 closed 理由入档） | ✅ 本仓（e2e-test-retry 完成） |

## 核心设计决策（讨论定稿记录）

1. **定界=模块划分，放 P1**：对 Linux 源码按功能划分模块，模块逐步迁移、
   后继模块只依赖已迁模块+胶水；MVP=模块前缀+模块内范围，P1 末人工一次审定
2. **测试二分法**：模块验收（依赖闭包=自身+已迁+胶水+既有设施；累积回归）
   vs 系统验收（按用户可见能力组织；含 deferred 判据清偿）。测试内容设计
   just-in-time：P1 草稿→P4 落地→P5 验收→P6 收口；验证能力探测在
   P6a（非 P0）
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
    --name my-first-port \
    --kb            new my-first-port   # 知识库目录（必填显式选择）
# --materials 可多次且可省略（agent 将仅凭目标 OS 源码树提取）
# --kb new <名>：新建（缺省复制 base 工具随附知识；--kb-empty 建空目录；
#   --kb-git ignore 可把该目录加进 .gitignore，缺省 track）
# --kb use <名>：指定既有目录（如 asterinas）复用；不带 --kb → rc 2

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
# 知识沉淀（增量）：p2-map 末自动草稿入 knowledge/temp/maps/；P2 末
# （首个沉淀点）人工审阅 mapping.md 后晋升：
#   python3 porter/main.py p2-promote --output-dir <ws> --driver e1000 --target asterinas
# （此后每轮 P3(M) 末自动刷新草稿——含人工路径 exit 3 后；可再次晋升——
#   同名替换保 hits）
```

# P3/P4/P5：垂直循环（须先跑过 p2；断点重入幂等）

```bash
# 全自动循环（P3(M)→P4(M)→P5(M) ×N，拓扑序推进；--max-modules 做切片验证）
python3 porter/main.py loop --output-dir migrations/my-first-port \
    [--module hw-defs] [--max-modules 1]

# 分步（单模块）
python3 porter/main.py p3 --output-dir migrations/my-first-port [--module M]
python3 porter/main.py p4 --output-dir migrations/my-first-port [--module M]
python3 porter/main.py p5 --output-dir migrations/my-first-port [--module M]

# 泊车绕行：某模块 attempts 烧穿 exit 3 后，可对 deps 已全部 done 的
# 后续独立模块显式 --module 绕行（只推进该模块即退出）。

# 产物（工作区级）：
#   loop_state.json      循环状态机（order + 每模块 phase/attempts
#                        {p3,p4,p5} 三桶；读存量自动补 p5 键）
#   deferred.json        deferred 判据登记（消费者落地当轮清偿，残余归 P6）
#   platform_patches.json fill/register-fill 登记（P7 上游补丁素材；
#                        P7 --patch-register/--patch-status 流转
#                        planned|proposed|closed，closed_note 入档）
#   human_questions.md   exit 3 人工关口问题（answers.md 承接，被消费节自动移除）
#   defects.json         P6 缺陷账本（发现/根因/修复/回归证据四字段）
#   P3/<M>/reports/      surface.json（使用面四分类）/ gap_decisions.json
#                        （处置分类）/ criteria.json（判据草案）/
#                        probes.json（探针注册表）/ report.md
#   P4/<M>/reports/      fill.json / migration.json（切片清单）/
#                        report.md（旧编号期还有 acceptance.json——
#                        P5 只读兼容）
#   P5/<M>/reports/      acceptance.json（L1/L2/L0/L3 逐判据结果 +
#                        累积回归 + deferred 清偿）/ report.md
#   P6/reports/          health.json/.md（聚合/执行全景）、
#                        l4_criteria.json（定稿判据 + 审核门状态）、
#                        l4_criteria_REVIEW.md（human 门评审摘要）
#   P7/reports/          final_report.json/.md（终态报告，人工撰写区
#                        收口）、patches/<gap>.md（上游补丁提案）
# 退出码：0 推进完成；1 失败；2 前置缺失；3 需人工——把答案写入
#   ws/answers.md（新协议 `## @<关口id>` 表单节；旧键 `## <linux_api>` /
#   `## retry <module>[-p3|-p4|-p5]` 兼容仍可用）重跑即续，
#   详见下节"人工介入"。
# 验收分层（P5(M) 执行）：L1/L2=runner build/boot 双信号；L0=runner
#   unit_test 节（机制中立：P0 skill 探明且 smoke_cmd 实跑过；存量
#   工作区由 P5 首次自动补探回填并真跑烟测复核（第二道），无机制则
#   L0 判据自动转 deferred）；L3=qemu.log regex
#   （本模块 + 已 done 模块累积回归）；e2e 归 P6 系统验收。
# P4 轮末快速冒烟 = compile+boot 双信号闸门（防半成品毒化，判据级
#   验收归 P5）。
```

<!-- ============================================================
     给未来 README 重写 session 的指引（人工介入子系统）：
     - 放置位置：本处（CLI/用法之后、横切原则之前）——读者先知道
       工具能跑什么，再知道什么时候需要自己出场。
     - 展开程度：保留下面"两种介入方式"结构（固定介入点表 + panic
       信号表 + 操作速查）+ 链接；协议细节（账本 schema/路由/生命周期）
       不要进 README，全在 docs/human-intervention.md（第 3 章）。
     - 两张介入点清单改代码时须同步更新（新增检查点/panic 信号）。
     - "工作区文件"清单应补五个交互面文件：gates.json /
       human_questions.md / answers.md / policy.md / checkpoints/。
     - 限制与演进不进 README（docs 第 5/6 章 + TODO.md 的活）。
     ============================================================ -->

## 人工介入（什么时候需要你出场）

设计目标一句话：**把"人盯着工具"变成"工具排队等人"**——你不在场时
工具能自己推进的就推进，拿不准的先记下来攒着事后批量找你确认；实在
推进不下去才停下。介入分两种方式，处理流程相同（看摘要/清单 → 填表
作答 → 重跑同一条命令从断点继续），停车时机不同。

### 方式一：固定介入点（检查点）

流程里预先安排好的审核时刻——这些地方预计需要人拍板（范围取舍、
验收标准、结果确认）。到点工具生成审阅摘要（checkpoints/ 下，列出
期间所有 AI/规则替你自动做的决定及置信度），你在待办清单
（human_questions.md）里按填空表格作答：整体批准（verdict: approve）
或否决某条自动决定（verdict: veto——工具回滚并安排重做）。

| 检查点 | 位置 | 审什么 | 默认 |
|---|---|---|---|
| CP0 | p0 末 | 环境探测结论备忘 | 开（不停车） |
| CP1 | P1 拆分策略产出后 | 模块拆分与范围（可先编辑 strategy.md 再批） | 开 |
| FM | loop 首个模块完成时 | 首模块的决策/判据/代码形态能否复制给其余模块 | 开（仅一次） |
| CP2 | P2 映射产出后 | API 映射质量抽审 | 关 |
| 债限额 | loop 过程中 | AI 自动决策攒够 30 条，批量复核 | 开 |
| CP3 | loop 全部完成 | 端到端验收判据草案（p6 --draft-l4 生成）审定+定稿 | 开 |
| CP4 | P7 开始前 | 缺陷修复闭账批审 | 开 |
| CP5 | P7 末 | 知识沉淀晋升提醒 | 开（不停车） |

### 方式二：panic（异常停车）

借用内核 panic 的含义：脚本自己检测到预定义的异常信号、且自动手段
（重试/常备规则/AI）都解决不了时，立即停车等人。停车瞬间保存出错
现场（快照：启动日志、判据状态、镜像哈希，不可变），问题带证据文件
和填空表格进待办清单；作答可附诊断笔记（进档案并带给下一轮 AI）。

| 异常信号 | 触发条件 |
|---|---|
| 自动重试烧穿 | 同一模块同一阶段连续失败 3 次 |
| 同错误死循环 | 同一编译错误重复 ≥2 次（零进展，提前停） |
| 推进超时 | 单模块超过 1 小时（疑似 AI 空转） |
| 映射不可用 | 迁移中 AI 报告前提映射有误（立即停，不烧重试） |
| 判据无法清偿 | 依赖模块全部完成后某验收判据仍不过 |
| 启动日志拿不到 | 复探一次仍缺——本轮判定中止（不按空日志误判） |
| 环境类 | P0 门禁失败 / 环境探测 3 轮未果 / 驱动类别无法识别 / P1 解环 3 轮失败 |

注：决策类问题停下前会先问你事先写好的常备规则（policy.md，如"凡
调试统计接口一律丢弃"）和 AI 照表试答；被自动答掉的决定全部记账，
到固定介入点批量复核、可否决。

### 操作速查

工具停下时生成待办清单（human_questions.md，每条带填空表格）；在
答案文件（answers.md）照表格填几行，重跑同一条命令即恢复。也可以用
命令行代填：

    python3 porter/main.py gate list --output-dir <工作区>   # 还有什么没答
    python3 porter/main.py gate answer <编号> --set 字段=值 --output-dir <工作区>
    python3 porter/main.py gate review --output-dir <工作区>  # 批量审阅材料

所有问题登记进工作区同一份台账（gates.json），你只负责表态，改文件
由工具代做。完整协议/实现/演进见 docs/human-intervention.md。

## 知识库（工具用过的经验，越攒越省）

<!-- 给未来 README 重写 session：本节为结构型介绍（两种知识+各自处理
     方式+接入点清单+命令速查），协议细节全在 docs/knowledge.md，勿在
     README 展开。新增知识域（kb.DOMAINS）或调用点时，下文"固定知识收
     成点"与"检索接入点"两张表须与 docs/knowledge.md 3.3/3.6 同步更新。 -->

设计目标一句话：**把"每次迁移从零摸索"变成"站在上次迁移的肩膀上"**
——经验自动收集成草稿、人工把关晋升、下次 agent 自己查着用。知识放
在工具仓库 `knowledge/` 下，分三个区：

```
knowledge/
├── base/      # 工具随附的一般知识（任意目标 OS 可用；git 跟踪）
├── temp/      # 草稿区（骨架跟踪，内容 gitignore）——agent 可写、未经人审
└── <name>/    # 一次迁移（或自维语料）的知识库目录；本次迁移的
               # 知识库 = temp ∪ <name>（开始时显式指定）
```

每个区内分五个子目录，即五种知识分类：**maps**（API 映射表）/
**gaps**（API 缺口处置，一个 API 一个文件）/**runbook**（目标 OS
构建/启动/测试手册）/**splits**（拆分策略样例）/**pitfalls**
（踩坑记录）。

### 知识一：固定知识（每次迁移必然产出）

流程到了固定位置就自动收成草稿进 temp（幂等，人工路径也不漏），
人审后晋升：

| 域 | 自动收成点 | 内容 |
|---|---|---|
| maps | P2 末 + 每轮 P3(M) 末 | API 映射整表（direct/adapt/gap/not-migrated 计数入描述） |
| gaps | 每轮 P3(M) 末 | 每个 API 的处置决策（策略/指令/证据/人工理由）+ fill 成败 |
| runbook | p0 末（环境探明后）+ P5 单测回填后 | 构建/启动/单测命令、成功特征、坑史 |
| splits | P1 产出策略时 | 拆分策略样例（与已沉淀一致不重写） |

### 知识二：随机知识（偶发发现）

四类探查钩子自动捕获成**候选**（去重闸防灌爆），经人工审核后入库：

| 钩子 | 捕获什么 |
|---|---|
| 关口答案 | 填表时写的诊断笔记/裁定理由（含 veto 理由） |
| 台账命令 | 缺陷根因链（--defect-close）、泊车理由、L4 park 理由、补丁提案 |
| 产物翻转 | 编译失败后重试成功的错误→修复留痕、探针降级判别现场 |
| agent 自报 | agent 干活时顺手发现的坑（输出里的 lessons 字段） |

处理流程：CP5 检查点生成备审材料（候选队列 + 草稿清点 + 健康报告：
哪些知识被用过几次/从没用过）→ agent 批量归类（可选）→ 人晋升或
拒绝。

### 检索（agent 怎么用知识）

agent 的任务指令里附带"知识条目目录"（每条一行：文件 + 一句话
说明，总纲规则 0：动手前必须先查），agent 判断相关才读全文，用完
报告读过哪些（计入热度）。**历史结论必须重新核实才能采用**；替人
自动答问题时只查已审知识（草稿不参与自动决策）。各调用点接入：
T3（runbook，历史基线把探测从 3 轮压到 1 轮）、P2a/P3 映射
（maps）、P3 gap 分类与 P4 fill（gaps——"这个 API 以前 fill 失败
过吗"直接查文件名）、P1 拆分（splits）、路由层答关（pitfalls 等）。

### 操作速查

```bash
# 开始一次迁移：显式选知识库目录（必填，缺省 rc 2）
python3 porter/main.py p0 … --kb new my-port          # 新建（复制 base）
python3 porter/main.py p0 … --kb new my-port --kb-empty --kb-git ignore
python3 porter/main.py p0 … --kb use asterinas        # 复用既有

# 迁移结束后的审阅与晋升（CP5 材料：checkpoints/CP5_knowledge.md）
python3 porter/main.py kb --output-dir <ws>                 # 候选清单+材料
python3 porter/main.py kb --output-dir <ws> --classify      # agent 批量归类
python3 porter/main.py kb --output-dir <ws> --promote all   # 晋升（--to 可改类）

# 固定知识晋升（各域独立命令）
python3 porter/main.py p2-promote --output-dir <ws> --driver e1000 --target asterinas
python3 porter/main.py p1-promote --output-dir <ws> --driver e1000
```

完整协议/实现/演进见 docs/knowledge.md。

## 横切原则（实现与后续阶段必须遵守）

- **判据双信号**：退出码 + 日志特征，缺一不可（管道吞退出码的教训已内置）
- **agent 产物信任但验证**：一切 agent 输出（JSON/runner/检索）经机器校验
- **幂等推进**：各步产物存在即跳过，失败可断点重跑
- **人工介入接口**：统一走关口账本（gates.json + human_questions.md
  渲染 + answers.md 表单作答，`porter gate` CLI 可代填）；分三级应答
  （policy.md 常备规则 → agent → 人），检查点批审 + 异常停车两车道。
  规范见 docs/human-intervention.md（新增介入点必须登记账本，禁止
  手写 human_questions.md）
