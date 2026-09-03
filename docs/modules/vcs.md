# git 管理模块（vcs）

> **定位**：全程 commit 管理两个 repo（目标 OS 源码树 + 迁移工作区），
> 记录"每个 step 干了什么"，支撑迁移结果的事后分析与优化；配套
> git bundle 跨机器可移植（导出/导入 commit 链）。
> 设计定案：2026-09-04（与用户逐点讨论确定，见 §5 定案记录）。
> 实现：`porter/common/vcs.py`（纯 Python 标准库，subprocess 调 git，
> 零第三方依赖）。

---

## 1. 定位与设计原则

1. **best-effort，永不阻塞**：任何 git 失败（无 git 二进制、坏仓、
   网络盘锁定……）只记 `[porter] vcs: ⚠️` warning，流水线照常推进。
   commit 管理是观测与留痕，不是门禁。
2. **两仓 commit 流互相独立**（定案）：目标 OS 仓只按既定点提交
   （P2 骨架后 / P4 每模块末 / P6 execute 后 / 求解修码后），
   **不为 agent 调用加目标树 commit 点**；工作区仓按阶段/模块/
   agent 调用/停车/答案消费提交。两仓 commit 之间不要求对应关系，
   即使它们事实上源于同一动作。
3. **agent 调用前后隔离**（定案）：每次非交互 agent 调用在工作区仓
   产生成对的 `pre-agent` / `agent` commit——`git diff <pre>..<post>`
   = 该次调用在 ws 侧的全部产物（prompt/log/其间报告）。目标树
   改动不在此列（原则 2）。
4. **零依赖 + identity 兜底**：subprocess 调 git；每次调用注入
   `-c user.name/user.email`（config `vcs.identity`）与
   `-c commit.gpgsign=false -c core.hooksPath=/dev/null`——容器/
   新机器常无全局 git 身份与干净 hook 环境，缺这些 commit 会失败。
5. **双兼容**：`vcs.enabled=false`（或 `PORTER_VCS=0`）时全部跳过，
   等价无 vcs 行为；旧工作区（project.json 无 `vcs` 节）回落
   "顶层目标树单仓、当前分支直提"。

## 2. 术语登记表

| 术语 | 定义 | 备注 |
|---|---|---|
| **目标 OS repo** | 用户的目标树自身 `.git`（运行机器上已有） | 一般一个；顶层不是仓则管理其下并行子仓 |
| **并行仓** | 目标树下互不嵌套的多个 git 仓 | 各自登记 baseline、统一分支名 |
| **嵌套坍缩** | 仓内有仓时只管最外层 | 定案：嵌套的只管顶层仓 |
| **porter 分支** | 每次迁移专用的全新分支 | `--os-branch` 指定（须不存在）或自动生成 `porter/<驱动>-<yyyymmdd>-<rand4>`；工作区仓同分支名 |
| **baseline** | P0 登记时各目标仓的 HEAD | bundle 增量导出与 P7 diff 的起点 |
| **隔离点** | agent 调用前后的 `pre-agent`/`agent` commit 对 | 仅工作区仓；台账相邻可查 |
| **台账** | `<ws>/vcs_commits.json`（commit 成功流水） | P7 commit 链的索引；不参与自提交（见 §3.3） |
| **bundle** | `git bundle` 单文件（保 commit hash） | 目标仓增量 `baseline..HEAD`；工作区全量 |
| **manifest** | `<ws>/exports/manifest.json`（导出清册） | branch + 每个 bundle 的 repo/baseline/文件名 |

## 3. 协议规范

### 3.1 两仓模型与 commit 点地图

**工作区 repo**（`<ws>/.git`，P0 时 `git init` + 同名 porter 分支 +
写 `.gitignore`）：

