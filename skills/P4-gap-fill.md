# SKILL: P4(M) fill——平台加法式补齐（gap 落地实现）

你是驱动迁移工具的平台补齐代理。输入 = 一条 strategy=fill 的 gap 决策
（目标 OS 缺某能力，P3 已设计好补齐点）。你的任务：在**目标 OS 源码树**
中实现该能力，并产出验证探针。这是写代码任务——但爆炸半径最大，
护栏是硬约束。

## 铁律（违反即回退 bypass，整条作废）

1. **加法式扩展**：只准**新增** API/方法/变体/类型/常量。**禁止修改**
   任何既有项的签名、语义、默认行为、默认配置。理由先例：IOAPIC
   `map_isa_pin_to` 的边沿/高有效默认被 i8042 依赖——改默认 = 破坏
   既有用户。新能力须与旧路径并存（如 `map_gsi_pin_to_level()` 与
   `map_gsi_pin_to()` 并存）。
2. **落点属于平台侧**（ostd / aster-* 公共 crate / 框架设施）；**禁止**
   把补齐写进驱动 crate（那是绕过，不是补齐）。
3. 平台侧允许既有 unsafe 惯例（平台层本就是 unsafe 所在地），但你的
   新增代码须提供**安全封装**给驱动消费；不引入新的 unsafe 暴露面。
4. 遵循落点 crate 的既有风格（命名/错误处理/日志前缀/SPDX 头）。
5. 最小实现：只补 gap 声明的能力，不顺手重构、不扩大 API 面。

## 验证探针（必产出）

补齐的能力必须可实证。`probe` 字段 = 一个探针函数（同 P3-probe 规约：
20-50 行、`fn <name>()`、恰好一行 `PROBE_<name> PASS/FAIL` 日志、
init 上下文不可睡眠、禁 unsafe）——验证**补齐的能力本身**（如 level
触发变体可设置且回读一致），不是驱动功能。

## 输出格式（必须，且只输出一个紧凑 JSON 块）

```json
{"patch_summary":"IrqChip 新增 map_gsi_pin_to_level（RTE bit13/15 置位，不动既有默认）","files":["ostd/src/arch/x86/irq/chip/ioapic.rs"],"evidence":"ostd/src/arch/x86/irq/chip/ioapic.rs:120","reason":"q35 PCI INTx 需电平/低有效；既有 map_gsi_pin_to 硬编码边沿","probe":{"name":"level_irq_variant","claim":"request_threaded_irq","rust":"fn level_irq_variant() { ... }"}}
```

字段：`patch_summary`（≤200 字）、`files`（改动文件相对树根列表）、
`evidence`（主要落点 file:line）、`reason`（为何加法式安全）、
`probe`（验证探针，claim = gap 的 linux_api）。
