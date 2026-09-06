# 构建 & 启动随手记（内部笔记，未必最新，以能跑通为准）

- 一律在官方容器里干活：`asterinas/dev:0.18.1-20260805`。容器里 VDSO 之类
  环境变量都配好了，别在裸机上折腾工具链（裸机 make 会直接报
  "the VDSO_LIBRARY_DIR environment variable must be given."）。
- 源码树挂到容器 /root/asterinas，然后 `make kernel`。判编完别盯着
  xorriso 那行 "Writing to 'stdio:...iso' completed successfully."——它只在
  全新构建/bundle 重打时出现，纯增量会复用缓存 bundle、直接跳过打 ISO，
  那行就没了（不是没编完！）。两态都必现的是 cargo 自己的
  "Finished `dev` profile" 行，认它。
- grub-rescue 会报一条 "cannot open directory .../share/locale" 的 warning，
  良性，别当失败信号（实测 rc=0 照样成功）。
- 增量编译 1~2 分钟；如果改了 RUSTFLAGS 之类的编译参数，等着吧，全量
  15-25 分钟起步（缓存全作废）。所以全量场景超时务必给 20 分钟以上。
- 跑起来：`make run_kernel`。默认会进 guest 的交互 shell（出不来，会卡住
  脚本）——要自动退出就加 `AUTO_TEST=boot`，guest 跑完 /test/boot_hello.sh
  会自己 poweroff，make 末尾还会自检 qemu.log 里有没有成功串，没有就
  rc=1（stdout 会打 "Boot test failed"）。
- 头一次跑 run_kernel 多花一分钟重打 ISO 是正常的（init-args 变了触发
  bundle 重建），不是故障。
- 日志在源码树根的 `qemu.log`（不是 stdout！stdout 只有一堆 QEMU 自己的
  输出）。判启动成功 grep "Successfully booted"（这行是用户态 echo 直出，
  没颜色码包裹）。挂了想看 panic——注意 panic 走的是另一条串口，落
  `qemu-serial.log`，qemu.log 里物理上不会出现 panic 字样；qemu.log 的
  失败形态就是成功串缺失。
- LOG_LEVEL=info 一定要带，默认 error 级几乎什么都不打。
