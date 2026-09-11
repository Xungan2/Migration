# Porter

将 Linux 驱动迁移为目标 OS 原生驱动的准备工具。一个主 agent 共享调查、按需派发任务，
分别验收骨架构建/载入和建议迁移规划；一个专门的知识 agent 通过 opencode 续接维护知识。
两项验收通过且最后一批知识已整理并由主 agent 确认，流程才完成。

需要 Python ≥ 3.10、git、PATH 中可用的 opencode，以及目标 OS 的构建与运行环境。
Porter 本体只使用 Python 标准库。`PORTER_MODEL` 可覆盖默认模型。

主 agent 负责派发、必要性判断与验收，大任务由宿主顺序交给子 agent。按需选用三类任务 skill：
源码与依赖调查、原生骨架实现与构建载入验证、迁移规划与补充验证；不要求依次执行全部类别。
任务与验证目标的尝试记录保存在工作区状态中。补充验证按稳定目标只尝试一次，失败或超时后
由知识 agent 整理到 AUTO-TODO；规划和补充验证分别交付。必要目标重试需失败证据和针对性修正。
宿主按目标标识检查重复派发；主 agent 负责识别改名但语义相同的目标，并监督任务内的停止条件。

```bash
python3 porter/main.py prepare \
  --linux-driver linux-5.10/drivers/md \
  --target-os asterinas \
  --output-dir migrations/dm-unified \
  --intent-file examples/dm-zero-intent.md
```

先按[意图指南](docs/intent-guide.md)描述场景、功能范围、约束和最终成功标准。
源码与目标目录首次运行必填；续跑可只提供 `--output-dir`。完成 prepare 后运行 `pre-mono` 生成 mono 输入，再运行 mono。
`--prepare-only` 仅校验、保存输入；`--budget` 是整个执行过程的墙钟预算，默认 3600 秒。
目标树需可写，工具不会自动切换目标分支、重置代码或提交迁移修改。

该分支只实现新架构。使用新的空工作区；旧工作区不会自动转换。
完整设备业务迁移、旧模块物理抽取和下游流程不在本工具当前执行范围内。

## 准备阶段输出

完整执行 `prepare` 后，交付以下成果：

| 交付物 | 内容与位置 |
| --- | --- |
| 原生驱动骨架 | 位于目标 OS 源码树，包含模块骨架、构建依赖、注册与初始化接线 |
| 构建与载入证据 | 实际命令、退出结果、构建日志、产物引用和运行日志，证明骨架参与构建并实际载入 |
| 建议迁移规划 | Markdown 文档，包含功能与设备/协议范围、目录内源码归属、目录外依赖、模块划分、实施顺序、验证策略及未知项 |
| 可复用知识库 | 工作区 `knowledgebase/`，整理调查事实、目标接入经验、失败修法、决策和后续待办 |
| 验收与执行记录 | 两项验收结论、证据指纹、任务交接、调用日志与知识处理收据，支持审阅和续跑 |

骨架代码直接写入 `--target-os`，其余记录以 `--output-dir` 为工作区。规划文档和构建/载入
日志的具体路径由任务交接记录与知识库索引给出，不要求统一文件名。以下路径均相对工作区：

- `project.json`：源驱动、目标 OS、意图来源及附加资料指针；`goals.md` 保存传入的意图内容。
- `prepare/state.json`：整体状态、两项主 agent 验收状态、证据指纹、会话与知识处理进度。
- `prepare/handoffs/`：任务交付、主 agent 决策、验收与失败交接；历史记录保留。
- `prepare/logs/`：每次调用的提示与原始输出；`prepare/knowledge/` 保存知识处理收据。
- `knowledgebase/`：按主题组织的 Markdown；根和主题 README 是按需阅读入口。
- `knowledgebase/verification.md`：知识 agent 记录的主 agent 验收结论；整体是否完成以状态文件为准。

`--prepare-only` 只保存输入，不产生骨架、迁移规划或验收结论。完整准备阶段的 `complete`
表示骨架构建/载入、规划验收及知识整理完成；真实设备业务实现和设备验证缺口按交付记录说明。

## 续跑

