<!-- hint-v1-spinor -->
# 单元测试提示（spi-nor 场景，OS 级结论直接用）

- 机制：**ktest**——树内内核态单测框架（`#[ktest]` 标记），OSDK 生成
  测试内核在 QEMU 里跑，结果打串口 → 命令 stdout。
- 全量命令（CI 同形）：

```
docker run --rm --privileged --network=host -v /dev:/dev -v ${PORTER_TARGET_OS_ROOT}:/root/asterinas asterinas/dev:0.18.1-20260805 bash -c 'cd /root/asterinas && make ktest NETDEV=tap'
```

- driver_scope（单 crate 范围，exp-mono 模块级单测用这条；
  `{PORTER_DRIVER_HOME}` = `kernel/core/comps/spi-nor`；dm-zero 轮
  同形态实测 ~145-160s rc=0）：

```
docker run --rm --privileged --network=host -v /dev:/dev -v {PORTER_TARGET_OS_ROOT}:/root/asterinas asterinas/dev:0.18.1-20260805 bash -c 'cd /root/asterinas && make install_osdk && make initramfs && cd {PORTER_DRIVER_HOME} && export CONSOLE=ttyS0 NETDEV=tap && cargo osdk test --boot-method=grub-rescue-iso --grub-boot-protocol=multiboot2 --qemu-args="-accel kvm" --initramfs=/root/asterinas/test/initramfs/build/initramfs.cpio.gz'
```

- 成败特征：成功 `[ktest runner] All crates tested.`；失败 `failures:`；
  权威判定 = rc==0（逐 crate 聚合）。
- 避坑：
  - 必须经 `cargo osdk test`（cargo→osdk 两级 CLI），直调 cargo-osdk
    二进制会报 unrecognized subcommand。
  - `--initramfs` 指向的 initramfs.cpio.gz 是指向容器私有 /nix/store 的
    符号链接，fresh 容器里悬空——driver_scope 命令前置 `make initramfs`
    重建。
  - 别选 `test result: ok.` 当特征——`ok` 被 ANSI 颜色码包裹，逐字匹配
    必 MISS。
  - `make ktest` 自带 CONSOLE=ttyS0；手跑 cargo osdk test 必须显式
    export，否则结果不进 stdout。
  - NETDEV=tap 规避 user 模式随机 hostfwd 端口撞车。
  - 全量超时 3600s（default-members 逐 crate 起 QEMU）；**spi-nor 新增
    crate 要进根 Cargo.toml default-members 才被 make ktest 覆盖**，
    进 crate 目录跑 cargo osdk test 不受此限。
