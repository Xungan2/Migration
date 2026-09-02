# TODO（driver_migration_tool）

> 全局待办。按"已商定但不做在本轮"的原则记录；每条含背景与入手点。

## 1. 工作区级配置覆写通用机制

现状：仅 `routing.gates` / `routing.default` 支持工作区 `routing.json`
覆写（routing.py `load_routing`）；`checkpoints` / `policy_file` /
`panic` 阈值只认仓级 `porter/config.json`。

要做：设计统一的两级配置访问器（仓级默认 + 工作区覆写 + 特异性合并），
把 `_cp_config`、`debt_limit`、`policy_path` 等读取方全部收编。后续其他
功能（预计）也有工作区级覆写需求——做成通用机制而非逐功能补。

## 2. 工具 log 子系统重建（用户规划）

events.jsonl（结构化流水账）+ 失败快照是现有观测层。用户计划后续重建
工具的 log 子系统整体设计——届时 events.py 是重做对象，新框架的调用点
（panic 快照 / veto / 聚类 / 路由遥测）依赖其接口，重建时保持兼容或
一并迁移。

## 3. §15 失败自诊重设计

现状：`self_diagnosis.enabled=false`（整体 bypass，2026-09-02 用户决策
——该块未做明白）。bypass 的已接受代价：失败无自动分诊 → 走 attempts
（3 次）→ panic 停给人；`--defect-diagnose` / `--defect-fix` 休眠。

重设计要点：
- triage 五回路规则的账本化（gates 关口）与去 e1000/QEMU 硬编码；
- b_class / escalation 门的 legacy md 写盘收编（triage.py:510 /
  diagnose.py:210 两处，bypass 下不可达，重启用时先转账本）；
- **空日志/静默控制台回路优先重想**——bypass 后空日志会烧 attempts
  （probes.py `_note_empty_log` 已按新语义标注两种根因假设）；
- events 观测层保留不动（见第 2 条的关系）。

## 4. p6 私有 boot 助手与共享版去重（审计 #19）

现状：p6.py `_boot_and_log` 已接共享的 `_recover_boot_log` /
`_log_face`（最小接入，保持 SLIRP 注入不动），但仍是独立实现（返回
四元组含去 ANSI 文本 vs 共享三元组）。完整去重 = 共享助手支持
extra_env 注入 + ANSI 变体，p6 改调共享版。
