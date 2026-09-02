# 人工介入子系统（gates）

> 本文档定义人工介入子系统（gates）的协议规范与实现参考：第 1 章
> 总览（README 引用此处）；第 2-3 章术语与协议标准；第 4-6 章实现
> 地图、已知限制与演进方向。
> 配套：`TODO.md`（已商定的后续工作）、`porter/config.json`（运行时
> 配置）、`tests/test_gates.py` 等 6 个测试文件（协议的行为级定义）。

---

## 1. 概述

工具能自动完成驱动迁移的大部分工作，但有三种时刻必须人来处理：

1. **需要拍板的时刻（计划内）**。比如"这个驱动功能要不要砍掉""最终
   验收标准定成什么样"。工具在流程里安排了几个固定的检查点，到点就把
   该看的东西整理成一份摘要，你确认后才继续。
2. **工具自己解决不了的时刻（计划外）**。比如同一种编译错误反复出现、
   启动后拿不到日志。工具会立即停下，保存好出错现场，写清"出了什么事、
   需要你做什么"，等你处理后从断点继续——重新运行同一条命令即可，
   已完成的工作不会重做。
3. **环境本身坏掉的时刻**。比如构建命令跑不通。工具修不了你的机器，
   只能报告清楚等你修。

人的操作固定且简单：工具停下时生成一份**待办清单**（human_questions.md），
每条问题下面带着填空表格；你在**答案文件**（answers.md）里照表格填几行，
重跑同一条命令，工具从停下的地方继续。也可以用命令行小工具代填：

```bash
porter gate list --output-dir <工作区>            # 看还有什么没答
porter gate show <关口编号> --output-dir <工作区>  # 看某条问题的全文与历史
porter gate answer <编号> --set 字段=值 --output-dir <工作区>   # 直接作答
porter gate review --output-dir <工作区>           # 生成批量审阅材料
```

设计目标一句话：**把"人盯着工具"变成"工具排队等人"**——你不在场时，
工具能自己推进的就推进，拿不准的先记下来攒着事后批量找你确认；实在
推进不下去才停下。为此系统做了三件事：

- 所有问题登记进同一份**台账**（gates.json），人看的清单只是台账的
  打印件——不会散落、不会互相覆盖；
- 你只负责**表态**（填表格），改文件这种事由工具替你做——你不需要
  知道答案该写进哪个文件；
- 工具拿不准的问题先问**规则**（你事先写好的常备规则，如"凡调试用
  统计类接口一律丢弃"）和 **AI 助手**（照表格试答），都失败才到你
  ——AI 替你答的会记账，事后批量给你复核，可以否决。

---

## 2. 概念与术语登记表

后文所有章节只使用本节登记过的术语。

| 术语 | 白话定义 | 为什么需要它 |
|---|---|---|
| **关口（gate）** | 一条登记进台账的、等人（或自动层）回答的问题 | 全部人工介入的统一抽象：有编号、有表单、有生命周期 |
| **台账（gates.json）** | 工作区根目录下的关口登记簿，唯一事实源 | 人读的清单由它渲染，永不手改、永不互相覆盖 |
| **车道（lane）** | 关卡的两种归属：`panic`（计划外异常停车）/ `checkpoint`（计划内检查点） | 区分"该停的停"与"设计好的批审时刻" |
| **类型（kind）** | 关口五分：`fact`（要事实）/ `decision`（要裁决）/ `approval`（要批准）/ `retry`（要重试授权）/ `memo`（备忘，不阻塞） | 类型决定答案怎么校验、怎么应用 |
| **关口大类（gate_type）** | 三分：`failure`（失败触发）/ `decision`（决策类）/ `physical`（物理环境） | 路由分派用：不同大类的自动层实现不同 |
| **层（tier）** | 谁第一个应答的有序候选：`rules` → `agent` → `human` | 实现"自动优先、人兜底"的排队机制 |
| **检查点（CP）** | 流程里固定的批审时刻：CP0-CP5、FM（首模块）、CP-DEBT（债限额） | 计划内车道的具体停靠站 |
| **决策债** | 被 rules/agent 层自动答掉、等待人事后复核的决策 | 自动化与监督的折中：不即时打扰，但留复核权 |
| **答案表单（answer_form）** | 每个关口自带的填空字段定义（字段名/选项/必填） | 消灭"自由文本猜格式"；机器可校验 |
| **应用器（applier）** | 按答案改"正本"文件的工具侧函数 | 落实"人只表态，工具代改" |
| **恢复（resume）** | 重跑同一条命令即从断点继续（幂等重入） | 人工处理后的回归方式，零新语义 |
| **快照** | 判定失败瞬间的现场抢救（日志/判据状态/镜像哈希，不可变） | 人答关口时有证据可看，不是只有问题 |
| **聚类检测** | 同一关口反复触发（≥3 次）时提示"该升检查点/写规则" | 让车道划分随实战自我校正 |
| **指纹绑定** | 批准类答案附带所审文件的哈希，文件一改批准自动失效 | 杜绝"旧批准一直放行新内容" |
| **§15 bypass** | 失败自诊（triage 分诊 + diagnose 诊断）整体旁路，config 开关默认关 | 该块未达质量预期，用户决策先旁路待重设计 |

