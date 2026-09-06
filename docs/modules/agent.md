# agent 调用模块（agent）

> handoff 生命周期、依赖门控和失败恢复见
> [`../sub-systems/handoff.md`](../sub-systems/handoff.md)。生产 agent 调用均在
> 已 prepare 的 task execution 内运行。

> **定位**：非交互 agent 调用的统一接口层——在 `run_agent`（单发）
> 之上提供两种增强形态：`run_agent_structured`（单发 +
> 结构化输出校验 + 反馈重试）与 `run_agent_seq`（**split_long_op**：
> agent 段 × N + 外部静态段交织；长操作时间不吃 agent 预算；段间
> opencode session 续接，无信息损失）。
> 设计定案：2026-09-04（与用户逐点讨论确定，见 §5 定案记录）。
> 实现：`porter/common/agent.py`（纯 Python 标准库，零第三方依赖）。

---

## 1. 定位与设计原则

1. **长操作不吃 agent 预算**（本模块的存在理由）：一次 agent 调用
   里若自己跑长操作（编译 10-30 分钟/测试/启动），操作时间会吃掉
   timeout。拆分 = agent 段（思考/改码，受总时间预算约束）× 静态段
   （外部执行，时长在预算之外），agent 按协议请求静态段并接收结果。
2. **无信息损失续接**：段间续接的目标语义 = "像在同一次非交互
   agent 跑的一样"。主路径 opencode `--session` 原生会话续接
   （1.18.27 实测：非交互下可用，记忆携带经暗号回溯验证）；解析
   不到 session id 时兜底为"模仿交互式对话轮次"的 transcript 注入。
   注意"无损失" = 与单次长会话一致——包含 opencode 自身在超长
   历史时的 compaction 行为。
3. **指针优于载荷**（2026-09-04 定案）：静态段结果不注入消息——
   完整输出落盘 `<stem>_S<n>_static.log`（随 vcs agent 隔离点入库），
   消息只给 verdict + 文件绝对路径；agent 按需自读（tail/grep 自选
   窗口）。写盘失败降级回尾 40 行注入（防死指针）。
4. **统一 handoff 钩子**：`run_agent`、`run_agent_structured` 和
   `run_agent_seq` 都读取当前 task execution；fresh session 先注入必读
   handoff/materials，随后把实际投递版本和 provider 原始证据归档。
   老 skill 文件不需要改输出格式（框架兼容
   `{"status":"done"|"blocked"}` 契约）。
5. **防打转用针对性机制，不设轮数上限**（定案）：同签名早退
   （连续 2 次静态失败规范化签名相同 = 零进展）+ 上下文保证
   （每段必带完整上下文，杜绝重复已做的事）；总时间预算兜底一切
   形态的空转。

## 2. 术语登记表

| 术语 | 定义 | 备注 |
|---|---|---|
| **agent 段** | 一次 `opencode run` 调用（思考/改码/请求静态段） | 每段受剩余预算约束 |
| **静态段** | 编排器外部执行的长操作（编译/测试/启动皆可） | `static["fn"]() -> (ok, output_text)`；时长在 agent 预算之外；fn 自带超时 |
| **phase 协议** | agent 段末尾必输出的 ```json 块：`{"phase":"run_static","message":...}` 或 `{"phase":"done",...}` | 消息末尾有且只有一个 |
| **status 兼容** | 老 skill 契约 `{"status":"done"|"blocked",...}` 等价识别 | blocked 短路 schema 校验、携带 status 交还调用方走既有 panic 流程 |
| **session 续接** | `opencode run --session <id>` 续同一会话 | 主路径；事件流 `sessionID` 字段解析（`--format json`） |
| **兜底 transcript** | 解析不到 session id 时，prompt 内注入"任务原文 + 用户/助手交替轮次 + 新消息" | 模仿交互式对话形状；最近 8 轮全量（单轮截 1500 字），更早压一行 |
| **结果指针** | 静态段结果的传递形态：verdict + `<stem>_S<n>_static.log` 绝对路径 | 零内容注入；文件随 vcs agent_pre/post 隔离 commit 入库 |
| **总预算** | `agent_budget_sec`：所有 agent 段共享的时长上限 | 原 timeout 语义的泛化；每段 timeout=剩余额 |
| **同签名早退** | 连续 2 次静态失败输出经规范化（去 ANSI/路径→basename/时间戳→TS/独立数字→N，尾 40 行）哈希相同 → `stalled` | 与 errorloop.failure_signature 同算法本地实现（防 common→loop 层倒置） |
| **final_static** | done 后编排器再强制跑一次静态段的终验选项 | 失败带结果回循环；仿 p4 老 probe_build 后验语义 |
| **隔离点** | 每段前后 `vcs.agent_pre/agent_post` 工作区 commit 对 | 静态结果文件由此自然入库（框架不自建 git 机制） |

## 3. 协议规范

### 3.1 运行模型

```
run_agent_seq(task_prompt, workdir, log_stem, *,
              static={"describe", "fn"},        # 静态操作（通用长操作）
              agent_budget_sec=1200,            # agent 段总时间预算
              gen_schema={"字段": "str|int|list|dict"},
              final_static=False,               # done 后强制终验
              model/task 同 run_agent) -> outcome

