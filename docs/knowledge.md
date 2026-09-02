# 知识库子系统（kb）

> 本文档定义知识库子系统的协议规范与实现参考：第 1 章总览（README
> 引用此处）；第 2-3 章术语与协议标准；第 4-6 章实现地图、已知限制
> 与演进方向。
> 配套：`TODO.md`（已商定的后续工作，第 5-9 条）、`porter/config.json`
> 的 kb 节（运行时配置）、`tests/test_kb.py` 等 6 个测试文件（协议的
> 行为级定义）。

---

## 1. 概述

工具每跑一次迁移都会产生大量"经验"：某个 Linux 接口在这个目标 OS
上怎么绕过去、这台机器怎么把系统编出来、某个编译错误怎么修好、某次
失败排查了很久才发现根因。这些经验如果只留在当次工作目录里，下次
迁移（哪怕同一个驱动、同一台机器）就要全部重新付一遍成本。

知识库子系统把这些经验集中到工具仓库的 `knowledge/` 目录，并让
agent 在干活时能查到它们。核心机制三句话：

1. **agent 起草，人工晋升**。工具在流程中自动把经验收集成草稿和
   候选（没人看着也不会出错——草稿不参与任何自动决策）；只有人
   审阅后才晋升为正式知识。人不下场时 agent 也可以查知识库，但
   查到的内容必须重新核实才能用（历史结论可能过时）。
2. **知识分两类，进料方式不同**。**固定知识**是每次迁移必然产出
   的（API 映射表、接口缺口处置、目标 OS 操作手册、拆分策略样例）
   ——流程到了固定位置就自动收成；**随机知识**是偶发发现的
   （一次失败排查的结论、agent 顺手发现的坑）——由探查钩子捕获
   成候选，经人工审核后入库。
3. **查询靠目录自取，不靠强制注入**。agent 的任务指令里附带一份
   知识条目目录（每个条目一行：文件名 + 一句话说明），agent 判断
   相关才去读全文，用完报告读过哪些（计入热度）。替人自动回答
   问题时也先查知识库（只用已审内容）。

人的操作集中在两个时刻：开始一次迁移时**指定知识库目录**（新建或
复用既有，必填）；迁移结束的 CP5 检查点上**审阅候选与草稿**并决定
晋升哪些。其余全自动。

设计目标一句话：**把"每次迁移从零摸索"变成"站在上次迁移的肩膀
上"**——经验自动收集、人工把关、下次自取。

---

## 2. 概念与术语登记表

后文所有章节只使用本节登记过的术语。