---

## 3. 协议规范（标准定义）

### 3.1 账本协议

**位置**：`<工作区>/gates.json`。写入原子（tmp + rename）。
`history` **append-only**——答案与处置永不删除，审计轨迹永久保留。

**条目 schema**（`GateLedger.add()` 全字段）：

```json
{
  "id": "p3.gap.rx-ring.netdev_alloc_pcp",   // 命名见 3.10
  "lane": "panic | checkpoint",
  "kind": "fact | decision | approval | retry | memo",
  "gate_type": "failure | decision | physical",
  "checkpoint": "CP3 | FM | CP-DEBT | null",  // checkpoint 车道的停靠站
  "phase": "P3", "module": "rx-ring", "step": "p4",
  "blocking": true,                            // memo 类恒 false
  "status": "open",                            // 状态机见下
  "question": "…",
  "context_files": ["P3/rx-ring/reports/gap_decisions.json"],
  "answer_form": [
    {"field": "strategy", "type": "enum",
     "options": ["bypass", "fill", "register-fill"], "required": true},
    {"field": "rationale", "type": "text", "required": true}],
  "agent_draft": {"confidence": "high", "policy_hit": "规则ID|null"},
  "answer": {"strategy": "bypass", "…": "…"},
  "answered_by": "human | agent | policy",
  "answered_at": "…",
  "applies_to": {"modules": ["rx-ring"], "files": ["…"]},  // veto 回滚范围
  "artifact_sha": "…16 hex…", "artifact_path": "P6/reports/l4_criteria.json",
  "resolution": "工具如何应用了该答案（自动回填）",
  "asked_at": "…",
  "history": [{"time": "…", "event": "re-asked|invalid-answer|answered|…",
               "detail": "…"}]
}
```

- **前向兼容**：登记时未识别的非 None 键原样保留（如 `target`、
  `subject`——应用器路由字段）。
- **幂等登记**：同 id 再登记不重复建条目，只追加 `re-asked` history。

**状态机**：

```
open ──人答/自动答──▶ answered ──应用──▶ applied ──批审/结清──▶ resolved
 │▲                                    │
 │└── 校验失败回写 ◀── invalid          └──批审否决──▶ vetoed（终态）
 └─（invalid 语义等同 open：阻塞、等重答）
```

**关键查询**：`open_blocking()`（open+invalid 且 blocking——exit 3 判定集）；
`pending_review()`（applied 且 answered_by ∈ agent/policy——决策债）。

### 3.2 答案协议

**格式**（answers.md）：

```markdown
## @<gate_id>
strategy: bypass
instruction: 丢弃 per-CPU 统计
rationale: e1000 单队列，MVP 无消费方，
  续行这样写也可以
```

- 节头 `## @<id>`；字段行 `字段名: 值`（字段名限 ASCII 标识符，
  冒号后为值；全角冒号 `：` 亦可）；
- 无字段前缀的非空行**追加到上一字段**（换行连接）——多行 rationale；
- **校验语义**（`validate_answer`）：必填缺失 / enum 取值非法 →
  关口置 `invalid`，错误进 history 并渲染标红，`@` 节保留待改；
