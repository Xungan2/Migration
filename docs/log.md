# log 子系统（统一观测框架）

> 状态：核心框架 + print 全量收编 + 上下文接续 API 接入均已落地
> （实现地图见 §10；真实 e2e 验证待下次实际迁移轮次顺带完成）。
> 规范以本文为准，代码与本文冲突时以代码为准并回改本文。

## 1. 目标与原则

log 子系统服务五类需求：

1. **全量记录**：所有运行信息（相位推进 / agent 调用 / 命令执行 /
   判定结论 / 人工介入）落可查询的结构化流；
2. **debug 与 resume**：统一时间线查询（CLI），"上次停在哪、当时
   发生了什么"一查即得；
3. **agent 上下文接续**：正式 API 取"上一次 agent 运行的输入/输出/
   结局"，供错误处理与两轮 agent 之间的上下文衔接；
4. **编译/测试证据留存**：每次 build/boot/ut 的双信号判定与日志
   永久可溯（失败另有不可变快照束）；
5. **服务其他子系统**：gates / knowledge / routing / §15 重设计
   （TODO #3）按稳定事件族与查询 API 消费。

**硬边界：本子系统纯静态脚本，永不调用 agent。** agent 仅作为日志
素材的生产者（输出原文 / 自报字段由静态代码解析入库）；需要 agent
的"读"（如未来诊断）是本子系统的消费者，不是组成部分。

设计原则：

- **流与账本分离**：events.jsonl 是唯一 append-only 事件流（真值
  源）；域账本（gates.json / policy_hits.json / INDEX hits / 候选
  账）保持域所有，其事件照常流入，状态语义不收编；
- **一次调用双 sink**：record() 同时落 console（人读）与
  events.jsonl（机读）；
- **schema 只增不改**：存量字段名冻结，新能力走附加字段，旧 jsonl
  行永久可读；
- **观测纪律**：记录永不抛异常；字符串字段截断 400 字符
  （`_MAX_FIELD`）；append-only 永不改写；
- **分层**：`porter/log/` 位于依赖图最底层，不 import 任何相位模块
  （修复原 env/common 反向延迟 import loop.events 的分层倒置，
  审计 H25）。

## 2. 目录框架

### 2.1 脚本侧

```
porter/log/
├── __init__.py    # 对外唯一进口（稳定 API 面）
├── core.py        # record() 唯一写入口：双 sink 分发 + 上下文戳
│                  #   + 派生助手 phase_begin/phase_end/judge
├── console.py     # console sink（[porter] 行渲染 + 级别阈值）
├── store.py       # events.jsonl 读写 + 进程级 bind
├── query.py       # 查询 / run 登记 / context_block / timeline
└── snapshot.py    # 失败即快照（不可变证据束）

porter/loop/events.py  # 兼容门面（re-export；旧调用点零改动）
```

### 2.2 工作区侧（运行时产物）

```
<ws>/
├── events.jsonl                    # ① 事件流（唯一 append-only 机读流）
├── failure-snapshot-<n>/           # ② 失败快照束（不可变）
│   ├── manifest.json
│   └── qemu.log / qemu-serial.log / …（可裁剪，见 §7）
├── P<n>/logs/（P3/P4 为 P<n>/<M>/logs/）
│   ├── <STEM>_R<尝试>.log          # ③ agent 输出 / 命令输出（纯文本）
│   └── <STEM>_R<尝试>.prompt.md    #    agent 输入归档（与 .log 成对）
├── P<n>/reports/*.json|*.md        # ④ 域结构化产物（域所有）
└── gates.json / policy_hits.json … # ⑤ 域账本（域所有）
```

## 3. 文件格式（五类）

### 类 1：事件流 events.jsonl（JSONL，每行一条，append-only）

信封字段（v1.1；前 8 个存量冻结，后 6 个附加）：

| 字段 | 型 | 存量/新 | 说明 |
|---|---|---|---|
| time | str | 存量 | ISO8601 毫秒 |
| kind | str | 存量 | 事件类型（§4 注册表） |
| subject | str? | 存量 | 对象 id（模块/API/判据/关口/切片） |
| intent | str? | 存量 | 意图（agent 调用 = log stem） |
| cmd | str? | 存量 | 命令 / prompt（截断） |
| rc | int? | 存量 | 退出码 |
| summary | str? | 存量 | 摘要（截断） |
| mount | str? | 存量 | = phase 旧名（保留兼容） |
| phase | str? | 新 | p0..p7 / loop / d1 / kb（缺省回落 bind 的 mount） |
| module | str? | 新 | 模块名 |
| step | str? | 新 | 步骤（fill/migrate/…） |
| attempt | int? | 新 | 尝试号 |
| level | str? | 新 | warn/error（info 缺省不落） |
| run_id | str? | 新 | 外键 → 原始日志对（= log stem） |
| ref | dict? | 新 | {log, prompt, report} 相对 ws 路径 |

示例行：

