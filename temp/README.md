# temp/ —— 未沉淀知识暂存区

存放一切**未沉淀**的知识草稿。未经人工审核晋升的内容一律放这里，
不进 `knowledge/`。

## 当前内容类型

- **拆分策略样例草稿**：工作区 `strategy.md` 的原样副本，
  路径 `temp/splits/strategies/<驱动名>.md`，由 `run_strategy` 在产出
  strategy.md 时**自动写入**（+ `INDEX.json` 条目）。与沉淀分区完全
  一致（同名+同文件集）不写；同名但构成不同则改名（`__2`、`__3`…）
  保留。
  草稿分区**随 strategy prompt 注入**（带"草稿，未经人审"标注），
  与已沉淀样例冲突时以已沉淀为准。
  晋升规则见 `knowledge/splits/strategies/README.md`。

## 约定

- 目录结构镜像 `knowledge/`（如 `temp/splits/strategies/` 对应
  `knowledge/splits/strategies/`），晋升时路径直接对应。
- 晋升（`p1-promote`）后文件与索引条目搬入 `knowledge/` 对应位置，
  并从本目录删除，不双存。
- 长期滞留未晋升的草稿应清理或推动审阅，避免暂存区变成垃圾场。
