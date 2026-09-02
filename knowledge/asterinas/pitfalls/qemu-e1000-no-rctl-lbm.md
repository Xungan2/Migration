# QEMU e1000 不实现 RCTL.LBM_MAC——MAC 回环判定机制性必败

**结论**：QEMU（含 10.2.1，`hw/net/e1000.c`）对 `RCTL.LBM_MAC`（MAC 内部
回环位）**没有任何处理**——TX 恒经 `qemu_send_packet` 直发 netdev；唯一
的回环分支在 `e1000_send_packet`：

```c
if (s->phy_reg[MII_BMCR] & MII_BMCR_LOOPBACK) {
    qemu_receive_packet(nc, buf, size);   // PHY 回环，不经 netdev
} else {
    qemu_send_packet(nc, buf, size);      // 直发
}
```

即想回环只有 **PHY BMCR bit14**（`MII_CR_LOOPBACK`）一条路（且该路径不经
netdev，filter-dump 抓不到）。设置 `RCTL.LBM_MAC` 后自发自收 = TX 计数
增长（xmit_seg 无条件回写 DD + GPTC）而 RX 恒零——表象恰似"RX 通路坏了"，
实为模拟器语义缺失。

**正径**：数据通路判定用**真实流量**——SLIRP 后端
（`-netdev user,id=e1 -device e1000,netdev=e1`）+ ARP 请求/应答往返
（who-has 10.0.2.2 tell 10.0.2.15，SLIRP 网关必应答，见
`e1000x`/libslirp `arp_input` 的 `goto arp_ok` 分支）。

**实证**（e2e-test-retry P6，defects.json RX-PATH）：LBM 探针双失败
（os_rx_irq::probe_receive / os_stats::probe_loopback_traffic，
rx_bytes=0）而独立 TX 探针成功；换 SLIRP 真流量后 ARP 往返
rx_packets≥1、GPRC/GPTC 与软计数一致。QEMU 源码副本：
`driver_migration_tool/refs/qemu-10.2.1-hw-net-e1000.c`。

**连带坑**：QEMU q35 双网卡环境默认 virtio-net 占 `52:54:00:12:34:56`，
显式 e1000 得顺序号 `:57`——探针若硬编码 `:56` 做 MAC 过滤断言会假失败。