同一命令续跑会复用会话与仍有效的证据。显式传 `--intent-file` 更新工作区 `goals.md`，
省略则沿用它。输入、源驱动或验收引用的代码/配置/证据变化时，需要主 agent 复核受影响项。
需要回答的问题查看交接记录，将答案写入工作区 `answers.md` 后续跑。

返回码：`0` 完成（或仅输入准备成功），`2` 输入/工作区问题，`3` 阻塞、预算或 provider 失败，
`130` 中断。失败时保留日志、交接和有效验收，知识维护中断不会冒充整体完成。
主 agent 验收证据语义；Python 检查交付、状态和指纹，不把 JSON 成功声明当成独立机器证明。

## mono（exp-mono）模块迁移

`pre-mono` 产出 `migration-plan.json` 与 `mono-input/modules/` 后，运行 `mono`
逐模块迁移：

```bash
python3 porter/main.py mono --output-dir <ws> [--module M]
                            [--module-research M] [--budget SEC]
                            [--budget-research SEC] [--budget-translate SEC]
                            [--session ID]
```

每模块跑**两段任务**——研究者 agent（只读目标树，产出结构化研究交付物）→
翻译者 agent（消费交付物写码+测试，四段复合 gate 验证）。平台事实全部
走工作区数据面，工具零目标 OS 假设（换 runner/manifest 数据即可跨目标
复用）。

- **双模型分层**（`porter/config.json` 的 `models` 块，跑前必配）：`reasoning`
  = 研究任务（读多写少的推理），`coding` = 翻译+修复任务（输出密集
  的生成）；两值允许一致；缺键或裸模型名（无 provider 前缀）启动即
  rc 2。
- 逐模块按 `migration-plan.json` 的 `order` 交错推进（R→T→R→T…，
  后继研究能读前序翻译的真实产物与契约登记表）；ledger 记 pass 即
  跳过（幂等断点续）；blocked/失败/预算耗尽 → exit 1 停车，重跑从
  断点续。