| 术语 | 白话定义 | 为什么需要它 |
|---|---|---|
| **知识库（KB）** | 工具仓库 `knowledge/` 目录及其管理机制的总称 | 子系统边界：工作区里的报告/账本不算知识库 |
| **三区** | knowledge/ 下的三种分区：base、temp、知识库目录 | 一切路径规则的出发点 |
| **base** | 工具随附的一般知识分区（`knowledge/base/`，git 跟踪），任意目标 OS 可用 | 区分"工具作者写的"与"迁移产生的" |
| **temp（草稿区）** | 未审知识暂存区（`knowledge/temp/`，目录骨架跟踪、内容全部 gitignore） | agent 可写而不污染正式知识；跨迁移共享 |
| **知识库目录（corpus）** | 一次迁移（或用户自维语料）的知识目录 `knowledge/<name>/`，p0 时由用户显式指定 | "本次迁移的知识库"的物理载体；可跨工作区复用 |
| **域（子目录）** | 知识的分类单位 = 知识库目录下的子目录（maps/gaps/runbook/splits/pitfalls） | 分类即存放位置即检索入口，三者合一 |
| **固定知识** | 每次迁移必然产出的知识（四域各有定点收成） | 免探查、免分类——收成即入库草稿 |
| **随机知识** | 偶发发现的知识（事件触发、有无不定） | 需要探查→审核→分类→沉淀流水线 |
| **候选（candidate）** | 随机知识的暂存记录（temp/candidates/ 下的账目） | 未分类知识不进域，先入候选账 |
| **探查钩子** | 挂在工具自有事件上的捕获点（四类，见 3.4） | "知道有东西产生了"的确定性问题 |
| **去重闸** | 候选入账前的签名查重与长度过滤 | 防高频事件灌爆审核面 |
| **收成（harvest）** | 固定知识从工作区产物抽取为 temp 草稿的动作 | 定点、幂等、agent 不参与 |
| **晋升（promote）** | 草稿/候选经人审后从 temp 搬入知识库目录的动作 | 策展权必人——正式知识的唯一入口 |
| **薄 INDEX** | 每个域分区的条目目录：`[{file, desc, hits}]` | agent 检索的物理形态；一句话描述对应细粒度文件 |
| **kb-guide（总纲）** | `skills/kb-guide.md`，随所有知识面调用注入的使用规范 | 规则 0 与铁律的单一事实源 |
| **规则 0** | 总纲第一条：处理任务前必须先查本次提供的知识条目目录 | 提高 agent 主动访问知识库的权重 |
| **知识面（KB 面）** | 一次 agent 调用携带的总纲 + 条目目录（`kb_face()`） | 检索的注入单位；无条目则无面 |
| **kb_consulted** | agent 在输出 JSON 里报告实际读过的条目文件 | 使用热度遥测，人工策展决策依据 |
| **hits** | 条目被报告阅读的累计次数（记在 INDEX 行） | 高频=健康、持续零=该复核或下架 |
| **归类（classify）** | 为候选定去向子目录的动作（查表建议 → agent 批量 → 人在晋升时可改） | 分类决定哪些 agent 会查到它 |
| **KB 健康报告** | CP5 材料里的聚合：hits 排行、零咨询清单、规则命中遥测、被否决自动决策聚类 | "哪些知识值得策展"的数据依据 |
| **起点假设** | T3 注入 runbook 时的定位标注：环境会漂移，命令与特征本轮仍须实测复核 | 防止历史命令被当成免验答案 |
| **信任分层** | 注入面规则：相位任务可见 temp+已审（草稿带标注）；自动应答只见已审 | 未审中间态不参与自动行为 |

---

## 3. 协议规范（标准定义）

### 3.1 目录模型

```
knowledge/
├── base/                    # 工具随附（git 跟踪；任意目标 OS 可用）
│   └── splits/strategies/   #   目前唯一实例：拆分策略样例骨架
├── temp/                    # 草稿区（README 骨架跟踪；内容全部 gitignore）
│   ├── maps/  gaps/  runbook/  splits/strategies/   # 各域草稿
│   └── candidates/          #   随机知识候选账
├── <name>/                  # 知识库目录（p0 --kb 指定；git 策略新建时定）
│   ├── maps/  gaps/  runbook/  splits/strategies/  pitfalls/
│   └── INDEX.json（每域每分区一份）
└── failures.md              # §15 失败签名册（域外，原地不动）
```

**命名空间**：temp 跨迁移共享，条目一律按 `<驱动名>@<目标OS名>`
隔离——maps/runbook 以文件名或子目录携带，gaps/runbook 为嵌套子目录
（`gaps/<ns>/<api>.md`、`runbook/<目标OS>/<主题>.md`），pitfalls 晋升
条目以文件名前缀 `[<ns>]` 描述携带。多工作区共用同一知识库目录为
单写者假设（见第 5 章）。

**p0 显式选择（必填）**：

| 参数 | 语义 |
|---|---|
| `--kb new <名>` | 新建 `knowledge/<名>/`；缺省复制 base，`--kb-empty` 建空目录 |
| `--kb-git track\|ignore` | 新建目录的 git 策略；ignore 时工具把 `knowledge/<名>/` 追加进 .gitignore（缺省 track） |
| `--kb use <名>` | 指定既有目录 |
| （缺省） | rc 2 + 打印选择指引（列既有目录）；已记录 kb_dir 的工作区复用记录值 |

