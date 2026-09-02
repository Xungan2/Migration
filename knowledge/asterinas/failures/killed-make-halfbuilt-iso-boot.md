# 杀 make 留半成品 ISO（killed-make-halfbuilt-iso-boot）

**签名**：empty-boot-log 形态 ∧ 日志止于 UEFI/BdsDxe 行（"UEFI
QEMU" 后无内核输出）∧ events 有被 timeout 杀掉的 make run_kernel
（其内部含 build）。

**判别**：正常流 UEFI 行后 ~0.6s 即出现 `[Info] Component
initialize complete` 起的组件日志；零内核行 = 半成品状态
（OSDK 构建缓存指纹/ISO 残缺）而非代码 bug。"WARNING: no console
will be available to OS" 是正常噪音，正常 boot 也有。

**归责**：infra（构建状态）

**建议动作**：`rerun` ——完整 `make kernel` 一次重烤即愈
（不设短 timeout、不中断）。**勿急着怀疑驱动改动**。

**实证与详情**：首现代价 ~30min。预防纪律与近亲家族见
pitfalls/killed-make-halfbuilt-iso。
