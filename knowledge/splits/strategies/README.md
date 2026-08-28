# 拆分策略样例库规范

## 样例是什么

样例 = 一次迁移工作区产出的 `strategy.md` **原样**（不改写、不摘要）。
由 `run_strategy` 在产出 strategy.md 时自动复制为草稿进入
`temp/splits/strategies/`；开发者在 P1 整体完成后决定是否晋升沉淀。

## 条目文件命名

`<驱动名>.md`（小写，如 `e1000.md`）。P1 只处理 Linux 侧源码，
样例与目标 OS 无关，命名不带目标 OS。

## 目录结构

```
knowledge/splits/strategies/     # 沉淀分区（已晋升，人工审阅过）
    INDEX.json                   #   目录：裸数组，随 strategy prompt 注入
    README.md                    #   本文件（不注入）
    <驱动名>.md                  #   样例
temp/splits/strategies/          # 草稿分区（run_strategy 自动写入）
    INDEX.json
    <驱动名>.md
```

**机器判定**：分区内除 `README.md` 外的 `*.md` 即样例条目。

## INDEX.json 格式（两分区相同，裸数组）

```json
[
  {
    "entry_file": "e1000.md",
    "driver_name": "e1000",
    "linux_dir": "/abs/path/to/drivers/net/ethernet/intel/e1000",
    "linux_files": ["e1000_main.c", "e1000_hw.c", "e1000.h",
                     "e1000_ethtool.c", "e1000_param.c"],
    "hits": 0
  }
]
```

- `linux_dir` 取 `project.json` 原值（绝对路径，仅展示/溯源，
  **不参与匹配**——不同机器路径不同）
- `linux_files` = 驱动目录下 `*.c/*.h` 文件名排序
- `hits` = 被后续 strategy 对照采用的次数，从 0 起；人工审阅新策略的
  "样例库对照"声明时更新

## 价值判定判据（run_strategy 自动执行，写入工作区报告）

新草稿与 **沉淀分区** INDEX 条目逐条对比：

| 判定 | 条件 | 处理 |
|---|---|---|
| 完全一致 | `driver_name` 相同 且 `linux_files` 集合相同 | 不写 temp，报告记"已存在" |
| 相关但非完全一致 | `driver_name` 相同，文件集不同 | 写 temp，报告记"有价值（构成不同）" |
| 无相关 | 沉淀分区无同名驱动 | 写 temp，报告记"有价值（全新）" |

不比对文件内容（同名同文件清单但内容不同的情形会被判完全一致；
现阶段接受该局限）。

## 同名碰撞处理（改名保留不同构成）

- **真重复**（驱动名相同 且 `linux_files` 集合相同）→ 跳过（草稿入
  temp）/ 拒绝（晋升入 knowledge）
- **同名但构成不同** → 自动改名保留：`<驱动名>.md` 已占用则
  `<驱动名>__2.md`、`__3.md`……（晋升时若沉淀分区裸名空闲则归位为裸名）
- 晋升歧义：同名条目多于一个时，`p1-promote --driver` 需给**条目
  文件名**而非驱动名

## 沉淀流程

1. `run_strategy` 产出 strategy.md → 按上表自动草稿入
   `temp/splits/strategies/`（+INDEX 条目；真重复跳过，同名不同构成
   改名保留）
2. 工作区报告 `reports/P1-knowledge.md` 给出价值判定
3. **P1 整体完成后**，开发者审阅草稿与报告，决定是否沉淀
4. 决定沉淀 → 执行：

   ```
   python3 porter/main.py p1-promote --driver <驱动名或条目文件名>
   ```

   命令将文件与 INDEX 条目从 temp 分区搬入沉淀分区（不双存；
   沉淀分区已有同名同文件集时拒绝，同名不同构成时改名并入）

5. 样例被后续策略命中借鉴后，人审时更新沉淀分区对应条目 `hits`
