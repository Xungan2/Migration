"""porter.loop — P3(M)/P4(M)/P5(M) 垂直循环（vertical-slice pipeline §3.2/§3.3/§10）。

模块：
    state              loop_state.json 状态机（断点重入；五相 p3→p4→p5）
    surface            P3(M) 使用面提取（纯脚本四分类）
    criteria           判据草案 schema 校验 + L0-L4 复核执行器
    probes             高风险探针（住骨架 + 双信号判定 + 有界改判）
                       + 共享 boot_and_log（P4 冒烟 / P5 L2 共用）
    p3 / p4 / p5 / run 阶段编排与循环推进（P4=fill+迁移+快速冒烟；
                       P5=模块级验收 L1/L2/L0/L3+累积回归+deferred）

退出码（与既有惯例一致）：0 成功 / 1 失败 / 2 前置缺失 / 3 需人工。
"""