```json
{"time":"2026-09-03T10:12:33.412","kind":"phase_begin","mount":"p4","subject":"rx-ring","summary":"p4(rx-ring) fill+migrate 开始","phase":"p4","module":"rx-ring"}
{"time":"2026-09-03T10:13:01.201","kind":"agent_start","mount":"p4","intent":"P4/rx/logs/MIG_a.c_100_R1","cmd":"prompt…","summary":"model=zhipu-ai/glm-5.2","phase":"p4","module":"rx-ring","step":"migrate","attempt":1,"run_id":"P4/rx/logs/MIG_a.c_100_R1","ref":{"log":"…R1.log","prompt":"…R1.prompt.md"}}
{"time":"2026-09-03T10:15:02.110","kind":"judge","mount":"p4","subject":"P4_rx_build","intent":"build","rc":0,"summary":"PASS rc=0 pattern=hit","phase":"p4","ref":{"log":"P4/logs/P4_rx_build.log"}}
{"time":"2026-09-03T10:15:30.001","kind":"gate-veto","mount":"loop","subject":"p3.gap.readb","summary":"cp.debt.2 否决：…"}
```

### 类 2：原始日志对（.log + .prompt.md，纯文本）

一次运行一对：`.log` = agent/命令完整原始输出；`.prompt.md` =
发给 agent 的输入原文（保真，不做任何加工）。永不解析、永不改写；
经事件流 `run_id`/`ref` 定位。

### 类 3：快照束（failure-snapshot-<n>/，不可变）

manifest.json 字段：n/time/source/subject/reason/files（copied/
missing/clipped/size，mapping 超 2MB 转 hash-only）/kernel（哈希，
不复制镜像）/qemu_cmdline。

### 类 4：域账本 / 域报告（域所有，本子系统不重设计）

仅两条约定：编码统一 UTF-8 + ensure_ascii=False；与事件流的关联靠
ref/subject，不反向耦合。

### 类 5：人读渲染（派生物，永不作真值源）

- console 行：`[porter] <scope>: <text>`，scope ∈ {P0..P7, T3,
  loop, gates, kb, agent, probe, …}（存量 301 处 print 的既有约定，
  本子系统定型）；级别标记（⚠️ 等）写在 text 内；
- CLI 视图：`porter log tail|runs|show|timeline`（§6）。

## 4. 事件 kind 注册表

**存量族（冻结，字段与语义不变）**：agent_start / agent_end /
cmd_start / cmd_end / snapshot / gate-veto / gate-cluster /
gate-auto-answered / policy-hit / kb-candidate /
boot-log-missing / boot-log-empty / memo / l4-draft /
escalation / diagnose_round / diagnose_round_end
（diagnose_round 族已随 §15 重设计退役——run_diagnosis 被求解循环
替代，2026-09-03；escalation 保留，改由 errorloop 调用）。

**新增族（snake_case）**：

| kind | 产生点 | 用途 |
|---|---|---|
| errorloop_round / errorloop_end | porter/loop/errorloop.py（求解循环每轮/终态） | 错误处理轮次轨迹与结局 |
| phase_begin / phase_end | run_p3/p4/p5 入口与成功出口 | 时间线界标 |
| judge | probe_build / probe_boot | 双信号判定证据流 |
| retry_reset / phase_fail / module_done / bypass_mode / bypass_rejected / bypass_done / parked_remaining / all_done / max_modules_reached / cp3_next / report_written / loop_abort | loop/run.py（print 收编试点） | 循环编排可查询化 |
| t3_verdict | probe_development | P0 门禁判定行 |

新增 kind 的规则：snake_case、语义自明、登记入本表。

## 5. 命名规范

- **STEM**：`<相位>_<任务>_<对象>_R<尝试>`（如
  `P4_rx-ring_MIG_e1000_hw.c_100_R2`）；存量名（T2_category /
  policy_consult / gate_answer / P1S_R1 / …）冻结沿用；
- **run_id** = 去 extension 的日志相对路径（如
  `P4/rx-ring/logs/MIG_a.c_100_R1`）——零新命名空间，人可 grep，
  事件流 ref 直接指向；
- **prompt 配对**：`<stem>.prompt.md` 与 `<stem>.log 同目录同名。

## 6. API 参考

```python
from porter import log

# 写（唯一入口；双 sink）
log.record(kind, subject=…, summary=…, scope=…,          # console 行可由
           console_msg=…, console_only=False,             # scope+summary 派生或
           store_only=False, level="info",                # console_msg 逐字给出
           phase=…, module=…, step=…, attempt=…,
           run_id=…, ref={log,prompt,report}, **extra)
log.console_only(scope, text, level)                      # 纯 console
log.console_line(line, level="info")                      # 整行直打（print
                                                          # 扫尾统一映射）
with log.ctx(phase="p4", module="rx", attempt=2): …       # 上下文戳
                                                          # （显式>ctx>bind）
log.phase_begin / phase_end / judge                       # 派生助手

