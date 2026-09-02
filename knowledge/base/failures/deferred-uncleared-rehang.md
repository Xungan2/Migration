# deferred 清偿失败（deferred-uncleared-rehang）

**签名**：deferred 判据清偿失败——其消费者模块全部 done，判据
仍 FAIL。

**判别**：两种可能——判据挂错了模块（真消费者在别处：日志里
谁实际产出该特征？）或跨模块集成真坏（消费者确实跑了但特征
被集成问题吃掉）。默认嫌疑前者。

**归责**：attribution（默认）/ migration（核实后）

**建议动作**：核实真实消费者（日志特征的实际产出者）→
`rehang` 改挂；核实不到或确实集成坏 → `fix-code` / `escalate`。

**实证**：e2e hw-link.link-ev 形态（消费者判定口径错挂）。