循环（无轮数上限；预算/签名两道护栏）：
  agent 段：发消息（首段=任务原文+运行协议；续段=增量消息）
            → --format json 事件流 → 解析 {session_id, 最终消息}
  段输出 run_static → 执行 static["fn"]（预算外）→ 完整输出落盘
                    → 下一段消息 = 指针块（verdict+路径）
  段输出 done     → blocked？短路交还调用方
                  → gen_schema 校验（缺字段/类型错 → 反馈重试）
                  → final_static？再验一次，失败回循环
                  → 通过：返回 outcome.status="done"、parsed=done JSON
  段输出不可解析  → 反馈"未见合法 phase JSON"重试（烧预算）
  预算耗尽 → budget-exhausted；连续同签名静态失败 → stalled
```

outcome：`{"status": done|stalled|budget-exhausted|failed|no-agent,
"session_id", "fallback", "rounds":[{seg, stem, rc, elapsed_sec, phase,
schema_errs, static:{ok, sig, log}}], "parsed", "total_agent_sec"}`。
轮次账落 `<log_stem>.seq.json`。

### 3.2 phase 协议与输出契约

- **协议注入**（`_seq_preamble`，每次调用动态生成拼在任务末尾）：
  ① 禁令——"禁止你自己执行「<describe>」（含任何等价命令）"；②
  run_static 请求格式；③ done 格式（含 gen_schema 字段）；④ 每轮
  末尾有且只有一个 JSON 块。
- **status 兼容**（`_parse_phase`）：识别 `{"phase":...}` 新协议与
  `{"status":"done"|"blocked"}` 老 skill 契约——17 个 skill 文件
  零改动。blocked 是停车信号不是完成任务：**短路 schema 校验**，
  携带 status 原样返回调用方（p4 映射到既有 `p4.blocked` panic 关口）。
- **schema 校验**（`_validate_schema`）：必填字段 + 浅类型
  （str/int/list/dict；bool 不算 int）；失败反馈自动重试。

### 3.3 段间接续

- **主路径**：首段后拿到 `session_id`（事件流 `sessionID` 字段），
  后续段 `opencode run --session <id> <增量消息>`——模型看得到自己
  全部历史（含工具调用与推理轨迹），与单次长会话行为一致。
- **兜底**（session id 解析不到 → `fallback=True`）：每段全新会话，
  prompt = 任务原文 + 协议 + transcript（用户/助手交替轮次）+ 新消息
  + 预算余量提示。助手轮文本取事件流最终消息（不可解析时原始输出
  尾 20 行）。
- **续接消息**（主路径）：静态结果指针块 + 预算余量一行，仅此——
  任务/协议/历史全在会话里，天然零重发。
- provider 非零退出、超时或中断会结束当前 task execution 并写
  `handoff-fail.md`；session ID 只作诊断证据。下一次任务尝试使用新的
  execution 与 fresh session，首轮会读到该失败文档。静态 build/test
  失败仍可在同一个 `run_agent_seq` session 中把结果指针送回修复。

### 3.4 静态段与结果指针

- **fn 契约**：`() -> (ok: bool, output_text: str)`；自带超时
  （编排器不代管——如 probe_build 用 runner 配置超时）；异常按
  失败处理（`静态段异常：<repr>`）。**复合操作**允许：一个 fn 串
  多步（p4 接线 = probe_build + 行数守卫）。
- **结果文件**：`<log_stem>_S<seg>_static.log`，与段日志同目录成对；
  round_rec["static"]["log"] 记入 seq.json。写盘 OSError → 降级回
  `_static_result_block` 尾 40 行注入（观测面不打断）。
- **入库**：文件落在相邻段的 vcs agent_pre/post 隔离 commit 里
  （定案：框架不自建 git 提交机制）。

### 3.5 预算与防打转

| 护栏 | 机制 | 范围 |
|---|---|---|
| 总预算 | 每段耗时累计，剩余≤0 → `budget-exhausted`；每段 timeout=剩余额 | 兜底一切空转形态 |
| 同签名早退 | 连续 `SEQ_SAME_SIG_REPEAT=2` 次静态失败签名相同 → `stalled`；成功重置 | "同一错误修不动" |
| 上下文保证 | session 续接/兜底 transcript（每段必带完整上下文） | 预防"重做已做的事" |
| （接线层）跨切片签名 | p4 保留老 sig_counts：失败切片读构建日志尾朴素哈希，跨切片连发 → 既有 `p4.slice_sig` panic | 换片打转 |

### 3.6 观测面

每段：`.log`（JSON 事件流全文）/`.prompt.md`（消息原文）/
`agent_start`/`agent_end` 事件（run_id=stem，v1.1 结构字段）；seq 末
`agent_seq_end` 事件 + `<stem>.seq.json` 轮次账（段数/phase/耗时/
签名/结果文件路径/fallback 标记）。vcs 隔离点同 run_agent。

### 3.7 run_agent_structured（单发形态）

`run_agent_structured(prompt, workdir, log_stem, *, gen_schema,
max_tries=2, validator=None) -> (rc, out, parsed)`：run_agent + done 协议 +
schema/业务 validator 校验。每个 provider 重试各有独立 child execution；
`blocked` 原样交还调用方，但该次 child 记为失败，不能提前发布成功。

## 4. 实现地图（改进参考）

`porter/common/agent.py` 函数分组（run_agent 之下新增约 580 行）：

| 组 | 函数 |
|---|---|
| 内部调用器 | `_opencode_json_runner`（--format json + --session；归档/事件/vcs 隔离同 run_agent 约定） |
| 消息通道 | 两个 runner 的消息一律经 **stdin** 传 opencode（argv 无消息元素，2026-09-05 定案）：无 128KiB 单参数上限、无 `-` 开头被当选项的歧义（argv+`--` 为历史中间方案，已被取代）、消息逐字 verbatim——argv 路径会给含空格消息包字面引号（run.ts `resolveRunInput` 实证）。live 验证 1.18.28 三检全过（事件流 / --session 续接 / verbatim）。**stdin 属未文档化行为：opencode 升级后需复验** |
| 事件解析 | `_parse_events`（JSONL → {session_id, text}；字段变体/噪音行防御式兼容） |
| 协议解析 | `_parse_phase`（phase 新协议 + status 老契约 + 裸 JSON 兜底） |
| 校验 | `_validate_schema`（必填+浅类型；bool≠int） |
| 防打转 | `_static_sig`（规范化哈希；errorloop 同算法本地实现） |
| prompt 块 | `_seq_preamble`（禁令+协议）/ `_static_pointer_block`（指针）/ `_static_result_block`（降级尾块）/ `_transcript_block`（兜底轮次） |
| 主入口 | `run_agent_seq` / `run_agent_structured` |

**接线范围**：P0 环境提取把 schema、runner 校验和三项 probe 放在同一
provider-round child 内；P1 按源文件划分、解环与 pruning 单次方案独立记录；
P2 mapping 按稳定符号集 ID 串接，scaffold/probe 也发布各自 handoff；P3
mapping、gap、criteria、probe/rejudge/fix 都是可查询 child；P4 fill API 和
每个源码切片独立记录，切片内部由 `run_agent_seq` 共享一次 execution；P5
补探、P6 L4、errorloop 每轮、routing 与 review 辅助调用同样走该协议。
phase/CLI execution 是聚合记录，不能代替这些 session 级 child。

probe generation/compile-fix/rejudge 的 schema 通过只发布 proposal。随后每轮
host build、boot、日志 judge 由稳定 `probe.<owner>.validation` task 发布真实终态；
失败 validation handoff 是下一 repair session 的显式输入。P4 fill 也使用独立的
`.proposal` 与 `.validation` task，避免 provider `rc=0` 抢先声称补齐成功。

P4 断点重跑只复用当前 artifact 指纹仍与既有成功 handoff 一致的 fill/切片；
不会从 `migration.json` 单独推导成功。失败切片的新 session 同时读取前一切片
成功 handoff 和自身最近失败 handoff。

| 级别 | 调用点 | 覆盖物 |
|---|---|---|
| ✅ 已接线（1） | P4 migrate 切片循环 | run_agent_seq（真实重迁 os-probe + P5 判据级 55/55 验证） |
| ✅ 已接线（2026-09-05） | P2b scaffold（发现轮） | 直连 `_opencode_json_runner`：单 session 贯穿回炉轮（--session 续接只发证据指针）；recipe 走**文件输出**（agent 写裸 JSON 到编排器下发路径，消息只回"已写入"——免疫 stdout 提取的截断/围栏坑）；输出质量问题（缺文件/坏 JSON/校验缺陷）同轮微增量续接 ≤2 次不烧轮；session 解析不到 = 静态 panic（RuntimeError，非人工关口）；超时/非零 rc 先经部分事件抢救 session_id |
| ✅ 已接线（2026-09-05） | P0 T3 环境提取 v2 | 同款直连形态扩展为**四 session 流水线**（build→boot→inject→unit_test，extract.py）：每能力一个 session；测试请求协议（agent 写 `{"cmd"}` 文件 → 编排器静态执行 → 结果文件指针续接）；片段双产物（runner.json+runner.md，md 锚点校验；md 为节 JSON 的扩充是软性写作指引，2026-09-06 起无尾块等值机械检查）+ 终验单 probe；质量失败不烧轮；耗尽 → 同 session 总结请求 → panic 关口 p0.t3.\<cap\> → 人答后新 session 种子（总结+答案）+ 小额资源；state 断点续跑（t3_state.json）。**不走 run_agent_seq / phase 协议**——文件即信号路线定案（TODO #11/#17） |
| 🟢 原生场景（2） | 探针 FAIL 回炉 / errorloop 求解轮（_prev_context 可被 session 续接替代；verdict 七动作词表需 gen_schema 适配） | run_agent_seq 或同款直连形态（各需把真实执行包成 static fn/静态执行器） |
| 🟡 机械可换（9） | P0 T5 烟测反馈 / P1D 划分 / P1R 解环 / P2a 映射 / P3 增量映射+gap+判据（3 处）/ P4 fill / P5 补探 / P6 draft-l4 | run_agent_structured |
| ⚪ 无必要（5） | P0 T2 类别 / P1S 策略 / routing 关口应答（2 处）/ CP5 分类 | 纯单发，老接口够用 |

**接线前置缺口**（🟡/🟢 类做之前需补框架）：
1. **通用 done 识别**（主缺口）：fill 的 `{patch_summary,...}`、
   P5 补探的 `{cmd,...}` 都没有 phase/status 键，`_parse_phase` 会
   误判不可解析——需 `done_key` 参数（"```json 块含此键即 done"）。
