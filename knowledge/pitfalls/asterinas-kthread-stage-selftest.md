# 内核自测挂 kthread 组件阶段（Bootstrap 禁 spawn 的正解）

**结论**：Asterinas 组件系统有三级初始化阶段（`component::InitStage`）：
`Bootstrap`（OSTD init 后、子系统 init 前）、`Kthread`（首个内核线程上下
文，SMP 就绪 + 调度器运行 + 本地中断开）、`Process`（首进程后）。需要
**真设备 + 开中断 + 等外部对端**的驱动级自测（open/close、寄存器回读、
ARP 往返、软触发中断），正确宿主是：

```rust
#[init_component(kthread)]   // component crate 宏支持 stage 参数
fn driver_l4_selftest() -> Result<(), ComponentInitError> { … }
```

而不是在 Bootstrap 期 probe 尾部 spawn 内核任务——Bootstrap 上下文
调度器未就绪（`Task::yield_now` 与调度器惰性注入冲突 panic，
migration/e1000 §13 实测；探针宿舍 probes.rs 亦据此不调 spawn）。

**设备句柄传递**：probe（Bootstrap）把具体型
`Arc<SpinLock<E1000NetDevice, _>>` 存 crate 级 `spin::Once` 单例
（在向 `Arc<SpinLock<dyn AnyNetworkDevice, _>>` trait 强转注册**之前**），
kthread 自测从单例取用——绕开 dyn 不可 downcast 的问题。

**范式产出**：e1000 `src/l4_selftest.rs`（P6）——逐条判据打
`L4 <id> PASS|FAIL` 日志行，编排器从 boot 日志 grep 判定（boot 观测与
ktest 之外的第三种验收执行形态）。

**注意**：kthread 阶段自测里的等待用 TSC 忙等（udelay 封装）——此时
调度器虽就绪，但判据时间窗是秒级有界，忙等换取时序确定性与无依赖。