- **消费**：启动时 `process_answered_gates()` 扫描全部 @ 节 → 校验 →
  记账 → 应用。答案进台账后 @ 节从 answers.md 移除（档案在台账）；
  未知 id 的节不动（渲染面提示）。

**批审语法**（对 status=applied 的决策债再作答）：

```markdown
## @<债项id>
verdict: veto          # 或 reject / 否决 → 回滚（见 3.6）
## @<债项id>
verdict: approve       # 结清（resolved）
```

**legacy 兼容键**（过渡期并存，均为旧自由文本协议）：

| 旧键 | 语义 | 现状 |
|---|---|---|
| `## retry <module>[-p3|-p4|-p5]` | 清零 attempts | 保留（state.py 消费） |
| `## <linux_api>` | gap 自由文本答案（强制 bypass） | 保留（p3 兼容路径） |
| `l4_criteria_finalization: approve` 单行 | L4 定稿放行 | 保留（p6 兼容路径） |
| `diagnosis_escalation: approve` / `b_class_autofix: approve` | §15 审核门 | **休眠**（路径不可达） |
| answers.md 全文自由文本 | T3 环境答案 | 保留（无 @ 节内容时的回退） |

### 3.3 渲染协议

`human_questions.md` 是台账的**纯渲染产物**：工具是唯一写者（`render_
human_questions()`），任何代码不得手写/追加该文件。每次登记/消费/校验
失败后重渲染，内容 = 全部 open/invalid 关口（含表单与上次校验错误）+
非阻塞备忘分区。P0/P1 相位目录下的 `human_questions.md` 是**问题内容
材料**（T3 题面、解环环清单），不是交互面——交互面只有工作区根这一个。

### 3.4 应用协议（applier 注册表）

**单一写入者原则**：人只表态；一切"正本"（gap_decisions.json、
deferred.json+criteria.json、loop_state.json、defects.json 等）由应用器
代改。按 kind 分发（`gates._apply`）：

| kind / target | 应用动作 |
|---|---|
| retry | loop_state 对应模块（±step）attempts 清零；诊断笔记进 resolution |
| decision + target=gap | gap_decisions 决策回写（strategy/instruction/answered）+ mapping notes 同步 + confidence=high |
| decision + target=deferred | **双写**：deferred.json 条目 criterion 副本与 P3 criteria.json 正本同步改 expr（根治"改哪个文件"的副本陷阱）；verdict=fix-code 仅记账 |
| decision + 其他 | 记账（rationale 留档供批审） |
| approval | 指纹校验：登记了 artifact_sha 且与当前文件不符 → 拒绝（"草案已变更，请重审"）；verdict 记录 |
| fact / memo | 记账 |

### 3.5 恢复（resume）协议

- **resume = 重跑同一条命令**（工具全程幂等断点重入）；
- **启动消费顺序**：相位入口先 `process_answered_gates()`（@ 节），
  再走 legacy 兼容路径（retry/api 键），最后推进相位；
- **agent 无状态**：每次 agent 调用都是单发；重跑时新 agent 的 prompt
  由盘上三块材料重建——①工作区正本状态 ②上一轮情况摘要（gates
  history + §15 快照 + 升级报告）③人工答案含 rationale；
- **层已消费语义**：相位内已消耗的自动尝试（如 T3 的 3 轮 agent、
  p5 的分诊）记入 history，路由链相应短路（见 3.8 内置默认表）。

### 3.6 检查点协议

| CP | 位置 | 关口 | 默认 | 批审动作 |
|---|---|---|---|---|
| CP0 | p0 末 | cp0.runner_review（memo，非阻塞） | 开（不停车） | 备忘确认 |
| CP1 | p1-strategy 后 | cp1.strategy（**绑定 strategy.md 指纹**） | 开 | verdict approve/reject；`p1` 直通路径同样拦（修审计 H5）；改文件即需重批 |
| CP2 | p2 末 | cp2.mapping_review | **关**（`checkpoints.CP2_enabled`） | 映射抽审 |
| FM | loop 首模块 done 后 | cp.fm.\<M\>（一次性：resolved 后永不再停） | 开 | verdict + note："这套模式可否复制给剩余模块" |
| CP-DEBT | 债限额触达 | cp.debt.\<n\> | 开（限额 30） | approve=批量结清当前债；reject=关口重开逐条处理 |
| CP3 | loop 完 / p6 | p6.l4.finalize（**绑定草案指纹**） | 开 | 草案由 `p6 --draft-l4` 生成；approve 定稿 |
| CP4 | p7 入口 | cp4.defect_review | 开（§15 bypass 下无债可审，自然跳过） | 缺陷闭账批审；单条否决 `## @p6.defect.fix.<ID>` veto |
| CP5 | p7 末 | cp5.promote（memo，非阻塞） | 开 | 晋升提醒 |

