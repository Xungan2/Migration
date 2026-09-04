# 组件 crate 无 host 显式引用则 init 钩子静默不执行（链接裁剪陷阱）

**结论**：在 Asterinas 里新增组件 crate，光有 workspace members 收编 +
`Components.toml` 登记**不够**——host crate（`kernel/core`）若无对它的
显式引用，链接器会把整 crate 裁掉，`#[init_component]` 钩子**静默不
执行**：无报错、无日志，组件像不存在一样。e1000 手工迁移轮 7 实测
（migration/e1000 PROGRESS.md 轮 7）。

**诊断签名**：crate 编译通过、`Components.toml` 正确，但 boot 日志里
没有组件的任何输出（无 "Component initializing"、无认领行）——查
host 引用，别查组件本身。

**正解**：接线三件套缺一不可——
1. 根 `Cargo.toml`：`members` 数组（×2）+ `[workspace.dependencies]`
   `aster-<drv> = { path = "kernel/core/comps/<drv>" }`
2. `Components.toml` `[components]`：`<drv> = { name = "aster-<drv>" }`
3. `kernel/core/src/driver/mod.rs`：`#[expect(unused_imports)]`
   `use aster_<drv>::*;`（下划线 = crate 名连字符转换）——**这就是
   防 GC 的显式引用**，i8042 等既有组件同款仪式

**对 P2b 框架引导的意义**：发现式施工单在 Asterinas 目标上必须包含
第 3 项，否则三信号验证的验收特征（认领日志行）必然 MISS 且无任何
编译期线索——回炉时优先核对 host 引用。
