<!-- hint-v1-dmzero -->
# 设备注入提示（dm-zero 场景——本能力与硬件驱动场景本质不同，如实处理）

## dm-zero 是软件设备，没有模拟器硬件注入形态

dm-zero 是 device-mapper 的 target 插件（纯软件块设备），QEMU 里**没有**
对应的硬件设备型号可以 `-device` 挂载。不要给 dm-zero 构造 e1000 那样的
硬件注入形态——那不是本场景的语义。

本场景的"注入"语义 = **把建表参数经内核 cmdline 送达**：Linux 侧的形态是
`dm-mod.create=<name>,<uuid>,<minor>,<flags>,<table>`，table 为
`<start_sector> <num_sectors> <target_type> <target_args>`（如
`dm-mod.create="dmz,,,rw, 0 1 zero"`）。注意 table 字段内含空格，而
命令要过 docker→bash→make→cargo 四层引号嵌套，引号处理要实测验证。

## 注入通路（已验证的 OS 级机制）

- OSDK 有 `--kcmd-args` 旗标（内核命令行参数），与 `--qemu-args` 同族；
  这些参数经 make 变量 `CARGO_OSDK_BUILD_ARGS` 传给 `cargo osdk run`。
- 从外部注入 = 在 make 命令行**整体覆盖** `CARGO_OSDK_BUILD_ARGS`
  （GNU make 命令行变量优先于 Makefile 内的 := 赋值）。整体替换语义：
  覆盖值必须逐项重建默认项（loglevel/earlycon/console 的 kcmd、
  --init-args=/test/boot_hello.sh、--boot-method=grub-rescue-iso、
  --grub-boot-protocol=multiboot2、--initramfs=...、
  --qemu-args="-accel kvm"）再追加自己的 `--kcmd-args="<DEVICE_ARGS>"`。
- cmd_suffix 用 `;` 开头的自包含完整命令形态（编排器把 suffix 原样
  追加到整条 boot.cmd 末尾、引号之外同一层 shell）——此形态已实测可用。
- 载体值含 `<DEVICE_ARGS>` 占位符（执行时替换为实际建表参数实例）；
  `example_args` 键 = 类别，值 = 一个建表参数实例（软件设备的"设备参数"
  就是 cmdline 里的建表串）。
- 早期日志要到 qemu.log 需 `CONSOLE=ttyS0`（见 boot 提示）。

## 差分锚点（唯一可能的诚实证据）

若内核 cmdline 内容被回显进判定日志（qemu.log），差分特征可以锚定
cmdline 里注入轮独有的内容（建表串）——"注入轮命中 ∧ 裸轮未命中"。
cmdline 是否回显、unknown 参数（如 dm-mod.create=）内核是否接受，
**没有先验证据，须你实测**（注意 Asterinas 树没有 dm 框架，不存在
"dm 驱动 probe 打印"这种锚点）。

## 诚实约束（刚性）

- 树内无该类别软件设备的驱动框架 → `driver_success_pattern` 若填，
  只能锚 cmdline 回显这类真实可观测信号；检索都没做不许填 null。
- 终验是差分判定：锚在两轮都命中的恒真特征会被打回。
- 若 cmdline 回显不成立、无法建立任何诚实证据：**按协议耗尽总结转
  人工**，把"软件设备无硬件注入通路/机制缺第三态"作为问题交给开发者。
  禁止伪造硬件注入形态、禁止恒真特征凑判定。
