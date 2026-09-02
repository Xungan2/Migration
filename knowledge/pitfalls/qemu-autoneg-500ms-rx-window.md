# QEMU autoneg 500ms 虚拟定时器内 LU=0 弃一切入站帧

**结论**：QEMU e1000 的链路建立走 500ms 虚拟时钟定时器
（`set_phy_ctrl` 写 BMCR 带 ANRESTART → `e1000x_restart_autoneg` →
`timer_mod(+500ms)` → `e1000_autoneg_done` 才置 `STATUS.LU` +
`BMSR.LINK_ST`）。窗口内 `e1000x_hw_rx_enabled`（收包第一道门）判
LU=0 → **一切入站帧被弃**（连 can_receive 都不过）。

**踩法一（boot 探针）**：复位/初始化后立即做 RX 探测——LBM 或真流量
均落在 500ms 窗内 → 假失败（e2e-test-retry boot 期探针 rx_bytes=0 的
成因之一）。
**踩法二（首笔 ARP）**：open → autoneg 重启 → 立即发 ARP——SLIRP 的
应答若在 LU 置位前到达即被弃；同窗重发即通（实测第 2 笔起必通）。

**对策**：
1. 判定等待条件用 **BMSR/STATUS.LU 读回**而非固定 sleep（QEMU 虚拟
   时钟与 host 时间不同步）；
2. 协议级正解 = **有界重试**（ARP ≤5 次 × 400ms 间隔——Linux 栈同理）；
   不要在驱动里写一次性发送 + 长轮询的单笔语义。

**证据**：`refs/qemu-10.2.1-hw-net-e1000x_common.c`
（restart_autoneg 的 timer_mod 500ms；rx_ready 的 LU 门）+ P6 ARP
重试爆发实验（首笔丢、重试 4/4 通）。
