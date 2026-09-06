# 挂设备的方法

QEMU 参数注入：以前笔记说源码树 tools/qemu_args.sh 末尾留了钩子吃
EXTRA_QEMU_ARGS 环境变量——**这个说法过时了，别信**。现在的树里
qemu_args.sh 全文和整棵树都 grep 不到这个变量，没有任何 env 钩子。

真实通路是 OSDK 的旗标：`--qemu-args`（追加重加 QEMU 参数，挂设备用
这个）和 `--kcmd-args`（塞内核命令行）。Makefile 里这些参数都累积在一个
叫 `CARGO_OSDK_BUILD_ARGS` 的变量里传给 `cargo osdk run`。要从外面注入，
得在 make 命令行上整体覆盖这个变量（GNU make 命令行变量优先于 Makefile
里的 := 赋值）——注意是**整体替换**，覆盖时必须把默认那几项
（loglevel/earlycon/console 的 kcmd、init-args、boot-method、
grub-boot-protocol、initramfs、-accel kvm）逐项重建一遍再追加自己的，
漏一项 bundle 拓扑就坏了。

注意：
- 默认 q35 机型有 PCI。别用 microvm（-machine microvm），没 PCI 总线，
  QEMU 直接报 "No 'PCI' bus found" 挂给你看。
- 挂网卡这类带真实网络后端的：-netdev user,id=e1 -device e1000,netdev=e1
  这种形态（只是验证设备挂载的话 peerless 的 -device e1000 就行）。
- `--netdev`/`-device` 是追加语义，和默认挂的 virtio-net-pci 并列共存，
  不会顶掉默认设备。
- 想在 qemu.log 里看到驱动 probe 期的日志（PCI/virtio 初始化那段），
  必须加 `CONSOLE=ttyS0`：默认 console=hvc0 拓扑下串口被分流到
  qemu-serial.log，console 驱动又初始化得比设备 probe 晚，那段日志
  物理上进不了 qemu.log——ttyS0 拓扑下串口就是 mux，才看得见。
- NETDEV=user 模式每次随机抽 hostfwd 端口，偶尔撞上宿主机已占端口
  （现象：qemu.log 空且 make 报 Error），重跑就好，瞬时故障。
- PCI 枚举日志在 logger 起来之前就打了，qemu.log 里看不到——设备有没有
  被系统认到，得等驱动 probe 之后才有日志（树里没的驱动就只能验证
  "挂上后启动不炸"）。