2. **不可重试中止通道**（仅 fill 三重验证进 seq 时需要）：boot 日志
   不可得今天是 exit 3 停车语义，静态段需能表达"infra 中止"而非
   可重试失败。
3. **多操作菜单（statics 集合）**：已与用户讨论（封闭菜单+窄参数
   +禁令只覆盖菜单），2026-09-04 定案搁置待需求落地。

## 5. 已知限制与定案记录

**限制**：
- 单任务单静态操作；多操作集合（封闭菜单/op 分发/args 形状护栏）
  已设计未实现（搁置）。
- 禁令为 prompt 级，无技术强制——保证来自"正规路径严格更好"（不烧
  预算+有判定+有结果文件）+ 工具调用转录留档可审计。
- 同签名槽只有连续两个（A→B→A 交替不触发早退，靠预算兜底）；
  agent 段空转（反复不可解析）无签名，纯靠预算。
- `run_agent_seq` 的 provider session 不跨进程恢复；宿主崩溃由 handoff
  recovery 先补写失败文档，重试从新的 execution/session 开始。已经验收且
  artifact 指纹一致的 P4 前序切片可通过自己的成功 handoff 复用。

**设计定案记录**（2026-09-04 与用户逐点确认）：
1. 先保底 split_long_op，包装成类似 run_agent 的一次函数调用，
   不改主体控制流；plan_execute 编排模式暂缓。
