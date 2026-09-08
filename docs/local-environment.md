# 本地迁移环境

本地路径为 `/home/llh/Migration`。Linux 是只读迁移输入，Asterinas 是
可写目标树；不编译或安装宿主 Linux 内核。

## 固定版本

| 项目 | 版本或提交 |
|---|---|
| Linux | v5.10.265，`2a3da1f4966798b0b48ce302944ad356b2c98b5d` |
| Asterinas | `aa458e9c3dee37a66f72bc68b7e58250e41ba41b` |
| 官方开发容器 | `asterinas/dev:0.18.1-20260901` |
| Rust | `nightly-2026-07-21`，由目标树指定，在容器内使用 |

两个源码提交与主仓库的 gitlink 一致；`.gitmodules` 提供官方来源。
恢复命令为 `git submodule update --init --depth 1`。

## 验证记录

2026-09-08：两棵源码树已恢复，工作树干净。Linux 的 `make kernelversion`
返回 `5.10.265`。porter 的 `p0 --t1-only` 已使用真实的 `drivers/md`、
Asterinas 树和 `examples/dm-zero-intent.md` 通过输入检查，识别 158 个
C 文件及 Kbuild。

实际验证全部通过：

| 检查 | 结果 | 证据（相对 `migrations/local-build/`） |
|---|---|---|
| 输入检查 | 158 个 C 文件、Kbuild、意图解析通过 | `input-check.log` |
| 局部编译 | `ring-buffer` 生成原生 `.rlib`，未生成 ISO | `module-build.log` |
| 完整构建 | `make kernel LOG_LEVEL=info` 返回 0，生成 ISO | `kernel-build.log` |
| 交互启动 | shell 返回本轮随机标记，`poweroff -f` 关机，返回 0 | `interaction-result.log`、`interactive-qemu.log` |
| 内核单测 | `ring-buffer` 的 3 个测试通过，0 失败，命令返回 0 | `unit-test.log`、`unit-qemu.log` |

机器可读结果为 `migrations/local-build/result.json`。输入检查工作区为
`migrations/local-env-smoke/`，它不是通过完整 P0 门禁的迁移工作区。

产物位于：

- `asterinas/target/x86_64-unknown-none/debug/libring_buffer.rlib`
- `asterinas/target/osdk/asterinas-osdk-bin.iso`

## 复现命令

本地已保留运行中的开发容器 `migration-asterinas-dev`，挂载
`asterinas/` 到 `/root/asterinas`，可访问 `/dev/kvm`，Cargo 并行度为 16。
Rust、OSDK 和 Nix 缓存保留在该容器内，QEMU 验证进程已退出。
以下命令从项目根目录执行：

```bash
# 局部编译
docker exec migration-asterinas-dev cargo build -p ring-buffer --target x86_64-unknown-none

# 完整镜像
docker exec migration-asterinas-dev make kernel LOG_LEVEL=info

# 本地交互验证脚本：等待 shell，发出随机标记命令，核对返回值后关机
python3 migrations/local-build/check-interaction.py

# 单模块内核态测试，显式指定串口参数
docker exec -w /root/asterinas/kernel/libs/ring-buffer -e CONSOLE=ttyS0 \
  migration-asterinas-dev cargo osdk test \
  --kcmd-args=console=ttyS0 --kcmd-args=earlycon \
  --qemu-args='-accel kvm' \
  --initramfs=/root/asterinas/test/initramfs/build/initramfs.cpio.gz
```

容器停止后可用 `docker start migration-asterinas-dev` 恢复。构建命令
应在容器内执行：initramfs 产物包含指向容器内 Nix store 的符号链接。

## 设计与环境验证的区别

P0 骨架前置、三个 loop 和默认关闭 P2a 的确认方案记录于
[p0-three-loops.md](p0-three-loops.md)，编排改造已实现；本页记录的是目标 OS 基线验证，完整 agent 发现流程需安装 opencode 后另行实跑。
本次环境验证针对上游 Asterinas；不表示新驱动骨架或驱动迁移已通过验收。
