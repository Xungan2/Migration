# OSTD IOAPIC 仅边沿/高有效——PCI INTx 永不送达（附 q35 GSI 公式）

**结论**：Asterinas OSTD 的 `map_gsi_pin_to` 使能路径把 IOAPIC 重定向表项
高 32 位写 0（`ostd/src/arch/x86/irq/chip/ioapic.rs` 非重映射分支：
`IOREDTBL[2i+1] = 0`）→ bit15 触发=边沿、bit13 极性=高有效。PCI INTx 按
规范为电平触发/低有效 → 设备拉线后 **CPU 永不收到中断**。

**为何全树至今无感**：现有中断用户 virtio/nvme 全走 MSI-X（DMA 写
LAPIC，不经 IOAPIC），i8042 走 ISA（`map_isa_pin_to`，边沿高有效恰好
正确）。**首个纯 INTx 设备驱动必撞**（QEMU 的 e1000 无 MSI）。

**判别特征**：设备侧一切正常（软触发 ICS 后 ICR 相应位置位、读清可见；
TX/RX 照常轮询工作），唯独回调计数恒 0——极易误判为驱动注册代码 bug。

**修法**（加法式，提案 `P7/reports/patches/ioapic-level-trigger.md`）：
新增 `map_gsi_pin_to_level(line, gsi, active_low)`；**不得**改既有默认
（会断 ISA 键盘）。

**q35 INTx→GSI 公式**（OSTD 无 ACPI _PRT 解析，驱动侧自算）：
`GSI = 16 + ((device_number + interrupt_pin - 1) % 4)`，pin=cfg 0x3D
（INTA=1..4），仅 bus 0。实证：e1000 @ slot 5 INTA → GSI 17。
cfg 0x3C（InterruptLine，SeaBIOS 写的 8259 IRQ）**不可用**。

**实证**：e2e-test-retry P6（ics kick evidence: ICR=0x14 而 irq_count=0）
+ 手工迁移项目 DECISIONS #35（独立发现，同结论）。

**连带**：INTx 交付修通前，被动收包（LISTEN 场景）会饥饿——smoltcp
poll 线程是事务驱动的（migration/e1000 DECISIONS #50）；guest 主动发起
的流量不受影响。