**名称校验**：单段名（`^[A-Za-z0-9][A-Za-z0-9._-]*$`），禁 base/temp。
**记录**：project.json 的 `kb_dir` 字段存目录名，全工具经
`kb.kb_dir_for(ws)` 解析。

**回流 base**：本轮不设通道（TODO 第 8 条）；人工可直接编辑 base
（它就是 git 跟踪的普通目录）。

### 3.2 域注册表（分类学）

域 = 子目录 = 分类。注册表（`kb.DOMAINS`）是分类学的唯一事实源：

| 域 | 子目录 | 内容 | 条目粒度 | scope |
|---|---|---|---|---|
| maps | `maps/` | Linux API → 目标 OS 对应物映射表（`.md` 人读 + `.json` 机器表双文件） | 一驱动@目标一张整表 | (api, target) |
| gaps | `gaps/` | API 缺口处置：怎么绕、fill 补齐成败与原因 | **一个 API 一个文件，文件名即 API 名** | (api, target) |
| runbook | `runbook/` | 目标 OS 操作手册：构建/启动/单测命令与坑史 | 一主题/一坑一文件 | (target, 环境) |
| splits | `splits/strategies/` | 拆分策略样例（strategy.md 原样） | 一驱动一文件 | Linux 侧（任意目标可复用→base） |
| pitfalls | `pitfalls/` | 踩坑记录：平台/模拟器坑与方法教训（条目标签区分） | 一坑一文件 | 跨领域 |

**加一个域 = 单点改动**：注册表一行 + kb-guide 总纲补一节 + 调用点
域预选补一行。完备性顾虑与扩展路径见 TODO 第 5 条。

**gaps 域特例**：文件名即 API 名（非法字符清洗为 `_`），"这个 API
以前 fill 失败过吗" = `prior_entry()` 文件名存在性检索，零内容解析。

### 3.3 固定知识收成协议

| 域 | 收成点（代码位置） | 产物 |
|---|---|---|
| maps | P2 末（`mapping.run_map` 尾）+ 每轮 P3(M) 末（`p3._refresh_drafts`） | temp/maps/ 整表双文件 + 薄 INDEX 行（desc 含 verdict 计数与换思路数） |
| gaps | 与 maps 同点（`_refresh_drafts` 同时调 `gaps.draft_gaps`） | temp/gaps/<ns>/<api>.md（含 strategy/instruction/evidence/rationale/fill 结果）+ INDEX 行 |
| runbook | p0 末（T5 门禁通过后）+ P5 unit_test 回填后 | temp/runbook/<目标OS>/{build,boot,unit_test}.md（含设备注入机制与坑史 notes）+ INDEX 行 |
| splits | P1 产出 strategy.md 时（`strategy._draft_to_temp`） | temp/splits/strategies/<驱动>.md + INDEX 行（价值判定：与已沉淀完全一致不重写） |

**幂等**：同命名空间整区重建（INDEX 行替换、hits 保留）。
**H18 修复（强制语义）**：P3 的**所有非零返回路径**（gap exit-3、
判据失败、探针失败）同样刷新草稿——人工关口答案恰在 exit-3 轮写入，
漏刷新即丢增量。
**收成不晋升**：晋升永远是人工动作（3.7）。

### 3.4 随机知识协议（探查→审核→分类→沉淀）

**探查 = 四类钩子**（porter 自有场所闭集；覆盖性是结构性的——任何
影响流水线的知识必经工具三个 I/O 面之一：它写的文件、它解析的
表单/CLI、它解析的 agent 输出）：