**digest**：`checkpoints/<CP>_digest.md`——决策债按 kind 分组表格
（关口/应答者/决策摘要/否决方式）+ 已否决清单。

**veto 回滚语义**（`_rollback_veto`）：标 vetoed → applies_to.modules
的 attempts 清零 + 相位按关口类型回拨（target=gap/mapping→p4、
deferred→p5）→ 重跑自动重新消费。**v1 限制**：已 done 的迁移切片仍被
migration.json 跳过——强制重迁须人工清对应切片记录（digest 中注明）。

### 3.7 panic 协议

**触发者永远是编排脚本**（agent 只是被监控方）。信号目录：

| 信号 | 关口 id | 判定点 |
|---|---|---|
| attempts 烧穿（3 次/模块/相位） | loop.attempts.\<M\>-\<pN\> | run.py |
| agent 单调用超时/零产出 | 不设关口（调用层留痕） | agent.py |
| P4 blocked（映射不可用） | p4.blocked.\<M\>.\<file\>-\<start\> | p4.py，**立即停不烧 attempts**（修 H13） |
| 同签名编译失败连发 ≥2 | p4.slice_sig.\<M\>.\<file\>-\<start\> | p4.py（错误尾 40 行哈希） |
| 模块墙钟超 3600s | loop.budget.\<M\> | run.py |
| boot 日志复探后仍缺 | infra.boot_no_log | probes.py（**抢占判定**，见下） |
| P5 判据 FAIL（不可重跑） | 无独立关口→烧 attempts→loop.attempts | p5.py |
| deferred 到期未清偿 | p5.deferred.\<M\>.\<id\> | p5.py |
| P1 解环 3 轮败 | p1.resolve.cycles | resolve.py |
| T3 提取 3 轮败 | p0.t3.extract | extract.py |
| T5 门禁败（环境坏） | p0.t5.env_gate | main.py |
| T2 类别不可识别 | p0.category.none / unparseable | category.py |

**处置链（开给人之前）**：`panic()` 内先走路由自动应答（3.8，仅
decision 类）→ 命中则转决策债、返回 0（调用方所在相位据此可续跑：
run.py 对 rc==3 复查 open_blocking，空则幂等重进）→ 未命中才登记
open 关口 + §15 快照 + 渲染 + exit 3。

**日志面抢占语义（H9 重构）**：`boot_and_log` 系助手返回
`(ok, log, state)`，state ∈ file/stdout/empty/missing——
- `empty`（来源在、内容空）= **有效失败信号**：判定照常（boot 必
  FAIL），event 标注两种根因假设（console 配错 vs 内核早挂）；
- `missing` → **有界复探一次**（吸收瞬时抖动）→ 仍缺 → infra 关口
  **抢占判定**：本轮全部日志类判据一个不判（消费方 p5/p4/探针/
  p6execute 见 state 即中止，rc 3 不烧 attempts）——判定输入不存在时
  拿空串判 FAIL 是本末倒置。

**聚类检测**：同关口 re-asked ≥3 → 打印 + event 提示"升检查点/写
policy 规则"。

**退出码约定**（全工具一致）：0 成功 / 1 失败 / 2 前置缺失 / **3 需人
工（= 存在 open 阻塞关口）**，退出时打印 `gates.summary_line()` 摘要。

### 3.8 路由协议（三级分流）

**层词汇固定**：`rules / agent / human`。**配置写概念层，实现按
gate_type 分派**：

| 层 | gate_type=decision | gate_type=failure |
|---|---|---|
| rules | policy.md（工作区自然语言常备规则，agent 解释、命中留痕 `policy_hits.json` + event） | 相位内 triage 已消费（§15 bypass 下无） |
| agent | gate-answer skill + **知识库检索**（kb-guide 总纲 + 按关口类型确定性选域注入已审条目目录；答案可附 `kb_consulted` 记 hits；temp 草稿不参与自动应答） | diagnose（§15 bypass 下休眠） |
| human | 人 | 人 |

