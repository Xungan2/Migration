# API 映射知识库规范（maps 域）

## 条目是什么

条目 = 一次迁移工作区产出的完整映射表**原样**（不改写、不摘要）：

- `<驱动名>@<目标OS名>.md` —— `mapping.md` 原样（人读渲染：域分节四列表
  + 换思路 + 接线清单）
- `<驱动名>@<目标OS名>.json` —— `mapping.json` 原样（机器消费侧：
  域过滤/结构化检索；条目 9 字段 + domain）

由 `p2-map` 产出映射时自动复制为草稿进入 `temp/maps/`；开发者在
P2 末（首个沉淀点）或循环中任意时点决定是否晋升沉淀。此后每轮
P3(M) 增量映射后草稿自动刷新（幂等覆盖）。

## 条目文件命名

`<驱动名>@<目标OS名>.md/.json`（如 `e1000@asterinas.md`）。
**命名带目标 OS**——映射结论离开目标 OS 无意义，这与
`splits/strategies`（P1 样例只涉及 Linux 侧、命名不带目标）相反。

## 目录结构

```
knowledge/maps/                 # 沉淀分区（已晋升，人工审阅过）
    INDEX.json                  #   目录：裸数组
    README.md                   #   本文件（不注入）
    <驱动>@<目标>.md/.json      #   条目（双文件）
temp/maps/                      # 草稿分区（p2-map 自动写入/刷新）
    INDEX.json
    <驱动>@<目标>.md/.json
```

## INDEX.json 格式（两分区相同，裸数组）

```json
[{
  "entry_stem": "e1000@asterinas",
  "entry_file": "e1000@asterinas.md",
  "entry_json": "e1000@asterinas.json",
  "driver_name": "e1000",
  "target_os": "asterinas",
  "target_os_commit": "<基线 commit（scope 校验用）>",
  "category": ["net"],
  "counts": {"direct": 54, "adapt": 311, "gap": 154, "not-migrated": 140},
  "redesigns": 7,
  "version": 1,
  "hits": 0,
  "updated": "..."
}]
```

## 晋升语义（与 p1-promote 的差异）

映射表是**活文档**（P3(M) 循环持续增量），故同名条目晋升 =
**版本更新替换**（version+1、保留 hits）；不做 P1 式快照并存
（否则 15 轮循环会堆积 15 个版本）。不同名 = 新增条目。

## 消费规则（铁律，与映射 agent SKILL 同款）

条目是"**经源码核实的主张**"，不是真理：

1. 消费前校验 scope：`target_os` 必须一致，`target_os_commit` 与
   当前目标树基线比较，漂移越大越须警惕
2. **核实后抄入**：把候选条目当提示，在当前目标树重新核实后才可
   写入新的映射表——evidence 的 file:line 在树演进后可能失效
3. **不跨目标复用未验证结论**：`e1000@asterinas` 的条目对
   `e1000@其他OS`、`其他驱动@asterinas` 只是线索，不是依据
4. 消费命中后递增对应条目的 `hits`（高频命中是升格/失效复核信号）