| 类 | 钩子 | 挂载点 | 覆盖的知识来源 |
|---|---|---|---|
| 1 | gate 应答收口 | `gates.process_answered_gates`（applied 与 veto 两分支） | 关口答案的 note/rationale：gap 裁定理由、T3 人工答案、attempts 诊断笔记、veto 理由等 |
| 2 | CLI 台账动作 | `p6.close_defect` / `p6.park_defect` / `p6.finalize_l4`（park 条目）/ `p7.register_patch` | 缺陷四字段根因链、泊车理由、L4 park 理由、平台补丁提案理由 |
| 3 | 产物状态翻转 | `p4` 切片 FAIL→PASS（重试 ≥2 次后成功）；`probes` 探针降级 | 编译错误→修复原始留痕（蒸馏归人工）、探针判别现场 |
| 4 | agent 自报 | `record_lessons`：解析结构化输出的 `lessons` 字段（P2a/P3A/P3G 三处） | agent 任务中顺手发现的坑 |

注：fill 失败原因与 runner 回填已被 gaps/runbook 固定收成覆盖，
不重复设钩。

**候选记录 schema**（temp/candidates/<ns>.json，文档格式
`{seq, items}`——seq 单调，id 出账不复用）：

```json
{
  "id": "cand-0007",
  "source": {"hook": "gate-answer", "ref": "loop.attempts.rx-ring-p4",
             "time": "…"},
  "scope": {"driver": "e1000", "target_os": "asterinas", "module": "rx-ring"},
  "draft": "<note/rationale 原文>",
  "evidence": ["P4/rx-ring/logs/"],
  "suggested_class": "pitfalls",
  "signature": "<sha1 前 16 位>",
  "status": "pending",
  "history": [{"time": "…", "event": "created（建议类 pitfalls）"}]
}
```

**去重闸**：draft 规范化（压空白+小写）sha1 前 16 位为签名，同账
重复 → 跳过；短于 `kb.min_draft_len` → 视为无知识跳过；
`kb.candidates=false` → 总关停。每次入账发 `kb-candidate` 事件。

**建议类**：关口 id 前缀查表（`p3.gap.`→gaps、`p0.t3.`/`infra.`→
runbook、其余→pitfalls）——免费、仅供参考、非定案。

**顺序**：探查 → **审核（人在 CP5 判价值）** → **分类（agent 批量
建议，`kb classify`）→ 沉淀（`kb promote`，人在 `--to` 可改，改判留
档在条目内）**。候选在分类前不进任何注入面（无归属即不可检索）。

### 3.5 薄 INDEX 协议

```json
[{"file": "msleep.md",
  "desc": "msleep：bypass——驱动内 TSC 忙等（i8042 先例）（fill 曾失败）",
  "hits": 3}]
```

- **核心字段就三个**：file（可携带相对子目录路径）、desc（一句话
  内容描述）、hits（被咨询次数）。旧富格式行（entry_file/title 等）
  渲染时兼容读取；
- **写入者**：收成器/晋升命令写行；`kb_consulted` 回报给行 hits+1
  （**只记已审分区**——temp 草稿不计数）；
- **desc 与粒度的契约**：文件粒度细（一 API/一主题/一坑一文件），
  desc 才能短而准——这是检索质量的根基；
- INDEX 缺失/损坏 → 渲染退化为该分区文件名清单（gaps/runbook 嵌套
  目录以 INDEX 为准）。

### 3.6 检索协议（消费面）

**注入形态 = kb_face(ws, domains)**：总纲（kb-guide）+ 相应域的
条目目录（已审分区在前、temp 草稿分区带"未经人审"标注在后）。
**无条目即无面**（规则 0 不空转）。含铁律：自己去读全文；内容是
历史主张，evidence 的 file:line 必须在当前树重新核实；禁止照抄
未核实结论；不跨目标复用；用完报 kb_consulted。

**调用点 → 域 预选表**（确定性，无 agent 参与）：

| 调用点 | 位置 | 域 |
|---|---|---|
| T3 环境提取 R1 | `env/extract.py` | runbook（起点假设标注，本轮仍须实测） |
| P2a 引导映射 | `bootstrap/mapping.py` | maps |
| P3 增量映射批次 | `loop/p3.py` | maps |
| P3 gap 处置分类 | `loop/p3.py` | gaps |
| P4 fill 前 | `loop/p4.py` | gaps 的 `prior_entry` 文件指针（历史失败必读） |
| P1 拆分策略 | `divide/strategy.py` | splits 三分区（base+知识库目录+temp，自有渲染） |
| 路由层 agent 答关 | `loop/routing.py` | suggest_class(关口 id) + pitfalls；**仅已审分区** |