**自动应答仅作用于 kind=decision**（fact 的 agent 层已消耗于相位内、
retry 的 agent 层=诊断链）。低置信/校验不合格 → 视同未命中，链继续。

**两级配置**：仓级 `porter/config.json` 的 `routing` 节 + 工作区
`routing.json` 覆写（**仅 routing.gates / routing.default 两键**——
其余键的 ws 级覆写见第 5 章限制）。合并：gates 字典 update、default
整体替换。**键特异性**：`p3.gap.m1.api_x` > `p3.gap` > `default`
（点分段前缀，长优先）。

**优先级**：硬路由保护 > 键覆盖 > 内置默认 > default。

**硬路由表**（必人四点，配置改 agent 须全局开关
`allow_agent_on_human_gates: true`——问责链弱化应是一次显式、全局、
留痕的授权）：`p0.t5.env_gate` / `cp1.strategy` / `p6.l4.finalize` /
`cp5.promote`。另：**register-fill（动平台代码）的必人在 p3 分类
代码层实现**（分类结果直接转 human 策略进 gap 关口），不经路由键。

**内置默认**（配置无覆盖时）：上表四点 + env_broken 为 `[human]`；
T3/类别类 `[human]`（agent 层已消费）；`p1.resolve.cycles`
`[agent, human]`；`loop.budget` `[human]`；其余走 default。

**决策债计数（收窄）**：自动应答时打 `debt_class`——
`skip`（gap bypass 类，机器看不见"被扔掉的东西重不重要"）、
`measure`（改量尺类）、`low`（低置信放行）、`general`（下游有机器
验证兜底，**不计**——现实本身就是复核者）。限额默认 30
（`checkpoints.decision_debt_limit`）。

**配置校验**：启动时 `validate_routing` 打警告（未知层/空链/链无
human 且非全自动点），不阻塞（回落内置默认）。

### 3.9 观测协议

**events.jsonl + 快照**（log 子系统 `porter/log/`，经 `loop/events.py`
兼容门面；§15 bypass 不受控、保留；规范见 docs/log.md）。
子系统写入的事件类型：`gate-auto-answered`（路由自动应答）、
`policy-hit`（规则命中 + 遥测 policy_hits.json）、`gate-veto`、
`gate-cluster`（聚类）、`boot-log-missing` / `boot-log-empty`
（日志面三态）、`memo`、`gate-cluster`；§15 内部的 `triage` /
`escalation` / `snapshot` 在 bypass 下不产生。

**§15 bypass 边界**（`self_diagnosis.enabled=false`，默认）：
triage+diagnose 休眠（p5 失败不分诊直接走 attempts→panic；p6 红项
直接进 verdict）；`--defect-diagnose` / `--defect-fix` 入口 rc 2；
`PORTER_SELF_DIAGNOSIS=1` 强制开（存量测试用）。

### 3.10 关口 ID 命名规范与全目录

**命名**：`<相位>.<种类>.<范围>.<主题>`，点分段、小写、稳定
（前缀即路由特异性键）。

**现存目录（21 个，截至本文撰写）**：

panic 车道（12）：

| id | kind | 路由链 | 表单 |
|---|---|---|---|
| p0.t3.extract | fact | [human] | answers（自由文本全文） |
| p0.category.none / p0.category.unparseable | fact | [human] | category |
| p0.t5.env_gate | fact/physical | [human] | note |
| p1.resolve.cycles | decision | [agent, human] | ack |
| p3.gap.\<M\>.\<api\> | decision/gap | [rules, agent, human] | strategy/instruction/rationale |
| p4.blocked.\<M\>.\<file\>-\<start\> | decision | [agent, human] | instruction/rationale |
| p4.slice_sig.\<M\>.\<file\>-\<start\> | retry/failure | 同 attempts | note |
| loop.attempts.\<M\>-\<p3\|p4\|p5\> | retry/failure | [human] | note（诊断笔记进下轮 prompt） |
| loop.budget.\<M\> | retry/failure | [human] | note |
| p5.deferred.\<M\>.\<id\> | decision/deferred | [rules, agent, human] | verdict(fix-criterion/fix-code)/new_expr |
| infra.boot_no_log | retry/failure | —（抢占型） | note |
| p6.escalation.\<did\> | approval | — | verdict（**§15 休眠**） |

