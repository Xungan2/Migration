# exp-accept 接线交接（2026-09-12）

> 面向两类读者：**接手 accept 侧续做者**（主体，§1-§4/§6）与
> **exp-mono 侧同学**（§5 接口契约与请求）。范围 = 接线剩余工作；
> 本日已落地部分的完整裁定链见 `AGENTS_2.md` §4「2026-09-12 第五
> session」条目（续①-④），本文不重复。行号以 2026-09-12 收口时
> 为准，会漂移——以函数名/符号 grep 为准。

## 1. 已完成一瞥（2026-09-12，五批，均未 commit）

| 批 | 内容 | 测试 |
|---|---|---|
| ① | 节归属修正：`SECTION_TIER={"t1":(1,2,3,4),"inject":(5,6),"e2e":(7,)}`；Tier2 只管 §5/§6；Tier1 骨架接线（关口①`exp-accept.t1`/CLI/`_run_tier` 泛化）；frozen 机器退场 | 全绿 |
| ② | 知识注入面全指针化（`_kb_face`/`_boot_face`）；runner.json 从 accept 全链脱钩 | 全绿 |
| ③④ | driver_home 单源 = `module-divsion.json`（旧源裁定不兼容留） | 全绿 |
| ⑤ | G4 接入：`_mono_knowledge_face`（契约登记表+词典指针） | 全绿 |

终态基线：`tests.test_accept` 23/23、全量 **174/174**。核心裁定三条：
prompt 知识注入**一律指针**（禁无依据 head-N）；runner.json **退役方向**
（accept 零引用）；`_T1_READY=False`——**Tier1 机制留空系用户明令**
（"具体通过什么方式目前还没定"），stub 为纯空壳，设计讨论归
AGENTS_2.md、代码不预设。

## 2. 接线终态速查表

**硬前置**（缺席 rc 2，accept.py:753-780）：
- `project.json`（`target_os` 键）——prepare 产物
- `module-divsion.json`（顶层 `driver_home`）——pre-mono 产物，**单源**
- `exp-mono/ledger.json`（全模块 pass）——mono 产物

**软注入**（全指针，inject/e2e/execute 三 prompt 的「背景数据」节）：
- `goals.md`、`exp-mono/{report,parking,negatives}.md` ——直接指针
- `_boot_face`（accept.py:148）：`runner.md` + `exp-mono/logs/`
- `_kb_face`（accept.py:134）：`knowledgebase/{build,boot}/README.md`
- `_mono_knowledge_face`（accept.py:160）：`exp-mono/contracts.md`
  （改码前必查）+ `mapping-notes.md`；均带"系 mono 快照、疑则以树内
  代码为准"漂移警示；缺席省略

**裁定不接**（AGENTS_2.md 续④，含理由与触发条件）：G2 hints、
G3 余域、G5 HUMAN.md、G6 goals 缺失容忍、G9 handoff 发布。

## 3. 剩余工作

### 3.1 Tier1 机制（唯一大项）

骨架在位、机制留空。**已具备**：
- `SECTION_TIER["t1"]=(1,2,3,4)`（accept_exec.py:39）、`_GATE_OF/
  _PREFIX_OF/_prompt_fn`（accept.py:270-282）——`_run_tier`
  （accept.py:284）对三 tier 通用：结构校验→全量终验（逐节真实
  invoke）→commit→评审摘要→关口登记，全部复用；
- CLI `--tier t1`（显式要求 → rc 2"机制未实现"）；默认路由打
  "Tier1 未实现——§1-§4 留空跳过"后继续；
- 关口① `exp-accept.t1` 常量在位；测试覆盖
  （`test_t1_skeleton_not_ready`、`_ready_index` 预播 §1-§4 模拟
  未来 Tier1 产物使执行相位可测）。

**留空边界（勿越）**：`_t1_prompt`（accept.py:178）是纯空壳
（raise NotImplementedError）。启用 = 实现 prompt + 写
`porter/skills/EXP-accept-t1.md`（常量 `SKILL_T1` 已在）+ 置
`_T1_READY=True`（accept.py:67）——届时 `--tier t1`、自动路由、
关口①全链自动激活，零新增编排代码。

**落地时要定的设计问题**（上 session 有草案备忘在 AGENTS_2.md
§4「同事 mono 适配拉入」条 C/D，**非承诺、可推翻**）：
1. 输入源与提取方式：runner.md 七固定标题（mono.py:66-79 结构
   契约，与 §1-§4 近乎一一对应——模块编译/镜像编译/设备自启动/
   单元测试）vs exp-mono/logs 反推 vs 亲读树代码（§4 锚点判据）；
