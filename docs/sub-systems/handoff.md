# handoff 子系统

handoff 把 Porter 的任务成果变成下一任务可验证的必读输入。每个逻辑任务有
稳定 `task_id`；每次尝试有唯一 `execution_id`；每次 provider 调用另有唯一
`invocation_id` 和可选 session ID。三者不能互相替代。

缺少、损坏或过期的前置 handoff 时，任务状态为 `not ready`，agent transport
不会启动。旧产物存在不构成例外。历史工作区须通过 `handoff import` 从真实产物
与验收事实补建文档。

## 存储

```
handoffs/tasks/<task-slug>/
├── index.json
└── executions/<timestamp>-<uuid>/
    ├── record.json
    ├── input.md
    ├── delivery.json
    ├── handoff.md | handoff-fail.md
    └── provider/<invocation-id>/{prompt.md,raw.log}
```

execution 目录不复用。索引只推进 latest 指针，旧 execution 与终态文档不覆盖。
写入采用同目录临时文件、`fsync`、原子替换，并由工作区锁串行化。

provider 的 prompt 和原始事件/日志复制到 execution 私有目录并记录 hash，所以
沿用的 `P*/logs/<stem>` 后来覆盖也不影响该次证据。代码目录和普通 artifact 只
保存绝对指针与当时指纹，并不宣称复制整个源码树；需要不可变正文的证据应声明为
文件或另存快照。

## 生命周期

1. 编排器用 `TaskSpec` 显式声明直接依赖与原任务 materials。
   子任务不依赖仍在 running 的 parent，但会把 parent 已固定的前置版本与
   materials 复制进自己的输入计划；因此切换 ContextVar 后不会丢外围阶段材料。
2. `prepare` 校验成功文档、hash、递归依赖版本、环和 materials，创建 running
   execution。
3. 每个 fresh provider session 收到完整 `input.md`。同 session 续接不重复注入；
   只有 session ID 不可用但任务尚未终态的 transcript fallback，才附带本
   execution 早前 provider 段的简短索引。终态重试属于下一 execution，读取的是
   已落盘 `handoff-fail.md`。
4. 注入前把实际文档 ID、execution ID、hash 写入 delivery receipt。planned 与
   delivered 分开。
5. agent 写语义说明；宿主保存原始输出，补充业务返回码、静态验证和 artifact
   指针。只有调用方业务验收通过后才发布 `handoff.md`。transport `rc=0` 不等于
   业务 success。
6. 失败、超时、异常和中断发布 `handoff-fail.md`。没有 agent 总结时明确写
   unknown，不推测未记录工作。

静态 build/test 失败可在 `run_agent_seq` 的同 execution、同 session 内修复。
provider 非零退出或超时属于该尝试的终态，session ID只留诊断，禁止续接；重跑
先读取失败 handoff，再用全新 execution 和 fresh provider session。

同 task 有活跃本机 PID 时拒绝并发启动。进程已消失时，下一次 `prepare` 先为旧
running execution 写崩溃 `handoff-fail.md`，再创建新 execution。异机 running
记录须显式 `handoff recover --force`。

新增任务通过同一个窄接口接入；`success` 必须表达该任务真实的宿主验收，不能只看
provider 返回码：

```python
from porter.handoff import TaskSpec, run_task

result = run_task(
    ws,
    TaskSpec("p8.example", dependencies=("p7",),
             materials=(ws / "project.json",)),
    do_example,
    success=lambda value: value.accepted,
    summary=lambda value: value.handoff_summary,
    artifacts=lambda value: (value.report_path,),
    verification=lambda value: value.validation_facts,
)
```

如果任务内部每次重试都会打开新 provider session，应把 `run_task` 放在重试循环
内并复用稳定 `task_id`。这样每轮各有唯一 execution，后一轮自动读取前一轮失败。

## 依赖与版本

模块 P3 的直接依赖来自 `deps.json.edges[module]`，另显式依赖共享 P2 probe。
共享目标状态（`lib.rs`、全局 mapping/probe、external interfaces）的前置取拓扑中
更早模块里实际已发布的最近 P5/P4 生产者；独立模块绕行不会被一个从未成功发布的
紧前模块误挡。真实生产者已有执行记录时，其文档损坏或版本过期会严格阻止下游，
不会回退到更老 producer；最近失败记录作为显式 failure input 交付。P4 依赖本模块 P3，
P5 依赖 P4。P3 步骤、P4 fill/切片、P2 mapping/probe 批次
按实际消费关系形成子任务链。parent 只表示执行归属，不依赖尚在 running 的父成果。

probe 生成、compile-fix、rejudge 的结构化输出只是 proposal handoff。每轮真实
build、boot 与日志判定另发布 `probe.<owner>.validation` 终态；验证失败先写
`handoff-fail.md`，下一次修复 session 将该失败作为显式输入。P4 fill 同样把
provider proposal 与 build/boot/probe validation 分开，schema 合法不会冒充 fill
验收成功。

新任务选择上游 `latest_success`。若 A1→B1 后发布 A2，B1 会因固定的 A1 被取代
而失效，依赖 B 的新任务不能启动，直至 B 基于 A2 重做。已运行任务开始时固定输入
版本，完成前再核对；执行期间上游变更会让本次结果失败。上游存在新的 running
execution 时，旧 `latest_success` 暂停供下游消费，直到新尝试先发布失败或成功，
避免下游与正在修改共享产物的生产者并发。

普通 material 是必读的“路径+开始时指纹”，允许任务合法修改；
`Material(path, must_remain_unchanged=True)` 还要求终态指纹一致，适用于只读切片
源码。它与不可变 handoff 文档版本不同。

直接前置 handoff 与本 task 最近一次失败全文注入。更早失败和传递上游以文档 ID、
hash、绝对路径进入 bounded index。正文超预算会拒绝启动，不静默截断；handoff
对 provider、child、artifact、验证与 delivery 列表分别设有上限；省略时明确数量并
指向完整 `record.json`。因此大量 fan-out child 不会让正常父任务文档无限增长。

## CLI

```
python3 porter/main.py handoff --output-dir <ws> inspect [--task-id ID] [--json]
python3 porter/main.py handoff --output-dir <ws> import --task-id ID \
  --summary "..." --artifact FILE --verification "验收事实"
python3 porter/main.py handoff --output-dir <ws> recover --task-id ID \
  --reason "宿主终止原因" [--force]
```

`import` 至少要求一个真实 artifact、非空 summary 和 verification，并标记
`historical_import=true`，不能创建无证据的成功声明。

实现集中在 `porter/handoff/core.py`；`integration.py` 保存 Porter task ID 与依赖；
`common/agent.py` 负责 fresh-session 注入、delivery 和 provider 证据归档。阶段编排
发布业务终态，env/divide/bootstrap/loop 的细粒度辅助任务复用同一接口。
