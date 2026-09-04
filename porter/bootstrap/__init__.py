"""bootstrap — P2 专属：引导映射（2a）+ 框架引导（2b）+ 探针预生成（2c）。

设计依据：~/.local/share/opencode/plans/vertical-slice-pipeline.md §3.1/§10
+ 2026-09-05 P2 重构定案（发现式骨架替换硬编码 skeleton；类型 B wiring
退役，接线知识由 2b 施工单+三信号验证承载）。
- extract_spine.py  主轴 API 提取（纯脚本）：MVP 模块 refs−defs 外部符号
                     + Linux 内核头文件域分组 → P2/reports/spine_api.json
- mapping.py        2a 编排：按域分批 agent 映射 → 机器校验（schema +
                     evidence 路径存在性）→ 增量合并 mapping.json → 渲染
                     mapping.md；换思路裁定单独一轮 agent 调用
- scaffold.py       2b 框架引导：agent 发现施工单（recipe：骨架代码 +
                     幂等接线编辑 + 验收特征 + 探针底座契约）→ 通用引擎
                     落地 → 三信号验证（build / boot_with_device + 特征 /
                     单测 smoke）→ 失败带证据回炉（≤3 轮）→ 人工关口；
                     成功后 manifest + mapping 批注 + kb 候选 + vcs commit
- recipe_apply.py   2b 施工引擎：OS/语言中立的 create/insert/replace +
                     marker 幂等 + journal 回滚 + 文件缺失防崩
- pregen.py         2c 探针预生成（生成段 agent 分批；同步+判定走共享
                     探针生命周期，宿舍路径来自 scaffold manifest）
- run.py            P2 入口编排：2a → 2b → 2c → vcs + CP2 映射审
"""
