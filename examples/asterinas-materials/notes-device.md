# 挂设备的方法

QEMU 参数注入：源码树 tools/qemu_args.sh 末尾留了钩子，吃环境变量
EXTRA_QEMU_ARGS，值就是一段 QEMU 设备参数。比如挂个 e1000 网卡就是
EXTRA_QEMU_ARGS="-device e1000"。

注意：
- 默认 q35 机型有 PCI。别用 microvm（-machine microvm），没 PCI 总线，
  QEMU 直接报 "No 'PCI' bus found" 挂给你看。
- e1000 要真实网络后端时用 -netdev user,id=e1 -device e1000,netdev=e1
  （只是验证设备挂载的话 peerless 的 -device e1000 就行）。
- PCI 枚举日志在 logger 起来之前就打了，qemu.log 里看不到——设备有没有
  被系统认到，得等驱动 probe 之后才有日志（我们目前没有驱动，所以只能
  验证"挂上后启动不炸"）。