2. timeout 语义改为 agent 段总预算；静态段时长移出限制。
3. 防打转不设轮数上限——用签名比对 + 上下文保证两个针对性机制
   （用户提议，替代 max_rounds）。
4. 上下文默认组装 = session 续接（主）+ 静态结果（唯一新信息）；
   不引入 agent 自总结 summary 链（会话记忆使其无必要）。
5. **指针优于载荷**：不注入结果内容；verdict+路径；agent 自读。
6. git 提交不在 run_static 里做——按既有 vcs agent 调用隔离粒度
   自然入库，框架零新增提交代码。
7. 框架兼容老 skill 输出契约（A 方案），skill 文件零改动。
8. 静态段触发 = agent 请求（run_static）+ 可选 final_static 终验。
9. 多操作集合：封闭菜单 + 窄参数 + 禁令只覆盖菜单（菜单外 agent
   自跑花自己预算）——设计已定，实现搁置。

## 6. 测试

- `tests/test_agent_seq.py`（38 例，全 mock 零真实 agent）：
  事件解析（session/文本/字段变体/噪音）、schema 校验（缺字段/
  类型/bool≠int）、phase 解析（新协议/status 兼容/blocked 短路）、
  签名稳定性（路径/时间戳/ANSI 碎改动不翻转）、主路径（session
  续接+指针纯净度+结果文件完整性+seq.json）、兜底 transcript、
  预算耗尽/no-agent/opencode 缺失快败、stalled/成功重置、静态段
  异常、final_static 通过与回环、schema 反馈重试、不可解析反馈、
  写盘失败降级尾块、preamble 禁令与文件路径提示。
- `tests/test_agent_seq_live.py`（2 例，opt-in：
  `PORTER_LIVE_AGENT_TEST=1`）：**暗号回溯**（段 1 埋随机暗号 →
  `--session` 续接后问答——记忆携带的无信息损失证明）；**指针
  e2e**（token 只存在于结果文件，agent 必须自读才能写进 done.notes；
  另断言所有段消息零 token 泄漏）。
- **实战验证**（2026-09-04，e2e-test-retry 工作区副本 + git worktree
  隔离）：os-probe 真实重迁——S1（1214s，超老单段上限不超时）→
  run_static → build 1m20s（预算外）→ S2（33s，2.3KB 增量消息）
  → done + 终验 → 轮末冒烟 PASS；P5 判据级验收 55/55（含 3 条 L3
  真实设备正则）。过程中 L3 首败暴露 infra 回归（qemu_args.sh 设备
  注入钩子随当年未提交改动丢失），求解循环（老接口）与人工独立
  诊断出同一根因并修复——新老接口混跑自洽。对照：老接口同任务
  3 次独立调用（R1 整轮 TIMEOUT 报废 1200s）+ 全量 prompt 重发 +
  err_info 手拼反馈。
