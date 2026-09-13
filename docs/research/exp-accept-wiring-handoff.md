# exp-accept 接线交接（2026-09-12）

> 面向两类读者：**接手 accept 侧续做者**（主体，§1-§4/§6）与
> **exp-mono 侧同学**（§5 接口契约与请求）。范围 = 接线剩余工作；
> 本日已落地部分的完整裁定链见 `AGENTS_2.md` §4「2026-09-12 第五
> session」条目（续①-④），本文不重复。行号以 2026-09-12 收口时
> 为准，会漂移——以函数名/符号 grep 为准。

## 1. 已完成一瞥（2026-09-12，五批，均未 commit）

| 批 | 内容 | 测试 |
|---|---|---|
| ① | 节归属修正：`SECTION_TIER={"t1":(1,2,3,4),"inject":(5,6),"e2e":(7,)}`；Tier2 只管 §5/§6；Tier1 agent 接线（关口①`exp-accept.t1`/CLI/`_run_tier` 泛化）；frozen 机器退场 | 全绿 |
| ② | 知识注入面全指针化（`_kb_face`/`_boot_face`）；runner.json 从 accept 全链脱钩 | 全绿 |
| ③④ | driver_home 单源 = `module-divsion.json`（旧源裁定不兼容留） | 全绿 |
| ⑤ | G4 接入：`_mono_knowledge_face`（契约登记表+词典指针） | 全绿 |

接线前基线：`tests.test_accept` 23/23、全量 **174/174**。核心裁定三条：
prompt 知识注入**一律指针**（禁无依据 head-N）；runner.json **退役方向**
（accept 零引用）。Tier1 现已由 agent 从 mono 事实提取，契约见
`porter/skills/EXP-accept-t1.md`。

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

**仍不接**（含理由与触发条件）：G2 hints、G3 余域、G5 HUMAN.md、
G6 goals 缺失容忍。

## 3. 剩余工作

### 3.1 Tier1 机制（已完成）

Tier1 agent 已启用。**具备**：
- `SECTION_TIER["t1"]=(1,2,3,4)`（accept_exec.py:39）、`_GATE_OF/
  _PREFIX_OF/_prompt_fn`（accept.py:270-282）——`_run_tier`
  （accept.py:284）对三 tier 通用：结构校验→全量终验（逐节真实
  invoke）→commit→评审摘要→关口登记，全部复用；
- CLI `--tier t1` 与默认自动路由均运行事实提取 agent；
- 关口① `exp-accept.t1` 常量在位；测试覆盖 Tier1 路由、结构校验与
  目标树零变更守卫。

Tier1 只允许写 `exp-accept/acceptance/` 节文件；目标树变更会被静态段拒绝。

提取源序为 `runner.md` 与 `exp-mono/logs/`；没有证据的节标记
`blocked`，不运行新实验。mono 的 runner 记录属于共享 append-only 日志。

### 3.2 交接与验证

`mono`、`accept` 与 `accept.execute` 均按成功边界发布 handoff；失败、
中断或输入指纹变化不会发布成功交接。目标树变更守卫、runner append-only
处理和旧 fixture 兼容均有测试覆盖。

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

之后全链 = `accept --tier t1` → 关口① → inject →
关口② → e2e → 关口③ → `accept --execute`。

## 4. 验证与复现

```bash
cd Migration
python3 -m unittest tests.test_accept          # 23 例，全 mock agent + 真实子进程 invoke
python3 -m unittest discover -s tests -q       # 全量 174
python3 porter/main.py accept --help           # --tier {t1,inject,e2e} 接线冒烟
```

fixture 两要点（tests/test_accept.py）：刻意**不建 runner.json**
（证明脱钩）；执行相位测试保留历史 fixture 的 §1-§4 预播兼容路径。

## 5. 给 exp-mono 侧的接口契约与请求

**accept 消费你们的产物清单**（缺则 rc 2 或降级，均为 feat/prepare
现行为）：

| 产物 | 消费点 | 约束 |
|---|---|---|
| `module-divsion.json` | driver_home **单源** | 顶层 `driver_home` 必须在场（mono 本身也强制） |
| `runner.md` | inject/e2e prompt 指针 | 七固定标题结构（你们 `_read_runner_md` 已校验的同一套） |
| `exp-mono/ledger.json` | 准入前置（全 pass） | — |
| `report/parking/negatives/contracts/mapping-notes.md`、`logs/` | prompt 指针（缺席省略，无硬约束） | 措辞按"以树内代码为准"消费，不锁你们的内容形态 |

**已完成**：mono 单测 gate 的实际执行命令经 `append_runner`
（porter/workspace.py）记入 runner.md，供 Tier1 提取真实证据；构建与
启动 probe 也会追加对应命令和日志指针。

**G9 现状**：mono 完成后发布 `mono` handoff；新工作区的 accept 启动时强制消费，
七节标准全部绑定后发布 `accept` handoff；`--execute` 七节复验通过后发布
`accept.execute` handoff。

## 6. 知识入口与已知坑

- 完整裁定链：`AGENTS_2.md` §4 2026-09-12 条目（续①-④）+ §7 路线
- 关键代码：accept.py（prompt 面 134-235 / tier 编排 284 / execute
  586 / 前置与主流程 753-880）、accept_exec.py（SECTION_TIER 39 /
  invoke_ladder / fingerprint_problems）、accept_gate.py（关口协议）
- 坑：①指针原则是**硬约束**——注入面不得 head-N 截断；
  ②runner.json 只供 mono 机器 gate，accept 已零引用；③`--execute`
  在 §1-§4 未绑定时由前置检查 rc 2；④accept 只消费成功的 mono handoff。
