# e1000 RX 计数复合形态（rx-composite-signature）

**签名**：stats 类日志 rx 计数恒 0 而 tx 非零
（`rx_(bytes|packets)=0 ∧ tx_bytes≠0`）。

**判别**：TX 通而 RX 不通——按 composite-defect-decompose 分解
（配置接线 / 调用方 / 模拟器行为三条独立链逐条验证）。本环境两个
已知常量：QEMU 不实现 RCTL.LBM_MAC（回环判定不可用）；QEMU
autoneg 500ms 虚拟定时器内 LU=0 弃一切入站帧（首笔 ARP 被吃）。

**归责**：migration（复合）或 platform（断点落在平台层时）

**建议动作**：`fix-code`（分解清偿：RCTL.EN 接线、watchdog
调用方、SLIRP 真流量路径）；断点在平台禁改文件（如 ioapic
电平触发）→ `park`（platform-gap-pattern）。

**实证与详情**：RX-PATH（~2h 双工具破案）。机理见
pitfalls/qemu-e1000-no-rctl-lbm 与
pitfalls/qemu-autoneg-500ms-rx-window。
