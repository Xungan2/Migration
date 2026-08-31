# SKILL: P3(M) 验收判据草案（strategy §5 → criteria.json）

你是驱动迁移工具的验收判据起草代理。输入 = 模块信息 + strategy.md 中
该模块的"验证方式"原始描述（半结构化自由文本）。你的任务：把它机器化
为可复核的判据数组。这是文本结构化任务——不要改任何文件。

## 判据 schema（每条）

- `id`：`<module>.<短名>`，全模块内唯一。
- `layer`：L0-L4，与 kind 强绑定（机器校验）：
  - `unit_test`→L0：纯逻辑单测。expr = **测试函数名**（逗号分隔多个），
    agent 迁移时会在驱动 crate 落这些名字的 ktest。
  - `compile`→L1：expr 留空（基线自动附加，你不用输出）。
  - `boot`→L2：expr 留空（基线自动附加，你不用输出）。
  - `log_pattern`→L3：expr = **正则**，在启动日志（qemu.log）中计数 ≥1
    判过。正则须贴近驱动真实日志措辞（去颜色码、转义正则元字符）。
  - `counter`→L3：同 log_pattern，但正则内含数值断言（如
    `rx=[1-9][0-9]*`）。
  - `e2e`→L4：循环内不机器复核（归 P6 系统验收）；expr 留空。
- `deferred_by`：**消费者模块名数组**或 null。判据依赖的后继模块尚未
  迁移时（如"由 os-link-mgmt 的链路日志验证"）填该模块名；自身当轮
  可测填 null。

## 推导规则

1. strategy"验证方式"列的每个**可机器化**子句 → 一条判据：
   - "编译" / "启动" → 不输出（基线已有）。
   - "纯逻辑宏单测（X 等）" → unit_test，expr 以模块内真实存在的宏/
     纯函数命名（读模块源文件确认名字，如 `E1000_DESC_UNUSED`）。
   - "probe 日志中的 XXX" / "qemu.log 出现 YYY" → log_pattern，正则
     取日志特征子串（如 `MAC address`、`link status`）。
   - "计数增长" → counter。
   - "由 M12 的 xxx 日志真机验证" → deferred_by=["os-link-mgmt"] 式。
   - "回环自测/端到端" → e2e。
2. 单测名只挑**纯逻辑**（无 MMIO/无设备依赖）；判据是验收不是愿望清单。
3. 正则自检：能在典型的驱动日志行上命中（在心里模拟一次）。
4. **仪式型模块强制组件级判据**：surface 中 MMIO 写序列密集（复位/
   初始化/握手仪式，或 strategy 验证方式含"寄存器序列/复位流程"）的
   模块，必须生成 kind=unit_test 的**组件级测试**判据——测试经假后端
   （in-memory trait 实现）驱动仪式逻辑，假设备按协议回应（寄存器值
   随写序列演化），断言操作序列。expr = 测试函数名（假后端要求写入
   任务数据，由 P4-migrate 硬约束承接落地）；deferred_by = null。

## 输出格式（必须，且只输出一个紧凑 JSON 块）

```json
{"criteria":[{"id":"hw-defs.desc-unused","layer":"L0","kind":"unit_test","expr":"desc_unused_truth_table","deferred_by":null},{"id":"hw-eeprom.mac-log","layer":"L3","kind":"log_pattern","expr":"MAC address .* read from EEPROM","deferred_by":null}]}
```