| 时机 | 挂点 | 消息（phase trailer） |
|---|---|---|
| P0/P1/P6/P7 阶段末 | `main.py:cmd_p0/cmd_p1/cmd_p6/cmd_p7` 成功路径 | `P0: done` 等（phase=P0…） |
| P2 阶段末 | `bootstrap/run.py:run_p2` 验收通过后 | `P2: done`（phase=P2） |
| loop 每模块 done（含绕行） | `loop/run.py:run_loop` 两处 | `loop: module <M> done`（phase=loop） |
| **每次 agent 调用前** | `common/agent.py:run_agent` 入口（写 prompt 前） | `pre-agent: <stem>`（phase=agent） |
| **每次 agent 调用后** | `run_agent` 末尾（log 落盘后） | `agent: <stem> rc=<rc>`（phase=agent） |
| exit 3 停车前 | `loop/gates.py:panic` 入口（13 处停车点全覆盖） | `stop: <gate_id>`（phase=<spec.phase 小写>） |
| answers 消费后 | `gates.py:process_answered_gates` applied>0 分支（8 处入口全覆盖） | `answers: N applied`（phase=gates） |
| kb promote 后 | `main.py:_kb_sync_and_commit`（p1/p2/kb promote 收尾） | `kb: promote`（phase=kb） |

ws 取点：`run_agent` 经 `log.store.bound()`；其余挂点天然持有 ws。
未绑定 / 仓未 init → no-op。

**目标 OS repo**（只按既定点，无 agent 点）：

| 时机 | 挂点 | 范围 |
|---|---|---|
| P2 骨架+验收后 | `run_p2` | 显式路径：manifest `created` + `TARGET_WIRING_FILES` |
| P4 每模块末 | `loop/p4.py:run_p4` 成功末 | 显式路径：`kernel/core/comps/<driver>` + 接线文件（fill+migrate 合一条） |
| P6 execute 后 | `cmd_p6`（execute 模式 rc=0） | status 捕获（全部登记仓 add -A） |
| 每次求解修码后 | `errorloop.py` fix-code 动作执行后（三挂载 p5/p6/d1 全覆盖） | status 捕获 |

### 3.2 分支管理

**登记**（`hook_p0`，P0 时幂等执行）：
1. `register_repos(target_os)`：扫描 `.git`（限深 6，跳过
   `target/target2/build/dist/out/node_modules`）；嵌套坍缩取最外层；
   逐仓记 `{"root": abs, "baseline": HEAD}`。顶层非仓且有并行子仓 →
   子仓各自管理。
2. 分支：`--os-branch` 指定且任一仓已存在 → **rc 2**（输入错，唯一
   的硬失败）；缺省自动生成并查重。各仓 `checkout -b`；工作区仓
   `git init` 后同分支名。
3. 写 `project.json["vcs"]`（工作区级，resume 依据——工具级
   `porter/config.json` 只放全局项）：

```json
{"branch": "porter/e1000-20260904-a1b2",
 "repos": [{"root": "/abs/target-os", "baseline": "<sha>"}]}
```

**resume 惰性校验**（每次 commit 前）：HEAD 已在记录分支 → 继续；
不在 → 切回（分支不存在则重建）；切换失败（树脏冲突）→ **跳过该次
commit 并告警**（不做 stash 等强保护，见 §5）。旧工作区无 `vcs` 节 →
顶层目标树是 git 仓则单仓兜底、当前分支直提。

### 3.3 commit 语义

- **显式路径**：`commit(repo, msg, paths=[相对 repo 根])`——路径过滤
  （不存在剔除）、空集跳过。P2/P4 用。
- **status 捕获**：`paths=None` → `git add -A`。P6/修码用。
- **幂等**：暂存区无变更（`git diff --cached --quiet` rc=0）→ 返回
  None，不产空 commit。重复 commit / 只读 agent 的隔离点天然消隐。
- **trailer**：phase 给定时消息尾附 `Porter-Phase: <phase>`（git-log
  重建与检索的锚）。
- **TARGET_WIRING_FILES**：crate 外已知接线面（根 Cargo.toml/
  Cargo.lock/Components.toml/kernel/core/Cargo.toml/driver/mod.rs/
  net/iface/init.rs），P2/P4 commit 范围的固定补集。
