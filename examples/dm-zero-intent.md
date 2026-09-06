# dm-zero 的 Asterinas 迁移意图

本次只迁移 device-mapper 的 zero target，以及让它在 Asterinas 中完成
注册、实例化、I/O 分发、失败回收和销毁所需的核心能力。

保留 Linux 5.10.265 的 dm-zero.c 所定义的构造参数校验、读回全零、写入
丢弃及其他操作的原有处理语义。以 Asterinas 原生块设备与初始化机制
实现，可通过单测和块设备读写验证；Linux cmdline 字符串不是兼容性要求。

本轮排除其他 DM targets、用户态 device-mapper ioctl 管理协议、udev
通知、request-based 路径，以及 zero 不需要的 DAX、PR、zoned 能力。
保留路径若引用这些设施，须给出语义完整的替代或移除调用路径方案。
不能通过空桩或始终成功返回来掩盖必要的初始化、错误处理和资源释放。

Linux 公共头用于理解规格；文件范围在 drivers/md 内按实际需要推导。
参考 dm-zero-manual 的消费者闭包思路；重新核实当前源码、功能范围和
Asterinas 目标，不固定为参考方案的 16 文件或 5 模块，也不沿用 UDK 假设。
