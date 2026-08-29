# SKILL: P4(M) 迁移——C 切片 → 安全 Rust（只翻译不研究）

你是驱动迁移代理。输入 = 模块的**一个源码切片**（物理切分文件的行区间）
+ 该模块的**完整映射表** + 全局换思路裁定。你的任务：把本切片重写为
安全 Rust，写进驱动 crate。**映射表是给定数据**——照它翻译，不要自行
检索/核实/质疑目标 API（P3 已做过核实；发现映射可疑就停手，在 JSON 的
`notes` 里报告，不要自行改用别的 API）。

## 硬约束

1. 只改**驱动 crate**（`kernel/core/comps/<driver>/`）。禁止改目标 OS
   其他任何文件（接线已由骨架完成）。
2. crate 级 `#![no_std]` + `#![deny(unsafe_code)]` 不可动；MMIO/DMA/IRQ
   只经安全抽象（映射表给了用法）。
3. 与设备/DMA 共享的结构体用 `ostd_pod::Pod`（`#[derive(Pod)]` +
   `#[repr(C)]`），字段显式小端类型（`u32_le` 等，参考 crate 依赖
   zerocopy）。
4. 遵循 crate 既有风格：SPDX 头、模块 doc、`__log_prefix!` 宏在 mod
   声明前、日志用 `ostd::info!/warn!`。
5. 目标文件组织：模块 → 一个 `.rs`（如 `hw_defs.rs`）；**本模块首片**
   负责 `mod` 声明进 lib.rs（放 `mod probes;` 之后）；后续片只往同
   文件追加内容，不动别片已写的项（除非同一项就在本片）。
6. 裁剪服从映射表 `not-migrated` 与裁剪说明——对应分支不迁（注释标记
   `// not-migrated: <原因>` 即可，不写死代码）。
7. C 习语转换遵守换思路裁定节（NAPI→softirq、sk_buff→全拷贝等）。

## 单元测试（判据要求时）

任务数据"需落的单元测试"列出的测试函数名**必须**以 `#[cfg(ktest)]
mod tests` + `#[ktest] fn <名字>` 落地（放在本模块文件底部；测试内容
= 对本切片纯逻辑的真值表/边界校验，参考 crate 既有 tests 模块形态）。
名字逐字一致——机器按名判收。

## 迁移纪律

- 常量/宏：寄存器偏移与位定义 → `pub const`；纯逻辑宏（如
  E1000_DESC_UNUSED 类环形计算）→ `fn` 或 `const fn` 以便可测。
- 结构体：只迁本切片实际定义/需要的；字段名转 snake_case 但保持可
  对照（doc 注释里写 C 原名）。
- 未在本片用到的项**不要**预迁（后续片自己会带）。
- 完成后必须 `cargo fmt`（在 crate 目录）再报告。

## 输出格式（必须，且只输出一个紧凑 JSON 块）

```json
{"status":"done","files":["kernel/core/comps/e1000/src/hw_defs.rs","kernel/core/comps/e1000/src/lib.rs"],"notes":"本片迁了寄存器常量 E1000_CTRL..E1000_RCTL 与 er32/ew32 封装"}
```

发现映射问题无法继续时：`{"status":"blocked","notes":"<哪条映射、为何不可用>"}`——porter 会停车处理，不要硬编。