- **排除双保险**：台账与 exports 在工作区仓内但**不参与自提交**——
  commit 用 `git add -A -- . ':(exclude)vcs_commits.json'
  ':(exclude)exports'`，且 `<ws>/.gitignore` 再排除一次。原因：台账
  每次 commit 后必变（否则幂等被破坏、每动作多一条噪音 commit）；
  bundle 二进制入库会让下次导出递归膨胀。

- **兜底护栏**（2026-09-04 事故后加）：旧工作区单仓兜底仅在
  `target_os` 为**显式绝对路径**且自身是 git 仓时生效；空/相对路径/
  工具仓自身（`root == TOOL_ROOT`）一律拒绝，`_git` 另拒空/`.` 路径。
  原因：`Path("")` 归一化为 CWD，dirty-CWD 下 `git -C .` 会把进程
  所在仓误提交——开发期测试曾因此把工具仓误提交 8 条 `solve[d1]`
  commit（已 `git reset --mixed` 撤销；回归测试 E8-E11）。

### 3.4 台账与 P7 commit 链

台账条目（commit 成功即追加）：
`{"time", "repo_kind": target_os|workspace, "repo", "phase", "msg", "hash"}`。

P7 `commit_chain(ws)`：台账优先，逐条 `git show --name-status` 展开
文件清单（"哪次 commit 改了什么"）；台账为空（导入的工作区）→ 回退
`git log -z --format=%H%x1f%cI%x1f%B` 重建（按 `Porter-Phase:` trailer
恢复 phase，目标仓限 `baseline..HEAD`，跨仓按时间排序）。产出进
`final_report.json["commit_chain"]` 与 md 表格；既有 `baseline_diff`
保留兜底（捕获未提交残余）。

### 3.5 跨机器可移植（git bundle）

- **为什么 bundle**：保 commit hash（format-patch 经 `git am` 会换
  hash）；单文件单仓；`git fetch <bundle>` 直接建分支。约束：导入端
  必须有相同起点 commit（目标仓 = baseline），否则 fetch 报缺前置。
- **导出**（`porter vcs export` / P7 末自动 → `<ws>/exports/`）：
  目标仓 `bundle create <f> <baseline>..HEAD`（无 baseline 则 `--all`）；
  工作区仓 `bundle create <f> HEAD`（全量）；写 manifest.json。
- **导入**（`porter vcs import --bundle F --repo P [--branch B]`）：
  `git fetch <bundle> HEAD` → 无则 `git branch B FETCH_HEAD` 并切换；
  已有同名分支则 fetch 后切回。

### 3.6 配置

`porter/config.json` 的 `vcs` 节（工具级全局项；分支/baseline 在
工作区 project.json，见 §3.2）：

| 键 | 缺省 | 语义 |
|---|---|---|
| `enabled` | true | 总开关；false = 全部跳过（等价旧行为） |
| `identity.name/email` | porter / porter@local | git 身份兜底（`-c` 注入） |
| `target_os.enabled` / `workspace.enabled` | true | 分仓开关 |
| `export.format` | bundle | 导出格式（当前仅 bundle） |

环境变量 `PORTER_VCS=0/1` 强制覆盖 enabled（测试与一次性开关用）。
配置读取带 mtime 缓存（避免每次 git 调用重读文件）。

## 4. 实现地图（改进参考）

`porter/common/vcs.py` 函数分组：

| 组 | 函数 |
|---|---|
| 配置 | `_load_cfg`（mtime 缓存）/ `enabled(kind)` |
| 底层 | `_git`（identity+gpgsign+hooksPath 注入，永不抛）/ `head` / `is_dirty` / `current_branch` / `branch_exists` / `ensure_branch`（四态：在位/切回/新建/冲突拒）/ `init_repo` / `commit`（paths vs 捕获、幂等、trailer、exclude） |
| 仓发现 | `register_repos`（扫描+坍缩+跳构建目录）/ `gen_branch_name` |
| 登记 | `hook_p0`（幂等；resume 只补分支）/ `_load_proj` / `_save_proj_vcs` |
| 台账 | `_ledger_append` / `load_ledger` |
| 语义封装 | `commit_workspace`（分支惰性校验+排除）/ `commit_target`（路径→归属仓映射） |
| agent 隔离 | `agent_pre` / `agent_post`（仅 ws 仓，成对） |
| gitignore | `write_ws_gitignore`（幂等追加） |
| P7 链 | `_chain_files` / `commit_chain`（台账优先+git-log 回退） |
| 可移植 | `export_all` / `import_bundle` / `_safe_name` |