**信任分层（铁律）**：相位任务面 = temp ∪ 已审（草稿带标注、冲突
以已审为准）；**路由自动应答面 = 仅已审**（+base）——未审中间态不
参与任何自动行为。

**kb_consulted**：agent 输出 JSON 的可选数组字段（实际读过的条目
文件名），编排器解析后调 `record_consulted` 记 hits。有结构化输出
的调用点解析（路由/P2a/P3A/P3G）；自由文本输出（P1S strategy）暂无
遥测。

**maps 内容注入的退役**：原 collect_hinks 的域过滤内容注入已被
统一目录注入取代；三个搁置的内容注入候选与回归验证义务见
TODO 第 6 条。

### 3.7 晋升协议

| 通道 | 命令 | 语义 |
|---|---|---|
| maps | `p2-promote --output-dir <ws> --driver <d> [--target <t>]` | temp 整表双文件 → 知识库目录 maps/；同名 = 活文档替换（hits 取两侧较高） |
| splits | `p1-promote --output-dir <ws> --driver <d>` | temp 样例 → 知识库目录 splits/；与 base 完全一致拒绝；构成不同自动改名（`__2`）并入 |
| 候选 | `kb promote --output-dir <ws> [--id ID…] [--promote all] [--to <域>]` | 候选 → 域条目（gaps 入 <ns>/ 嵌套，其余扁平加 ns 前缀；重名递增）；`--to` 覆盖建议类时条目内留改判记录 |
| 薄格式域 | `kb.promote_entries(域, files, kb_dir)`（库函数） | 通用文件+INDEX 行搬运（嵌套目录支持） |

共同纪律：**晋升必人触发**（CP5 只提醒不代做）；两 promote 命令的
`--output-dir` 用于解析 kb_dir（缺失/未记录 → rc 1 并给指引）。

### 3.8 审核协议（CP5 面）

CP5 检查点（p7 末，memo 非阻塞）生成**知识备审材料**
`checkpoints/CP5_knowledge.md`，三节：

1. **候选队列**：逐条列出 id/建议类/来源钩子/草稿摘录/证据指针/
   处置命令（promote/reject）；
2. **temp 草稿清点**：各域草稿条数与文件清单；
3. **KB 健康报告**：已审条目 hits 排行 + 零咨询清单（该复核或
   下架）+ policy 规则命中遥测 + 被否决自动决策聚类（边界证据，
   理由可写成规则或知识）。

审核后动作：`kb classify`（agent 批量归类，一次调用；NO_AGENT 跳过
人工 `--to` 定案）→ `kb promote` / `kb reject`。细节演进见 TODO 第 9 条。

### 3.9 遥测与事件协议

| 信号 | 载体 | 消费者 |
|---|---|---|
| 条目 hits | 各域已审分区 INDEX 行 | KB 健康报告（策展依据） |
| kb_consulted | agent 输出 JSON 字段 | record_consulted → hits |
| kb-candidate 事件 | events.jsonl | 观测层 |
| policy_hits.json | 路由 rules 层命中计数 | KB 健康报告 |
| veto 聚类 | gates 账本 vetoed 条目 | KB 健康报告 |

### 3.10 与 gates 子系统的关系

- **关口答案是知识源**：表单的 note/rationale 强制留档 → 类 1 钩子
  产生候选；gap 关口的 rationale 由 applier 回写 gap_decisions
  （随固定收成入 gaps 域）；
- **路由三层中的 agent 层带 KB 面**：rules（policy.md）→ agent
  （+知识库检索，仅已审）→ human。policy.md 保留（即时性出口：
  同类问题被问烦即写规则即生效）；知识库走审核（跨驱动复用）；
  两者的遥测都汇入 KB 健康报告；