checkpoint 车道（9）：cp0.runner_review（memo）、cp1.strategy、
cp2.mapping_review、cp.fm.\<M\>、cp.debt.\<n\>、p6.l4.finalize、
p6.defect.fix.\<did\>（**§15 休眠**）、cp4.defect_review、
cp5.promote（memo）。

---

## 4. 实现地图（改进参考）

### 4.1 模块

| 模块 | 职责 | 关键公共面 |
|---|---|---|
| `porter/loop/gates.py` | 账本 + 检查点 + panic + 渲染 | `GateLedger`（load/save/find/open_blocking/pending_review/add/note/mark）、`parse_gate_answers`、`validate_answer`、applier 注册表（@applier("retry/decision/approval/fact/memo")）、`process_answered_gates`、`resolve_applied`、`render_human_questions`、`panic`、`summary_line`、`load_config`、`self_diagnosis_enabled`、`checkpoint_enabled`、`first_module_review_enabled`、`checkpoint_digest`、`checkpoint_run`、`strategy_checkpoint` |
| `porter/loop/routing.py` | 三级分流 + policy + 债计数 | `load_routing`、`validate_routing`、`route_for`、`consult_policy`、`agent_answer`（含 KB 检索注入与 kb_consulted 记账）、`maybe_auto_answer`、`debt_count`、`debt_limit`、`policy_path` |
| `skills/gate-answer.md` | rules/agent 层共用作答指令 | — |
| `skills/L4-draft.md` / `skills/defect-fix.md` | S5 自动化的 skill | —（defect-fix 随 §15 休眠） |

### 4.2 各相位接入点

| 位置 | 接入 |
|---|---|
| main.py cmd_p0 | 启动消费 @ 答案；T5 败→panic(p0.t5.env_gate)；尾接 CP0 |
| main.py cmd_p1 / cmd_p1_divide | CP1（strategy_checkpoint，直通路径也拦） |
| main.py cmd_p6 | `--draft-l4` / `--defect-fix` 参数分发 |
| main.py cmd_p7 | 入口 CP4（缺陷债批审）；尾 CP5 |
| main.py cmd_gate / main() | gate CLI 四子命令；启动路由校验警告 |
| bootstrap/run.py run_p2 | 尾接 CP2（默认关） |
| loop/run.py | attempts/budget panic；rc==3 复查 open_blocking（空→续跑）；债限额软停 `_debt_checkpoint`；结算 `_settle_debt_checkpoint`；FM 检查点；全完→CP3 指针 |
| loop/p3.py | gap human 关口（`_panic_gap_gates`）；register-fill 分类层转 human；双协议答案消费 |
| loop/p4.py | blocked 立即停（rc 3）；同签名检测；fill/冒烟的日志面抢占 |
| loop/p5.py | deferred 关口；`_judge_core` 返回 log_state、missing 抢占 rc 3；§15 bypass 断路 |
| loop/p6.py | L4 finalize 审批关口（指纹绑定+刷新）；draft_l4 生成器；fix_defect（§15 守卫）；execute 红项 guard + 抢占；escalation 关口 |
| loop/probes.py | `_recover_boot_log`（三态）、`_log_face`（复探+panic）、空日志标注；生命周期 missing 抢占 |
| env/extract.py、env/category.py、divide/resolve.py | T3/T2/解环关口 |

### 4.3 配置 schema（porter/config.json）

```json
{
  "self_diagnosis": {"enabled": false},          // §15 总开关
  "review_gates": {"l4_criteria_finalization": "human",
                   "strategy_review": "human",
                   "diagnosis_escalation": "agent",   // §15 休眠中
                   "b_class_autofix": "agent"},       // §15 休眠中
  "routing": {
    "default": ["rules", "agent", "human"],
    "gates": {"p0.t5.env_gate": ["human"], "…": "…"},
    "allow_agent_on_human_gates": false},
  "checkpoints": {"CP2_enabled": false,
                  "first_module_review": true,
                  "decision_debt_limit": 30},
  "policy_file": "policy.md",
  "panic": {"same_signature_repeat": 2, "cluster_threshold": 3}
}
```

