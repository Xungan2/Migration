# Porter

Porter 是一个面向 Linux 驱动迁移的准备、执行和验收工具。它读取源驱动、目标 OS 源码树和迁移意图，调用 agent 完成源码调查、目标 OS 原生骨架、模块迁移与验证，并把计划、日志、交接和证据保存到工作区。

Porter 本体只依赖 Python 标准库。运行需要：

- Python 3.10 或更高版本；
- git；
- PATH 中可用的 `opencode`；
- 目标 OS 的编译、启动和测试环境。

完整的架构、运行、证据和验收说明写在 `docs/porter.md`；该文档包含独立的输入模板和示例，不要求再打开其他说明文件。

## 最短运行路径

先准备一份自然语言意图文件，描述驱动类型、使用场景、必须保留的行为、排除范围、约束和成功标准。最小模板如下：

```text
驱动类型与使用场景：
必须保留的行为：
明确排除的范围：
不可妥协的约束：
完整迁移怎样算成功：
已知目标 OS、硬件与环境：
本轮准备阶段的特殊要求：
```

首次运行需要提供源驱动目录、目标 OS 目录和工作区：

```bash
python3 porter/main.py prepare \
  --linux-driver /path/to/linux/drivers/example \
  --target-os /path/to/target-os \
  --output-dir /path/to/workspace \
  --intent-file /path/to/intent.md
```

准备阶段完成后，按顺序运行：

```bash
python3 porter/main.py pre-mono --output-dir /path/to/workspace
python3 porter/main.py mono --output-dir /path/to/workspace
python3 porter/main.py accept --output-dir /path/to/workspace
python3 porter/main.py accept --output-dir /path/to/workspace --execute
```

`prepare` 保存输入、建立目标 OS 骨架、验证骨架构建与载入，并生成迁移规划。`pre-mono` 把规划拆成有依赖顺序的模块输入。`mono` 逐模块完成研究、翻译和四段 gate 验证。`accept` 生成七节验收标准，`--execute` 执行这些标准并归档证据。

## 工作区和目标树

Porter 将目标 OS 的代码修改直接写入 `--target-os` 指向的源码树；计划、状态、日志和验收文件写入 `--output-dir` 指向的工作区。目标树必须可写，Porter 不替用户切换分支、重置修改或提交与迁移无关的代码。

首次 `prepare` 使用空工作区。后续命令只需要 `--output-dir`，工具会从工作区状态和成功交接中恢复当前阶段。

## 主要交付物

- 目标 OS 中的原生驱动骨架或迁移代码；
- `runner.md` 与 `runner.json`：构建、启动、设备注入和单元测试的执行记录与机器契约；
- `migration-plan.md/json`：迁移范围、依赖顺序和验证策略；
- `module-division.md/json`：模块划分、源文件归属和依赖；
- `exp-mono/`：模块研究交付物、ledger、日志、修复记录和报告；
- `exp-accept/`：七节验收标准、消费脚本、执行日志和终态报告；
- `knowledgebase/`：调查事实、目标 OS 接入方式、构建/启动经验和待办；
- `handoffs/` 与 `state.json`：阶段边界、输入指纹和续跑依据。

每次阶段命令会自动启动独立的后台 monitor。它读取 `events.jsonl` 和阶段
ledger，生成 `taskboard.md`；超时会记录日志原因并尝试用原 session 续跑一次，
失败或 blocked 会执行一次有界诊断。自动处理耗尽后生成 `HUMAN.md`；人类把
prompt 写在该文件标记下方后，monitor 会消费并继续运行。

## 常用选项

```bash
# 只校验并保存输入，不调用 agent
python3 porter/main.py prepare ... --prepare-only

# 只跑一个模块；模块名必须来自 migration-plan.json 的 order
python3 porter/main.py mono --output-dir <ws> --module <module>

# 只跑一个模块的研究段
python3 porter/main.py mono --output-dir <ws> --module-research <module>

# 覆盖总预算或两个 mono 子阶段的预算（秒）
python3 porter/main.py mono --output-dir <ws> \
  --budget <sec> --budget-research <sec> --budget-translate <sec>

# 续接被中断的 agent 会话
python3 porter/main.py mono --output-dir <ws> --session <session-id>
python3 porter/main.py accept --output-dir <ws> --session <session-id>

# 手动运行一次 monitor（阶段命令已自动启动时通常不需要）
python3 porter/main.py monitor --output-dir <ws> --once
```

`mono` 运行前必须在 `porter/config.json` 的 `models` 中配置带 provider 前缀的 `reasoning` 和 `coding` 模型。`PORTER_MODEL` 可覆盖其他流程使用的默认模型。

## 返回码和测试

- `0`：当前命令成功完成；
- `1`：agent、模块 gate 或终局验证失败；
- `2`：输入、配置、前置交接或工作区不满足要求；
- `3`：准备阶段阻塞，或验收关口等待人工处理；
- `130`：用户中断。

运行本地测试：

```bash
python3 -m unittest discover -s tests
```

测试使用本地 agent 替身，不访问真实模型、容器或目标 OS；真实迁移仍需在目标环境执行并审阅证据。