- **信任分层的根据**：自动应答只依据人写的规则与人审过的知识——
  未审候选/草稿不参与。

---

## 4. 实现地图（改进参考）

### 4.1 模块

| 模块 | 职责 | 关键公共面 |
|---|---|---|
| `porter/bootstrap/kb.py` | 骨架：三区路径、域注册表、薄 INDEX、目录渲染、通用晋升、kb_face、kb_dir 解析、select_kb | `DOMAINS`、`domain_temp/kb/base`、`kb_dir_of/for`、`select_kb`、`load/save_index`、`upsert_entry`、`bump_hits`、`render_catalog`、`catalog_block`、`load_guide`、`kb_face`、`record_consulted`、`promote_entries` |
| `porter/bootstrap/knowledge.py` | maps 域收成/晋升 | `draft_knowledge`、`promote_map` |
| `porter/bootstrap/gaps.py` | gaps 域收成/检索 | `draft_gaps`、`prior_entry`、`sanitize_api` |
| `porter/bootstrap/runbook.py` | runbook 域收成 | `draft_runbook` |
| `porter/bootstrap/candidates.py` | 探查：候选账/去重闸/建议类/钩子助手 | `record_candidate`、`record_from_gate`、`record_lessons`、`load_candidates`、`remove_candidate`、`suggest_class` |
| `porter/bootstrap/review.py` | 审核面/分类/晋升/拒绝 | `build_cp5_material`、`classify_candidates`、`promote_candidate`、`reject_candidate` |
| `porter/divide/strategy.py` | splits 三分区注入/草稿/晋升 | `sample_partitions`、`_build_samples_injection`、`_draft_to_temp`、`promote_sample` |
| `skills/kb-guide.md` | 总纲（规则 0/三区/五域/铁律） | — |

### 4.2 各相位接入点

| 位置 | 接入 |
|---|---|
| main.py cmd_p0 | --kb 决策（rc 2 逼显式）；kb_dir 记录；T5 过后 runbook 收成；CP0 前 |
| main.py cmd_p1_promote / cmd_p2_promote | 晋升命令（--output-dir 解析 kb_dir） |
| main.py cmd_p7 | CP5：build_cp5_material + 扩容关口 |
| main.py cmd_kb | kb 子命令族分发 |
| env/extract.py | T3 R1 注入 runbook 面（起点假设） |
| bootstrap/mapping.py | P2a 注入 maps 面；kb_consulted/lessons 解析；P2 末 maps 收成 |
| divide/strategy.py | P1S 三分区注入 + splits 草稿 |
| loop/p3.py | P3A/P3G 注入与 kb_consulted/lessons；gap 分类的 rationale 落 gap_decisions；`_refresh_drafts`（maps+gaps，全失败路径） |
| loop/p4.py | fill 前 prior_entry 指针；切片翻转钩子 |
| loop/p5.py | unit_test 回填后 runbook 收成 |
| loop/p6.py | close/park/finalize-park 钩子 |
| loop/p7.py | patch-register 钩子 |
| loop/gates.py | 应答收口钩子（applied/veto）；`_apply_gap` rationale 回写 |
| loop/probes.py | 降级钩子 |
| loop/routing.py | agent 层 KB 面（仅已审）+ kb_consulted 记账 |

### 4.3 配置 schema（porter/config.json 的 kb 节）

```json
"kb": {
  "candidates": true,      // 探查总开关
  "dedup": true,           // 签名去重闸
  "min_draft_len": 24      // 过短草稿视为无知识跳过
}
```

### 4.4 CLI

```bash
porter p0 … --kb new <名> [--kb-empty] [--kb-git track|ignore]   # 或 --kb use <名>
porter kb --output-dir <ws> [--list] [--classify] [--promote all]
          [--id ID …] [--to <域>] [--reject ID]
porter p1-promote --output-dir <ws> --driver <名>
porter p2-promote --output-dir <ws> --driver <名> [--target <名>]
```

（kb 缺省动作 = 刷新 CP5 材料 + 列候选。）