工作区覆写：`<ws>/routing.json`（仅 routing 节，见第 5 章限制）。
工作区规则：`<ws>/policy.md`（自然语言，agent 解释，命中记
`policy_hits.json`）。

### 4.4 CLI

`porter gate list [--status open|debt|all]` / `show <id>` /
`answer <id> --set 字段=值（可多次）` / `review [--cp 名]`。
CLI 是便利层——协议本体是"账本 + answers.md"，纯文件操作完全可用。

### 4.5 测试地图（协议的行为级定义）

| 文件 | 验证 |
|---|---|
| tests/test_gates.py | 3.1-3.4（账本/答案解析校验/应用器/渲染/panic 幂等） |
| tests/test_s3_checkpoints.py | 3.6（digest/veto 回滚/CP1 指纹/CP2 开关/FM 一次性） |
| tests/test_s4_routing.py | 3.8（层链/两级合并/自动应答三结局/债收窄） |
| tests/test_s5_automation.py | draft-l4 / fix-defect（后者需 §15 开） |
| tests/test_s15_bypass.py | 3.9（开关/入口守卫/p5 快速断路） |
| tests/test_loop_state.py | 3.5/3.7（烧穿→关口→retry 恢复全链） |
| tests/test_mounts.py / test_replay.py / test_p6.py | §15 强制开的存量行为 + 新关口断言 |

---

## 5. 已知限制（改进时的检查清单）

1. **veto 回滚粒度**：相位回拨后，已 done 的迁移切片仍被 migration.json
   跳过——需要强制重迁的片须人工清除对应记录（digest 已注明）；
2. **工作区覆写范围**：`routing.json` 仅覆写 routing.gates/default；
   checkpoints / policy_file / panic 阈值只认仓级配置（TODO 第 1 条）；
3. **§15 休眠面**：triage.py 的 b_class 与 diagnose.py 的 escalation
   两处 legacy md 写盘**不可达但未收编**——§15 重启用时须先转账本
   （TODO 第 3 条）；`p6.defect.fix.*` / `p6.escalation.*` 关口在
   bypass 下不会产生；
4. **CP2 默认关**：依据 = e2e 实证（无映射人审跑通）+ 下游机器验证
   兜底（探针/编译/判据会逮住错映射）+ 850 条人审成本高；
5. **infra.boot_no_log 固定 id**：多处触发共用一个关口（re-asked 计数
   即聚合），无按 label 细分；
6. **FM 只对拓扑首模块生效**：`--module` 绕行路径不触发 FM；
7. **自动应答的 agent 调用成本**：每个 decision 类 panic 都会尝试
   policy→agent 两层（各一次有界调用）；PORTER_NO_AGENT=1 全跳过。

## 6. 演进方向与"该回头改"的信号

已商定的后续工作见 `TODO.md` 四条：工作区覆写通用机制 / 工具 log
子系统重建 / §15 重设计（含空日志-静默控制台回路优先项）/ p6 私有
boot 助手去重。

**回头信号**（出现即该回来改本子系统）：

- 聚类提示频发（同关口 re-asked ≥3 常态化）→ 该类问题该升检查点或
  补 policy 规则；
- policy 命中率低或误命中多 → 规则库需要重写/收紧；
- 债批审单次耗时过长 → digest 分组粒度或限额需要调；
- 单次迁移 panic 次数常态偏高 → 自动化覆盖不足（对照第 3.10 目录
  找高频关口）；
- 新增介入点时：先过第 2 章分类学判定"真需要人吗"，再按第 3 章协议
  登记——**禁止**绕过账本直接写 human_questions.md。

---

## 附录 A：16 处介入点最终归宿

原审计的 16 处人工介入点在本子系统下的归宿：

