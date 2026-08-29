"""bootstrap — P2 专属：引导映射（2a）+ 全局骨架（2b）。

设计依据：~/.local/share/opencode/plans/vertical-slice-pipeline.md §3.1/§10。
- extract_spine.py  主轴 API 提取（纯脚本）：MVP 模块 refs−defs 外部符号
                     + Linux 内核头文件域分组 → P2/reports/spine_api.json
- mapping.py        2a 编排：按域分批 agent 映射 → 机器校验（schema +
                     evidence 路径存在性）→ 增量合并 mapping.json → 渲染
                     mapping.md；换思路/接线单独一轮 agent 调用
- skeleton.py       2b 生成器：目标 OS（Asterinas）专属模板，生成 crate
                     与全部接线点改动（幂等，写入清单落 reports）
- run.py            P2 入口：2a → 2b → 验收（runner build + boot 双信号
                     + 骨架组件日志特征）
"""
