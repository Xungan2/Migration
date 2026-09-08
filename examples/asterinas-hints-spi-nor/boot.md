<!-- hint-v1-spinor -->
# 启动提示（spi-nor 场景，OS 级结论直接用）

- 命令骨架（非交互、自行退出）：

```
docker run --rm --privileged --network=host -v /dev:/dev -v ${PORTER_TARGET_OS_ROOT}:/root/asterinas asterinas/dev:0.18.1-20260805 bash -c 'cd /root/asterinas && make run_kernel AUTO_TEST=boot LOG_LEVEL=info'
```

- `AUTO_TEST=boot` 是自退出机制（guest 跑 /test/boot_hello.sh 后
  poweroff），make 末尾还会自检日志，失败时 stdout 打 "Boot test failed"
  且 rc=1。
- 超时 600s 够（dm-zero 轮实测 108.1s；头一次会多花约 1 分钟重打 ISO，
  属预期）。
- 判定日志 = 目标树根的 `qemu.log`（log_is_stdout=false，stdout 只有
  QEMU 噪声）。
- 成功特征 `Successfully booted.`（用户态 echo 直出，无 ANSI 包裹）。
- 失败形态 = 成功串缺失 + rc≠0。注意：panic 字样物理上落
  `qemu-serial.log`（默认 console=hvc0 拓扑把串口分流了），qemu.log 里
  不会有——排 panic 去看那份。
- **组件/探针日志可见性（spi-nor exp-mono 启动 gate 依赖）**：
  `LOG_LEVEL=info` 下 `ostd::info!`（驱动骨架、`PROBE_* PASS` 探针行）
  经 AsterLogger → console(hvc0) → mux → qemu.log 可 grep（e1000 /
  aster-md / dm-zero 轮均实证；注意组件初始化批次须在 AsterLogger
  注入后——靠依赖抬优先级或 Kthread 阶段钩子，aster-md r1 教训）。
- 避坑：
  - 内核日志行的时间戳/级别 token 被 ANSI 包裹，别选 INFO/WARN 之类
    token 当特征；探针锚取无色区连续子串（如 `PROBE_xxx PASS`）。
  - NETDEV=user 每次随机 hostfwd 端口，偶发撞宿主机端口（现象：qemu.log
    空且 make 报 Error）——瞬时故障，重跑即愈。