| 原介入点 | 归宿 |
|---|---|
| #1 T3 环境提取失败 | panic 关口 p0.t3.extract（fact） |
| #2 T3 非阻塞备忘 | 降级：P0/reports/memo.md + event（永不写阻塞面） |
| #3 T2 类别识别失败 | panic 关口 p0.category.*（--category 免交互捷径保留） |
| #4 T5 门禁失败 | panic 关口 p0.t5.env_gate（硬路由必人） |
| #5 P1 strategy 审阅 | CP1 检查点（指纹绑定；p1 直通也拦，修 H5） |
| #6 P1 解环失败 | panic 关口 p1.resolve.cycles（agent 层已消费 3 轮） |
| #7 P3 gap human | panic 关口 p3.gap.*（满链分流）；register-fill 分类层转必人 |
| #8 answers 未齐 | 消亡（新协议逐关校验，不齐即 invalid 标红） |
| #9 attempts 烧穿 | panic 关口 loop.attempts.*（诊断笔记进下轮 prompt） |
| #10 P4 blocked | panic 关口 p4.blocked.*（立即停，修 H13） |
| #11 deferred 无法清偿 | panic 关口 p5.deferred.*（fix-criterion 双写修副本陷阱） |
| #12 L4 定稿 | CP3 审批关口 p6.l4.finalize（指纹绑定） |
| #13 L4 草案起草 | 自动化：p6 --draft-l4 生成器（修 H1/H2），人审归 CP3 |
| #14 缺陷修复 | 自动化：--defect-fix（修 H4）→ **§15 bypass 下休眠**，人工+--defect-close |
| #15 §15 审核门 ×2 | §15 bypass 下休眠（重启用时先转账本） |
| #16 知识晋升 | CP5 提醒（memo 非阻塞），晋升仍是人工命令 |

新增（原审计之外）：同签名连发（p4.slice_sig）、墙钟预算（loop.budget）、
日志不可得（infra.boot_no_log，抢占型）、FM 首模块审、CP-DEBT 债限额、
CP2 映射抽审（默认关）。

## 附录 B：人机往返 walkthrough

**场景 A（最典型）：P4 反复失败 → 诊断 → 恢复**

```
$ porter/main.py loop --output-dir ws
[porter] loop: P4(rx-ring) 失败 rc=1（attempts 3/3）
[porter] gates: panic loop.attempts.rx-ring-p4（待答关口 1 个）→ 详见 ws/human_questions.md
$ echo $? → 3

# 人：看 ws/human_questions.md 的表单（note 可选），修好问题（如工具链路径）
$ cat >> ws/answers.md <<'EOF'
## @loop.attempts.rx-ring-p4
note: 修复了 cargo 路径未导出导致的编译失败
EOF

$ porter/main.py loop --output-dir ws
[porter] gates: 关口已应用 loop.attempts.rx-ring-p4（attempts 清零（rx-ring-p4）｜诊断笔记: …）
[porter] loop: P4(rx-ring) …   ← 从断点继续，已成功切片不重做
```

**场景 B（展示 veto）：否决一条自动决策**

```
# gap 关口 p3.gap.m1.api_x 被 agent 层自动答成 bypass（决策债）
# CP-DEBT 停车或人事后复核时发现该统计接口下游需要：
$ cat >> ws/answers.md <<'EOF'
## @p3.gap.m1.api_x
verdict: veto
rationale: 该统计接口被验收判据引用，不能丢
EOF
$ porter/main.py loop --output-dir ws
[porter] gates: 决策债被否决 p3.gap.m1.api_x（已否决…attempts 清零 + 相位回拨：m1…）
# → m1 相位回拨到 p4，按原 mapping 重新迁移该 API 所在切片
```

## 附录 C：README 集成指引（给未来的 README 重写 session）

- **放置位置**：CLI 子命令总览之后、验证/测试章节之前——读者先知道
  工具能跑什么，再知道什么时候需要自己出场。
- **展开程度**：README 放"两种介入方式"结构（固定介入点表 +
  panic 信号表 + 操作速查）+ 指向本文档的链接；当前 README 已按此
  写成，重写时可对照迁移。**协议细节（第 3 章）不要进 README**——
  那是维护者文档。
- **两张清单须同步**：代码里新增检查点或 panic 信号时，README 的
  两张表与本文 3.6/3.10 一并更新。
- **交叉引用**：README 的"工作区文件"一节应列 gates.json /
  human_questions.md / answers.md / policy.md / checkpoints/ 五个
  交互面文件（当前 README 缺）。
- 与 TODO.md 的关系：README 不描述限制与演进（第 5/6 章的活）。