- **研究任务**：prompt 注入词典/契约登记表/泊车现文 + 规格 + 登记
  约定/测试基质/driver_home 现有文件；产出
  `exp-mono/research/<module>.md`（叙事正文 + 尾部 ```json 块：
  mappings/contracts/prunes/parking/read_list/negatives/
  open_questions，叙事必含「适配架构与整合方案」「可测面评估」两节
  标题）；编排器浅校验（verdict 词表/必填/必写节标题/证据路径警告）
  不过 → 同 session 回灌修（≤2 次）；过 → **机器收割**（mappings→
  词典、contracts→契约登记表、parking→泊车——agent 不再手写这三个
  文件，格式错误不可能发生）。预算 `clamp(600, LOC×1.5, 2400)`。
- **翻译任务**：prompt 注入交付物**全文**+契约登记表+泊车（不注入
  词典——交付物已含本模块裁定）；skill 授予**兜底研究权**（有界
  grep）并要求**矛盾上报**（交付物与目标树事实不符时不得静默改判）。
  预算 `clamp(900, LOC×1.3, 4200)`。
- **四段复合 gate**（便宜先行失败短路，反馈回灌同 session）：① 产物
  守卫：driver_home 非注释代码增量 ≥ `max(8, LOC//20)` + 构建单元
  登记核对 + git 改动白名单；② 构建：`runner.build` 原样；③ 驱动
  自启动（阻断）：全树构建后启动自检；④ 单测：`unit_test.driver_scope_cmd`
  （缺键降级全量 cmd）。修复段有**预算下限宽限**（FIX_FLOOR_SEC，
  防主段吃光预算后修复段被秒杀）；同签名连败先注入**停滞元反馈**
  （"换思路"警告）再 stalled。
- **单测验收为记账制，无个数指标**：agent 在 done JSON 声明
  `migrated_functions` / `tests` / `untested` 三字段；编排器机械核对
  migrated ⊆ tests ∪ untested、同一单元不双挂、声明测试数 ≤ 测试标记
  实际增量；**核对不过同 session 自动重试 ≤2 次**（进程内闭环）；
  `report.md` 渲染每模块函数覆盖台账（已测 fn/aspect、豁免理由）供
  人审——覆盖质量归人，不归计数器。模块 pass 时把当前事实写入
  `knowledgebase/modules/<module>.md`，后继模块消费最新知识而非旧日志。
- 终局验收：全量单测 + 驱动自启动**均为必要验收**，任一 FAIL →
  exit 1。
- 调试入口：`--module M` 单模块（须 order 内）；`--module-research M`
  只跑 M 的研究任务（跳过翻译）；`--budget-research/--budget-translate`
  覆盖两段预算；`--session` 续接超时中断的 provider 会话（研究续跑与
  翻译续跑按上次失败位置自动路由）。
- 工作区产物：`exp-mono/{ledger.json, research/<module>.md（研究
  交付物）, mapping-notes.md（词典，机器收割）, contracts.md（契约
  登记表）, parking.md, logs/, report.md, migration-plan.md, change.md}`
  与 `knowledgebase/`。

## 知识沉淀

迁移过程中的源码调查、接线方式、构建与载入经验、失败修法和规划决策，会持续整理到
工作区的 `knowledgebase/`，供后续迁移复用。`handoff` 保存一次任务交付，知识库维护当前
有效认识：每个事实只维护一处，其他主题通过链接引用，原始日志与历史交接保持原样。

任务 agent 交付报告，主 agent 通过交接记录提交决策与验收结论；专门的知识 agent 是共享
知识的唯一写入者。宿主将新增交接交给它，通过 opencode 会话续接更新知识库，并保存处理
收据。知识冲突先核对证据，无法消解时交回主 agent 决定。会话不可用时可从文档和收据恢复。

从 `knowledgebase/README.md` 进入，再按需阅读主题索引与具体材料。主题按实际内容创建，
不要求预先填满所有目录：

| 主题 | 沉淀内容 |
| --- | --- |
| `source/` | 源驱动职责、调用链、数据与状态、源 OS 行为及源码归属 |
| `integration/` | 目标接口、骨架接线、初始化机制、依赖与约束 |
| `environment/` | 工具链、容器、运行前置与环境限制 |
| `build/` | 构建命令、日志、产物及参与内核构建的证明 |
| `boot/` | 实际载入步骤、运行证据与已验证边界 |
| `testing/` | 测试方法、验证结果及设备验证缺口 |
| `plan/` | 迁移范围、模块职责、依赖顺序与建议步骤的材料或入口 |

四份总览便于快速接续工作：`AUTO-DECISION.md` 记录有效决策、理由与替代历史；
`AUTO-TODO.md` 记录未知项、未完成工作及后续触发和完成条件；`AUTO-FIXME.md` 记录故障、
失败尝试、修法与重验结果；`verification.md` 记录主 agent 的验收结论和证据。

知识条目需注明证据、适用版本与条件，区分事实、推断和未验证内容。构建通过、骨架载入与
真实设备功能验证分别记录，延期或缺少硬件的验证仍保留为待办。两项验收通过后，还需整理
最后一批交接并由主 agent 确认，整体状态才会变为 `complete`。当前仅维护本工作区知识，
不自动同步全局知识库；清理工作区会一并删除知识库，需要长期保留时应先归档。

## 开发与验证

清理迁移工作区先运行 `python3 scripts/clean-workspace.py` 预览，加 `--apply` 执行。
清理范围为 `migrations/`、`archive/`、Asterinas 的 `target/` 和 `osdk/target/`，
以及使用 `asterinas/dev` 镜像的 `porter-*`、`spi-nor-clean-*`、`migration-asterinas-dev`
容器及匿名卷。`--reset-target` 额外丢弃目标树修改和未跟踪文件，并切回仓库锁定提交。
脚本保留 intent、Porter 代码、基础镜像和宿主全局模型会话；仓库外的测试副本不自动删除。
它不清除目标仓库的 Git 历史，要求无历史干扰的实验仍应使用独立源码副本和全新模型数据目录。

实现集中在 CLI、工作区输入、opencode transport、统一执行循环四个模块。
[规格](docs/specs/unify-p01.md)和[架构决策](docs/adr/0001-unify-p0-p1.md)说明职责与范围。

```bash
python3 -m unittest discover -s tests
```

unittest 经公开 CLI 调用本地 opencode 替身，覆盖分别验收、知识续接、按需交接、失效复核、
输入、锁、中断及恢复，并包含本地编译/动态载入案例。它不访问模型、容器或真实目标 OS；
真实迁移实验由使用者执行。旧阶段文档仅作历史参考。