2. 禁改码守卫变体：现 `_run_tier` 复用的守卫是 driver_home ∪
   paths 白名单（"agent 直接改码"语义）；t1 需收紧为**树零变更**，
   修复方向反转——invoke 红只许修节文件或上报，永不修代码让标准
   过（"忠实提取应当立即全绿，红本身是信号"）；
3. §2 零测试计数防线（min_matches，计数可取 mono ledger）；
4. 出处可溯：提取自哪个源写节 notes 供人审。
   交接时点的一个既定事实：源序讨论中 runner.json 已裁退役，
   若草案提"三源"按两源（runner.md > logs）理解。

### 3.2 交接时已清的欠账（勿重做）

AGENTS_2.md 续④补记（五项不接裁定收口）、`_t1_prompt` 削纯空壳
——均于本文落盘同轮完成。**仍欠**：五批改动的 commit 切分
（建议按 §1 表五行切语义 commit，或一个大 commit，待用户裁定）。

### 3.3 真跑准备（spinor-mini）

工作区 `migrations/spinor-mini-e2e-20260911/ws-spinor-mini` 为旧
形态：只有 `P2/reports/scaffold_manifest.json`、**无
module-divsion.json**——accept 前置会 rc 2。跑前补种：

```bash
python3 - <<'EOF'
import json
ws = "migrations/spinor-mini-e2e-20260911/ws-spinor-mini"
dh = json.load(open(f"{ws}/P2/reports/scaffold_manifest.json"))["driver_home"]
json.dump({"order": [], "modules": {}, "driver_home": dh, "unknown": []},
          open(f"{ws}/module-divsion.json", "w"))
EOF
```

之后全链 = `accept --tier t1`（机制落地后）→ 关口① → inject →
关口② → e2e → 关口③ → `accept --execute`。

## 4. 验证与复现

```bash
cd Migration
python3 -m unittest tests.test_accept          # 23 例，全 mock agent + 真实子进程 invoke
python3 -m unittest discover -s tests -q       # 全量 174
python3 porter/main.py accept --help           # --tier {t1,inject,e2e} 接线冒烟
```

fixture 两要点（tests/test_accept.py）：刻意**不建 runner.json**
（证明脱钩）；执行相位测试**预播 §1-§4 节对**模拟未来 Tier1 产物
（真 Tier1 落地后可改为真跑覆盖）。

## 5. 给 exp-mono 侧的接口契约与请求

**accept 消费你们的产物清单**（缺则 rc 2 或降级，均为 feat/prepare
现行为）：

| 产物 | 消费点 | 约束 |
|---|---|---|
| `module-divsion.json` | driver_home **单源** | 顶层 `driver_home` 必须在场（mono 本身也强制） |
| `runner.md` | inject/e2e prompt 指针 | 七固定标题结构（你们 `_read_runner_md` 已校验的同一套） |
| `exp-mono/ledger.json` | 准入前置（全 pass） | — |
| `report/parking/negatives/contracts/mapping-notes.md`、`logs/` | prompt 指针（缺席省略，无硬约束） | 措辞按"以树内代码为准"消费，不锁你们的内容形态 |

**请求**：mono gate 的实际执行命令经 `append_runner`
（porter/workspace.py:46，**API 在场、零调用点**）记入 runner.md
执行记录（一行调用即可）。这是 Tier1 提取质量的保障——决定 §1-§4
提到的是"真跑过的命令"还是"手册写的应该这么跑"。Tier1 开工时
此请求优先级上升。

**G9 现状**：accept 不发布 handoff（`accept`/`accept.execute` 索引
未接）——编排脚本**勿依赖** `require_success('accept')`；触发重议
条件 = accept 出现下游消费者。

## 6. 知识入口与已知坑

- 完整裁定链：`AGENTS_2.md` §4 2026-09-12 条目（续①-④）+ §7 路线
- 关键代码：accept.py（prompt 面 134-235 / tier 编排 284 / execute
  586 / 前置与主流程 753-880）、accept_exec.py（SECTION_TIER 39 /
  invoke_ladder / fingerprint_problems）、accept_gate.py（关口协议）
- 坑：①指针原则是**硬约束**——未来任何注入面不得 head-N 截断；
  ②runner.json 残余消费者只剩 mono 机器 gate（你们领土），accept
  已零引用；③`--execute` 在 §1-§4 未绑定（Tier1 未落地）时被前置
  检查 rc 2 挡住并提示——这是设计行为不是 bug；④执行相位测试对
  §1-§4 的覆盖靠 fixture 预播，真 Tier1 落地后注意同步。
