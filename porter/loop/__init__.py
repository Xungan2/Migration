"""porter.loop — P3(M)/P4(M) 垂直循环（vertical-slice pipeline §3.2/§3.3/§10）。

模块：
    state              loop_state.json 状态机（断点重入）
    surface            P3(M) 使用面提取（纯脚本四分类）
    knowledge_consume  knowledge/maps 消费侧（INDEX 路由 + 域过滤 + hits）
    criteria           判据草案 schema 校验 + L0-L4 复核执行器
    probes             高风险探针（住骨架 + 双信号判定 + 有界改判）
    p3 / p4 / run      阶段编排与循环推进

退出码（与既有惯例一致）：0 成功 / 1 失败 / 2 前置缺失 / 3 需人工。
"""
