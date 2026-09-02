# 静默成功矛盾（silent-success-contradiction）

**签名**：命令 rc==0（退出码准确、测试真跑完）但 success_pattern
缺失——输出型判据**全体**假 FAIL。

**判别**：矛盾点在"成功退出 vs 输出无特征"——代码没挂，是**输出
通道哑了**。对照 events 里同命令的历史输出（此前正常 → 输出配置
依赖了被清除的隐式状态）。若仅单条判据失败而非全体，不属本签名。

**归责**：infra（环境/输出配置）

**建议动作**：`fix-runner` ——检查 runner 命令的 console 参数与
输出重定向是否依赖隐式缓存/环境配置，显式化之；不确定时先
`rerun` 一次排除瞬时抖动。

**实证**：ktest 静默案（首现代价 ~3h，其中 ≥2h 为无轨迹重复推理）。
目标 OS 特定修法见 lineage 分区（如 asterinas 的
ktest-silent-console-args）。