### 4.5 测试地图（协议的行为级定义）

| 文件 | 验证 |
|---|---|
| tests/test_kb.py | 3.1/3.2/3.5/3.6（注册表/kb_dir/薄 INDEX/通用晋升/select_kb/目录渲染/kb_face/路由检索与记账） |
| tests/test_maps_domain.py | 3.3/3.7（maps 收成薄行与 hits 继承/晋升搬运与同名替换） |
| tests/test_gaps_runbook.py | 3.3/3.6（gaps 归并与 fill 结果/命名空间/prior_entry；runbook 三主题与坑史；rationale 回写） |
| tests/test_candidates.py | 3.4（记录/去重/开关/建议类/钩子助手/挂载冒烟/出入账） |
| tests/test_kb_review.py | 3.8（CP5 材料/批量归类 mock/晋升改判留档/嵌套落盘/拒绝） |
| tests/test_strategy_knowledge.py | 3.6/3.7（splits 三分区草稿/晋升/注入） |

---

## 5. 已知限制（改进时的检查清单）

1. **候选不进检索面 → 同类问题当轮可能重复打扰人**：人答过一次的
   关口知识要等审核晋升才可被路由检索。即时出口是 policy.md（写规则
   即生效）；候选区可查的设计已被裁定撤回；
2. **kb_consulted 是自愿字段**：agent 不报则遥测缺失；P1S（自由文本
   输出）无遥测；
3. **maps 保持整表粒度**：与"细粒度文件"原则的偏离是有意为之
   （表型知识 + .json 机器表）；消费侧域过滤已退役，靠 desc 概括；
4. **多工作区共用同一知识库目录为单写者假设**：并行收成/晋升可能
   竞写 INDEX；
5. **classify 的提示词未经实战校准**：改判日志已积累数据，校准属
   TODO 第 9 条；
6. **base 回流无通道**（TODO 第 8 条）；**taxonomy 完备性**未检验
   （TODO 第 5 条）；
7. **§15 相关域外**：failures.md 候选区生产源随 bypass 休眠；判据
   auto_fixed 修正史无生产者——§15 重设计时其产出按第 3.4 节类 3
   接入即可。

## 6. 演进方向与"该回头改"的信号

已商定的后续工作见 `TODO.md` 第 5-9 条：taxonomy 完备性 / 固定知识
差异化检索设计（含 kb_consulted 回归验证）/ ktest 无会话复演 /
corpus→base 晋升 / CP5 审核细节。

**回头信号**（出现即该回来改本子系统）：

- **P3/P2a 映射批次的 kb_consulted 持续为零** → agent 不在读目录
  （统一指针化的回归信号）——按 TODO 第 6 条恢复局部内容注入或加强
  总纲措辞；
- **候选账长期积压** → 审核面太重或钩子太滥（去重闸/长度阈值该调）；
- **晋升时 `--to` 改判率高** → 建议类映射表或 classify 提示词需要
  校准（数据在改判留档里）；
- **零咨询条目常态化** → 知识失效或检索面选错域，健康报告该触发
  下架/修版；
- **新增知识类型时**：先过第 3.2 节注册表判定归属（加一行即扩类），
  不得在域外私设目录。

---

## 附录 A：审计 19 知识生产点归宿总表

原审计的 19 个知识生产点在本子系统下的归宿（编号沿用审计报告 §6）：

