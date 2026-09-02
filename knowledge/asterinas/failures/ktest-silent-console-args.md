# ktest 静默——console 缓存参数被清空（ktest-silent-console-args）

**签名**：silent-success-contradiction 形态（rc==0 ∧ 特征缺失 ∧
输出型判据全体假 FAIL）∧ 命令为 `cargo osdk test` ∧ 近期触发过
全量重建（Cargo.toml 变更/构建缓存清空）。

**判别**：裸 `cargo osdk test` 继承 make 流程烤进**构建缓存**的
`--kcmd-args`；全量重建清空缓存 → ktest 内核静默。对照 events：
重建前同命令输出正常。

**归责**：infra（环境）

**建议动作**：`fix-runner` ——ut 命令显式补
`--kcmd-args="console=ttyS0" --kcmd-args="earlycon"`。

**实证与详情**：首现代价 ~3h（e2e §14 定谳）。完整机理/输出
路径/成功特征口径见 pitfalls/ktest-console-args。
