# asterinas 树内 qemu_args.sh 无设备注入钩子——runner 的 env 注入需要树侧配套 edit

**结论**：Asterinas 上游树（含 0.18.x 基线与迁移完成后的主树）的
`tools/qemu_args.sh` **没有任何 `EXTRA_QEMU_ARGS` 挂钩点**。runner 的
`inject_device` env 机制只负责把变量送进 boot 命令的进程环境；QEMU
实际参数由 `qemu_args.sh` 生成，**不消费该变量** → 设备根本没挂上，
boot 一切正常但驱动 probe 无设备可认领。

**诊断签名**（P2b 框架引导 2026-09-05 校准实录）：三信号中 build ✓
boot ✓、组件 init/probes 日志 ✓，唯独**认领特征 MISS** 且 qemu.log 无
任何该设备痕迹——先查 QEMU 命令行有没有 `-device <drv>`，再查驱动。

**正解**：接线面必须包含树侧钩子 edit（这就是历史 `_WIRING_FILES`
清单里躺着 `tools/qemu_args.sh` 的原因——手工迁移工作区私改过，从未
上游化）。在 `qemu_args.sh` 最终 `echo $QEMU_ARGS` 前插入：

```bash
if [ -n "$EXTRA_QEMU_ARGS" ]; then
    QEMU_ARGS="$QEMU_ARGS $EXTRA_QEMU_ARGS"
fi
```

**对 P2b 发现步骤的意义**：设备注入扩展点是接线面的一部分——施工单
发现时必须回答"boot 命令如何接受注入的设备参数"，缺了它三信号里的
boot_with_device 与设备无关地静默通过（校准 r1-r3 全败于此，r4 补
edit 后认领行 `claimed PCI device 8086:100e at PciDeviceLocation
{ bus: 0, device: 5, function: 0 }` 命中）。
