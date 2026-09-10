将 Linux device-mapper（DM）迁移为 Asterinas 原生安全 Rust 实现。
迁移范围包含 DM 核心、各 targets 及必需依赖。
drivers/md 中无关的 MD RAID、bcache 不作为迁移主体。
请明确列出范围、依赖和无法支持的功能，供人工审阅。
DM 是软件块设备框架，验证应基于目标 OS 支持的底层块设备。
