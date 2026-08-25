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
│   │   └── agent.py     #     opencode 非交互调用抽象（PORTER_MODEL 可配）
│   └── env/             #   P0 专属：目标环境接入与验证
│       ├── inputs.py    #     T1 输入解析（脚本）
│       ├── category.py  #     T2 类别识别（agent）
│       ├── extract.py  #     T3 环境信息提取（agent 多轮×探测交织×人工升级）
│       ├── probe.py     #     T3 探测执行（build/boot/boot_with_device，双信号）
│       └── gate.py      #     T5 门禁（脚本，机器可检）
├── skills/              # SKILL：agent 行为指令（每轮注入，指令性、精瘦）
├── examples/            # 资料束样例（Asterinas；模拟开发者提供的自由资料）
└── migrations/          # 迁移项目工作区（运行时生成）
```

目录分界规则：**多个阶段共用** → `porter/common/`；**单阶段专属** → 该阶段子目录（env/ 为 P0；未来阶段实现时再建各自目录）。

## 阶段流水线（设计定稿；P0 已实现）

| 阶段 | 职责 | 状态 |
|---|---|---|
| P0 | 开发能力硬门禁（编译/启动/设备挂载）+ 类别识别。设备核心检索（原 T3d）已后移——归入未来"依赖分析补充流程"（约 P1 后，落点随该流程设计确定；现阶段优先驱动代码本体迁移） | ✅ 本仓 |
| P1 | Linux 驱动解剖 + **模块划分与依赖 DAG**（MVP 定界门禁在 P1 末：模块前缀+范围+全局约束，人工一次审定）；模块卡含 verify 草稿（now/deferred+needs） | 设计定稿，待实现 |
| P2 | API/类型/头文件映射 + 高风险映射运行时探针 + 导出 P4 依赖序（映射知识的存储形态随知识库设计一并定） | 设计定稿，待实现 |
| P3 | crate 骨架 + **全量胶水 stub**（设备注册/栈接线/测试位——使"已迁移模块+胶水"从第一模块起即可依赖） | 设计定稿，待实现 |
| P4 | 按模块 DAG 增量迁移；每模块 L0-L4 分层验收（判据机器复核，agent 打勾仅为申请）；平台缺口正式状态（决策队列→gaps→绕过入档）；有界修复环 | 设计定稿，待实现 |
| P5 | P5a 预实测（验证能力探测：替身载波建环境基线→真实驱动偏离归因）→ P5b 系统验收（按能力组织，含从 P4 流入的 deferred 判据清偿） | 设计定稿，待实现 |
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

## 横切原则（实现与后续阶段必须遵守）

- **判据双信号**：退出码 + 日志特征，缺一不可（管道吞退出码的教训已内置）
- **agent 产物信任但验证**：一切 agent 输出（JSON/runner/检索）经机器校验
- **幂等推进**：各步产物存在即跳过，失败可断点重跑
- **人工介入三接口**（P0 现阶段）：T3 缺失问答（3 轮自动提取未果→
  human_questions.md→answers.md→R4）、runner.json 人工审核（reviewed=false→
  入库）、`--category` 人工指定；后续阶段追加（MVP 决策 P1 末等）
