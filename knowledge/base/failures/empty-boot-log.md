# 空 boot 日志（empty-boot-log）

**签名**：boot 日志文件存在但为空，或仅 UEFI/固件行后无任何内核
输出（连 earlycon 都没有）。

**判别**：两种根因假设**机器不可判定**，须依次排查——
1. console 参数/重定向配错（环境类：重跑结果不变，判据 FAIL 是假信号）；
2. 内核在 console 初始化前早挂（真失败：这是有价值的崩溃信号）。
先核 runner boot 命令的 console/log_file 配置并与历史正常日志比对
开头；无配置疑点则按真失败走 `fix-code` 方向排查（earlycon 丢失/
panic 于极早期）。

**归责**：unknown（待判——infra 或 migration，排查后定）

**建议动作**：`fix-runner`（配置错）或 `fix-code`（真早挂）；
判定不了 `escalate` 附两种假设的排除过程。

**实证**：半成品 ISO 案的判读入口（环境特定形态见 lineage 分区
killed-make-halfbuilt-iso-boot）。