# 存（兼容面，等价旧 events.py）
log.bind(ws, mount) / log.unbind() / log.append_event(…)
log.read_events(ws) / log.tail_events(ws, …)

# 据（查询面；全部为事件流派生读）
log.query.events(ws, kind_prefix=…, subject=…, phase=…, module=…, run_id=…)
log.query.runs(ws, subject=…, last_n=…)      # agent 运行登记（配对）
log.query.context_block(ws, subject, includes=("outcome","log_tail"),
                        tail_lines=40)       # 上下文接续（拼 prompt 用）
log.query.timeline(ws, module=…)
log.query.tail_text(text, lines)             # 尾部 N 行共享格式器
log.query.tail_block(ws, log_path, lines, title, note="")
                                             # 日志文件尾部块（prompt 注入）
```

CLI：

```
porter log --output-dir <ws> tail   [--kind K] [--subject S] [--module M] [--phase P] [-n N]
porter log --output-dir <ws> runs   [--subject S]
porter log --output-dir <ws> show   <run_id>[-n 尾行]     # 元数据+日志+输入头
porter log --output-dir <ws> timeline [--module M] [-n N]
```

console 级别阈值：环境变量 `PORTER_LOG_LEVEL`（debug/info/warn/error，
缺省 info；低于阈值的 console 行跳过，store 不受影响）。

## 7. 体积纪律

实测一次完整迁移（16 模块）全工作区 46MB，与磁盘容量差两个数量级
以上；各面上界：

| 项 | 量级 | 闸 |
|---|---|---|
| events.jsonl | 每行 ≤1KB，全程几 MB | 字段截断 + 事件数天然有界 |
| prompt.md | 10~100KB/次调用 | 调用次数被 attempts/预算 panic 截断 |
| .log | 实测最大 ~150KB | 存量既有 |
| 快照 | qemu.log 可达 MB 级 | 内核只存哈希；mapping>2MB hash-only；**单文件>5MB 裁剪复制（头 1MB+尾 2MB，manifest 记 clipped:true）** |
| agent 空转灌水 | — | attempts 烧穿 + 墙钟预算 panic（域层，非本子系统） |

不删任何证据（保留策略留 config 旋钮位，默认永不删）。

## 8. 兼容与迁移

- `porter/loop/events.py` 为 re-export 门面（bind/append_event/
  read_events/tail_events/note_*/take_failure_snapshot 签名不变），
  14 个存量调用点与行为级测试零改动；
- 旧 events.jsonl（无附加字段）永久可读（缺键 = 无该维度）；
- gates / knowledge 两子系统 §3.9 声明的事件族不变；
- print 收编：**已完成**——试点（loop/run.py + env/probe.py）走
  record()（事件+console）；其余 284 处（30 文件）经机械 codemod 统一
  映射为 `_log.console_line(<原表达式>)`（byte 兼容，获得
  PORTER_LOG_LEVEL 门控与单一咽喉点）；非 [porter] 前缀的用法提示行
  有意保留 print。新增输出一律走 record/console_only/console_line。

## 9. 对其他子系统的接口

| 消费者 | 接口 | 状态 |
|---|---|---|
| gates（§3.9） | gate-* / policy-hit 事件族 + panic→快照 | 不变 |
| knowledge（§3.9） | kb-candidate 事件 + CP5 健康报告（读域账本，不读流） | 不变 |
| §15 重设计 → 错误处理模块（TODO #3，2026-09-03 落地） | query.events（judge 史/相关事件）+ runs/context_block（轮间接续）+ tail_text/tail_block（prompt 注入） | **已接入**（errorloop 证据组装与轮间上下文） |
| P4 重试 | tail_block（err_info 构建）+ context_block | 已接入（tail_block；context_block 供跨 run 场景随用随取） |
| TODO #6 kb_consulted 遥测 | query.events(kind_prefix="gate-auto-answered") 等 | 预留 |

## 10. 实现地图与测试

| 模块 | 职责 | 关键公共面 |
|---|---|---|
| porter/log/core.py | 写入口 + 上下文戳 + 派生助手 | record / console_only / console_line / ctx / phase_begin / phase_end / judge |
| porter/log/store.py | events.jsonl 读写 + bind | append_event / read_events / tail_events / bind / note_* |
| porter/log/console.py | console sink | emit / emit_line / format_line |
| porter/log/query.py | 查询/登记/接续 | events / runs / context_block / timeline / tail_text / tail_block |
| porter/log/snapshot.py | 失败快照 | take_failure_snapshot |
| porter/main.py cmd_log | CLI 查询面 | porter log tail/runs/show/timeline |

测试：tests/test_log.py（core/console/console_line/上下文戳/v1.1 字段/
run 登记/prompt 归档/CLI/judge/界标/快照钳制/run.py 事件/tail 助手/
module 戳）；tests/test_events.py（存量行为：append/read/tail/快照/
埋桩——经门面跑，验证兼容）。
