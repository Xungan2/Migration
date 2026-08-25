# 构建 & 启动随手记（内部笔记，未必最新，以能跑通为准）

- 一律在官方容器里干活：`asterinas/dev:0.18.1-20260805`。容器里 VDSO 之类
  环境变量都配好了，别在裸机上折腾工具链。
- 源码树挂到容器 /root/asterinas，然后 `make kernel`。顺利的话最后会打一行
  "Writing to 'stdio:...iso' completed successfully."（这行没了就是没编完）。
- 增量编译 1~2 分钟；如果改了 RUSTFLAGS 之类的编译参数，等着吧，全量
  15-25 分钟起步（缓存全作废）。所以全量场景超时务必给 20 分钟以上。
- 跑起来：`make run_kernel`。默认会进 guest 的交互 shell（出不来，会卡住
  脚本）——要自动退出就加 `AUTO_TEST=boot`，guest 跑完 /test/boot_hello.sh
  会自己 poweroff。
- 日志在源码树根的 `qemu.log`（不是 stdout！stdout 只有一堆 QEMU 自己的
  输出）。判启动成功 grep "Successfully booted"；挂了会有 panic 字样。
- LOG_LEVEL=info 一定要带，默认 error 级几乎什么都不打。
