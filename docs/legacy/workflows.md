# 旧工作区的后续阶段

本页仅供已有 runner 与旧阶段产物的工作区使用。新 P0 的下游适配尚未完成，
不能按本页直通迁移；当前行为见[自主 P0](../p0-three-loops.md)。
旧 P0 建环境的流程已退役，历史材料通过 Git 查看。

#### P1 拆分策略

- **p1-strategy**（agent）：读 Linux 源码产出自由 Markdown 策略分析
  `strategy.md`，**人工审阅**（CP1 指纹绑定）。**意图在场时双产物**：
  正文含「迁移范围」节（纳入/排除/逐条响应意图）+ 尾部 ```json 块分离
  落盘 `P1/scope.json`（文件并集=迁移白名单，分组仅参考；文件必须全部
  位于驱动目录内，公共头仅作参考）。
- **p1-divide**（agent × 按文件 + 脚本）：索引预建→（scope 白名单过滤：
  只分配闭包内文件）→按文件 agent 分配→机械展开→物理抽取 →
  `P1/modules/<name>/`。
- **p1-resolve**（agent × ≤3 轮 + 脚本）：符号扫描→依赖图→环检测→agent
  搬运循环（守恒校验）→拓扑序 `deps.json`（循环输入）。3 轮败 → exit 3。

#### P2 探针预生成

- 默认暂停 **P2a 主轴提取和批量映射**；保留手动 `p2-map` 与已有映射数据。
  缺少映射表时初始化空表，后续 P3 按实际模块使用面增量映射。
- 复用 **旧工作区已验证骨架**，不重复施工/验收。骨架接口记录保存在 manifest，
  已有映射可获得验证批注。
- **P2c 探针预生成**：对已有映射中的风险主张前置验证；P3 按需补充。
- 为保持后续探针和迁移消费者兼容，骨架 recipe/manifest/journal 暂保留
  `P2/reports/scaffold_*` 路径；这些是旧工作区的产物位置。

#### P3-P5 垂直循环（×N 模块，拓扑序）

按 `deps.json` 拓扑序逐模块走 P3→P4→P5，`loop` 命令自动推进：

- **P3(M) 分析**（agent + 脚本）：使用面四分类→增量映射→gap 四策略处置
  （bypass/fill/register-fill/human）→判据草案→探针补新。gap human → exit 3。
- **P4(M) 生产**（agent + 脚本）：fill 统一（平台补齐）→切片迁移（≤900 行/片，
  映射作数据注入只翻译不研究）→轮末快速冒烟（compile+boot 防毒化）。
  blocked 立即停；同签名连发 2 次=零进展早退。
- **P5(M) 验收**（脚本 + agent 补探）：L1 build / L2 boot 双信号 / L0 ktest
  同场 / L3 qemu.log regex + 累积回归 + deferred 登记/清偿。失败走求解循环
  挂载①；耗尽 → `p5.unsolved.<M>` 关口。deferred 无法清偿 → exit 3。

#### P6 系统验收

- **聚合模式**（默认，零重测）：汇总各模块 acceptance + deferred + 判据状态
  + defects → `health.json/.md`。
- **--draft-l4**：从 deferred 系统判据 + P3 e2e 材料生成 L4 草案。
- **--finalize-l4**：L4 定稿门（CP3 审批，指纹绑定；config 配 agent/human）。
- **--execute [--l4]**：一轮 build + SLIRP boot + ktest → 全判据重判 + deferred
  清偿。失败走求解循环挂载②；耗尽 → `p6.unsolved` 关口。
- **--defect-diagnose ID**：单缺陷求解循环挂载③ → 四字段闭账 + CP4 债；
  耗尽 → `d1.unsolved.<did>` 关口。
- **defects 账本**：`--defect-add/close(四字段强制)/park/list`。

#### P7 终态报告

- **聚合**：P0→P6 全产物 + git baseline diff + crate/映射统计 + 补丁台账 →
  `final_report.json/.md`（数据驱动骨架，"结论与去向"节留人工撰写）。
- **CP4 缺陷闭账批审** + **CP5 知识沉淀提醒**（非阻塞）。
- **补丁台账**：`--patch-register` / `--patch-status`（planned|proposed|closed）。

### exp-mono：单体模块迁移 loop（实验形态）

与 `loop`（P3→P4→P5 垂直循环）相对：跳过映射/探针/判据中间层，一个
"研究+翻译一体"的迁移者 agent 一次迭代迁一个模块——验证单体 agent
形态的可行性。平台事实全部走工作区数据面，工具零目标 OS 假设（换
runner/manifest 数据即可跨目标复用）。

- 逐模块按 `P1/modules/deps.json` 的 `order` 推进；`exp-mono/ledger.json`
  记 pass 即跳过（幂等断点续）；blocked/失败/预算耗尽 → exit 1 停车，
  重跑从断点续。
- `--module` 单模块调试（须 order 内且依赖全 pass）；`--budget` 覆盖该
  模块 agent 预算（缺省 `clamp(900, 模块源行数×1.3, 4200)` 秒；编译/
  单测等静态段时长在预算之外）；`--session` 续接超时中断的 provider
  会话（各模块 session_id 记在 ledger，供人工续跑）。
- **三段复合 gate**（便宜先行失败短路，反馈回灌同 session）：
  ① 产物守卫：driver_home 非注释代码增量 ≥ `max(8, LOC//20)` + 构建
  单元登记核对 + git 改动白名单（driver_home + manifest 登记的接线
  文件，快照差分滤既有噪声）；② 构建：`runner.build` 原样（probe_build，
  rc0 + success_pattern）；③ 单测：`unit_test.driver_scope_cmd`（缺键
  降级 `unit_test.cmd` 全量，慢但正确；判据 rc0 + success_pattern 命中
  + fail_pattern 不命中）。
- **单测验收为记账制，无个数指标**：agent 在 done JSON 声明
  `migrated_functions` / `tests` / `untested` 三字段；编排器机械核对
  migrated ⊆ tests ∪ untested、同一单元不双挂、声明测试数 ≤ 测试标记
  实际增量；`report.md` 渲染每模块函数覆盖台账（已测 fn/aspect、豁免
  理由）供人审——覆盖质量归人，不归计数器。
- 工作区产物：`exp-mono/{ledger.json, mapping-notes.md（JIT 词典接力）,
  parking.md, logs/, report.md}`。

### exp-accept：整驱动系统验收（实验形态，接 exp-mono 终态）

模块都绿 ≠ 驱动作为系统工作（模块级 gate 有结构性盲区：整机集成
约束只有真启动才暴露）。本流程回答四层：**L1 编译 / L2 全量单测 /
L3 设备+驱动启动（驱动对设备参数有真实反应）/ L4 端到端**，外加
差分、负向与回归。平台事实 100% 走工作区数据面，工具零目标 OS /
驱动假设（跨目标复用同 exp-mono 哲学）。

- **判据来源三分**：L1/L2 与 L3 执行命令**机械绑定** runner 键
  （origin=frozen，agent 不可改）；L3 驱动反应锚点由 agent 从迁移
  产物代码**引用**（`cite.tree` 带 file:line/quote，工具 grep 核实，
  反幻觉）；L4 端到端全部 agent 生成——两处均须**人审放行**
  （gates 审批关口 + 方案指纹冻结；改动即失效）。
- **`--draft`**：P2b 直连形态（文件即信号 + 同 session 增量续接 +
  同轮 schema 微反馈不烧轮 + 静态证据核查回炉 ≤4 轮，零启动消耗）
  → 合并冻结判据 → `review.md`（frozen/proposed 分标）→ exit 3 待审。
- **人审**：answers.md 写 `## @exp-accept.plan` + `verdict: approve`
  （或 `gate answer` CLI）；作答与放行双点验指纹。
- **`--execute`**（断点续跑，ledger 记每单元结果+树头，绿且树未变
  即跳过）：幂等 prepare（脚手架落树 + wiring_cmd 接线 + 独立
  commit，脚手架内容此后冻结）→ 成本升序执行（L1 → L3 裸/注入 →
  L4 模板单元；红集汇总后才跑 L2 全量单测殿后）→ 每单元日志即
  快照归档、纯判定核心机械核对 → 红项先过 infra 分类（rc≠0 ∧ 日志
  缺失/空 → 重试 1 次）→ **自动修环**。
- **修环**（`run_agent_seq`，静态段=只重跑受影响单元+重判；改动守卫
  = git status ⊆ driver_home ∪ 登记接线文件；脚手架漂移自动还原）：
  agent 按 EXP-accept-fix 归责（infra/criteria/migration/platform），
  修好自动 commit 续跑；未解/数据面缺陷 → parking + `--redraft`
  人工环路。
- **诚实闸门**：收据必须内容派生（`expect.literal` 或
  `fill_sha256{byte,length}` 通用数学原语，"读什么"的语义活在数据
  里）；工作负载首行运行证明；跨单元 must-not（防日志串判/收据
  泄漏）；差分对（载荷锚 hit ∧ 无载荷基线 miss）。
- 工作区产物：`exp-accept/{acceptance.json, review.md, ledger.json,
  report.md, parking.md, logs/}`。
- 前置：exp-mono ledger 全 pass（本流程只接迁移终态）。新驱动
  （如块/网络/闪存类）跑完 exp-mono 后即插即用——判据/负载/脚手架
  全由 draft agent 按该驱动的可观测面设计。


子命令参数以 `python3 porter/main.py <command> --help` 为准。
