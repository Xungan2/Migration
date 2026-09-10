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
源码与目标目录首次运行必填；续跑可只提供 `--output-dir`。`p0` 是同一新入口的别名。
`--prepare-only` 仅校验、保存输入；`--budget` 是整个执行过程的墙钟预算，默认 10800 秒。
目标树需可写，工具不会自动切换目标分支、重置代码或提交迁移修改。

该分支只实现新架构。使用新的空工作区；旧工作区不会自动转换，也不会继续旧 P1–P7。
完整设备业务迁移、旧模块物理抽取和下游流程不在本工具当前执行范围内。

## 结果与续跑

- `prepare/state.json`：整体状态、两项主 agent 验收状态、证据指纹、会话与知识处理进度。
- `prepare/handoffs/`：任务交付、主 agent 决策、验收与失败交接；历史记录保留。
- `prepare/logs/`：每次调用的提示与原始输出；`prepare/knowledge/` 保存知识处理收据。
- `knowledgebase/`：按主题组织的 Markdown；根和主题 README 是按需阅读入口。
- `knowledgebase/verification.md`：知识 agent 记录的主 agent 验收结论；整体是否完成以状态文件为准。

同一命令续跑会复用会话与仍有效的证据。显式传 `--intent-file` 更新工作区 `goals.md`，
省略则沿用它。输入、源驱动或验收引用的代码/配置/证据变化时，需要主 agent 复核受影响项。
需要回答的问题查看交接记录，将答案写入工作区 `answers.md` 后续跑。

返回码：`0` 完成（或仅输入准备成功），`2` 输入/工作区问题，`3` 阻塞、预算或 provider 失败，
`130` 中断。失败时保留日志、交接和有效验收，知识维护中断不会冒充整体完成。
主 agent 验收证据语义；Python 检查交付、状态和指纹，不把 JSON 成功声明当成独立机器证明。

## 开发与验证

实现集中在 CLI、工作区输入、opencode transport、统一执行循环四个模块。
[规格](docs/specs/unify-p01.md)和[架构决策](docs/adr/0001-unify-p0-p1.md)说明职责与范围。

```bash
python3 -m unittest discover -s tests
```

unittest 经公开 CLI 调用本地 opencode 替身，覆盖分别验收、知识续接、按需交接、失效复核、
输入、锁、中断及恢复，并包含本地编译/动态载入案例。它不访问模型、容器或真实目标 OS；
真实迁移实验由使用者执行。旧阶段文档仅作历史参考。
