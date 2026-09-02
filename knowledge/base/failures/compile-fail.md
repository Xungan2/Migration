# 编译失败（compile-fail）

**签名**：编译类判据失败——detail 含 `rc≠0` 或 `pattern=MISS`；
build 输出含 error 行（`error[E...]` / `error:` 等）。

**判别**：编译错即迁移代码问题——与量尺、环境、归属均无关。
注意 build 输出摘录可能截断（evidence 只带尾部），定位须读
完整构建日志（相位 logs / 快照）。

**归责**：migration

**建议动作**：`fix-code` ——按完整 build 日志的错误行修目标树
驱动 crate；修后重跑 build+boot 双信号复验。多错误并存时先按
`composite-defect-decompose` 判断是否复合。

**实证**：P4 切片迁移常态形态；e2e-test-retry 多次。