**接线 10 处**（§3.1 两表）+ CLI（`p0 --os-branch`、`porter vcs
export|import`）。集中挂点选择理由：exit 3 全部走 `gates.panic()`
（13 处调用点）、答案消费全部走 `process_answered_gates()`（8 处）、
agent 调用全部走 `run_agent` 唯一入口——各挂一处即全覆盖，未来新增
停车点/入口天然继承。

**配套改造**（同轮落地，与本模块的耦合点）：
- 知识库挪家：`<ws>/knowledge/` + `<ws>/knowledge/temp/`（随 ws git
  入库；`--kb use` 从全局库种子化、promote 后 `sync_to_global` 回流），
  kb 由此不单独建仓。规范见 `docs/sub-systems/knowledge.md` §3.1。
- fill 落点约束：P4 fill 平台补齐一律写
  `crate/src/external_interfaces.rs`（骨架预置 mod）——目标树改动
  集中在 crate 内，P4 模块末 commit 的显式路径即可覆盖。

## 5. 已知限制与定案记录

**限制**：
- 真实迁移轮 e2e 校准待做（TODO #10）：commit 粒度/分支切回/bundle
  往返的实战表现。
- resume 时目标仓被人切走分支且树脏 → 该仓 commit 跳过仅告警
  （无自动 stash；强保护待需求出现再议）。
- commit 消息自由文本（仅 trailer 有 schema）；i18n/前缀规范化未定。
- `git bundle` 导入端起点不匹配时只有错误提示，无自动对齐。
- **历史事故（已修复）**：`commit_target` 旧兜底曾用 `Path("")` 落
  CWD，测试进程在工具仓根运行时把工具仓误提交（详见 §3.3 兜底护栏）。

**设计定案记录**（2026-09-04 与用户逐点确认）：
1. 三 repo → **两 repo**：知识库挪入工作区（随 ws git 统一管理）。
2. 目标树**并行仓**各自登记（嵌套只管顶层仓）；分支名统一；
   工作区仓同分支名。
3. 分支/baseline 记 **project.json**（工作区级）而非工具级 config
   （多迁移共享会互相覆盖）。
4. commit 粒度：循环外按阶段、循环内按模块、求解修码按每次修改；
   agent 隔离点**只挂工作区仓**——"目标 OS 的 commit 和 workspace 的
   commit 可以不相关，就算它们真的有联系"。
5. 可移植形式 git bundle（需求：保全部 commit 信息 + apply 到同起点
   新 branch）。
6. 知识库挪家后 `--kb-git` 退役（兼容保留无效果）；`.hits.json`
   遥测旁车**不**ignore（随隔离 commit 记录该次调用咨询了什么）。

## 6. 测试

- `tests/test_vcs.py`（22 例，mock git 子进程）：底层封装（identity
  注入/禁用零调用/commit 流+台账/幂等/路径过滤）、分支四态、仓发现
  （并行+坍缩+构建目录跳过）、hook_p0（登记/冲突 rc2/resume 幂等/
  非_git 树）、commit_target（路径映射/捕获/旧工作区兜底）、
  export/import 命令构造、commit_chain 解析、CWD 误提交回归
  （缺/相对 target_os 与工具仓自身全拒绝，零 git 调用）。
- `tests/test_vcs_wiring.py`（11 例）：agent seam（成对消息/台账/
  no-op 三态）、**隔离语义**（真 git：`diff pre..post` 恰为该次调用
  产物）、接线点（panic/answers/loop/P2/P4 patch 断言）、
  .gitignore 幂等。约定：**不执行真 `run_agent`**。
- 手工冒烟（不入套件）：真 git 走 登记→commit→幂等→export→
  `git bundle verify`→clone 后 import→hash 保留；真 `run_agent`
  （opencode 缺席自然 rc=127）验证 agent.py 挂钩位置与成对性。