| # | 知识点 | 归宿 |
|---|---|---|
| 1-3 | runner build/boot/unit_test（含 notes 坑史） | runbook 域固定收成；notes 随主题入文，人工可蒸馏 pitfalls |
| 4 | API 映射表 | maps 域（收成 + 目录注入消费） |
| 5 | 换思路/接线清单 | maps 域（随整表） |
| 6 | gap 处置决策（含 rationale） | gaps 域固定收成（rationale 经 applier 回写） |
| 7 | 平台补齐候选 | 类 2 钩子（patch-register 候选）+ P7 台账 |
| 8-9 | fill 成功摘要 / 失败原因 | gaps 域（fill 结果入 API 文件——成败同渠道） |
| 10 | 切片编译错误反馈 | 类 3 钩子原始留痕（蒸馏归人工） |
| 11 | 探针验证史 | 结论随 maps notes；判别现场 → 类 3 钩子候选 |
| 12 | 判据自动修正史 | §15 域外（bypass 休眠；重设计后按类 3 接入） |
| 13 | 缺陷根因链 | 类 2 钩子（close/park 候选） |
| 14 | 失败签名库 | failures.md（§15 域外，原地不动） |
| 15 | 平台/模拟器坑 | pitfalls 域（手写保留 + 候选晋升入册） |
| 16 | 设备行为仲裁源 | refs/（参考数据，不属 KB） |
| 17 | 拆分策略样例 | base + temp + 知识库目录三分区（机制保留） |
| 18 | 知识消费遥测 | kb_consulted → hits（全域）+ policy_hits |
| 19 | §15 观测面 | events.jsonl 保留；新增 kb-candidate 事件 |

## 附录 B：两条 walkthrough

**场景 A（随机知识全链）**：

```
$ porter/main.py loop --output-dir ws
[porter] P4(rx-ring) 失败 rc=1（attempts 3/3）→ panic 关口停车（exit 3）
$ 填 answers.md：## @loop.attempts.rx-ring-p4 / note: 根因是构建缓存
  参数失效，修复须显式传 console 参数
$ porter/main.py loop --output-dir ws
[porter] gates: 关口已应用 …
[porter] kb 探查: 新候选 cand-0001（gate-answer，建议类 pitfalls）← 类 1 钩子
    …迁移继续至完成…
$ porter/main.py p7 --output-dir ws
[porter] CP5: 知识备审材料 checkpoints/CP5_knowledge.md   ← 审核面
$ python3 porter/main.py kb --output-dir ws --classify
[porter] kb classify: 归类完成（改判 0/1 条——建议 pitfalls 成立）
$ python3 porter/main.py kb --output-dir ws --promote all
[porter] kb promote: cand-0001 已晋升 → knowledge/<name>/pitfalls/…
# 下一次迁移中路由层答 attempts 类关口时，agent 检索到该条目（已审），
# 报 kb_consulted → hits+1；健康报告开始积累该条目的使用热度
```

**场景 B（固定知识复用）**：

```
$ porter/main.py p0 … --output-dir ws2 --kb use asterinas
[porter] --kb: 使用既有知识库目录 knowledge/asterinas
    …T3 R1 prompt 注入 runbook 条目目录（起点假设，须实测复核）…
[porter] T3: R1 探测全绿          ← 历史基线把 3 轮压到 1 轮
    …P3 gap 分类注入 gaps 目录；agent 检索 msleep.md
    （含"fill 曾失败"结论，动手前先读）…
    …P3(M) 末草稿刷新（含 maps+gaps，人工路径也不丢）…
$ porter/main.py p2-promote --output-dir ws2 --driver e1000 --target asterinas
[porter] p2-promote: e1000@asterinas 已晋升 → knowledge/asterinas/maps/
# 同名替换（hits 取两侧较高）——活文档语义
```

## 附录 C：README 集成指引（给未来的 README 重写 session）

- **放置位置**：CLI 子命令总览之后、人工介入节之前或紧随其后
  （读者先知道工具能跑什么，再知道经验怎么积累——与人工介入节
  相邻构成"工具怎么用你"与"工具怎么记住"一对）；
- **展开程度**：README 放"两种知识 + 各自处理方式 + 三区布局 +
  p0 必填选择 + kb 命令速查"的结构型介绍 + 指向本文档的链接；
  **协议细节（第 3 章）不要进 README**——那是维护者文档；
- **两张表须同步**：代码里新增域（kb.DOMAINS）或调用点域预选时，
  README 的接入点表与本文 3.6 的预选表一并更新；
- 与 TODO.md 的关系：README 不描述限制与演进（第 5/6 章的活）。
