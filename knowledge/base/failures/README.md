# knowledge/base/failures/ — 失败签名（通用逻辑形态）

错误处理模块求解循环（porter/loop/errorloop.py）的检索面。条目 =
**一个可识别的失败形态 → 归责回路 → 建议动作**。

## 与 pitfalls 域的分工

- failures 条目是**处置入口**：薄——签名/判别/归责/建议动作四节，
  技术细节用指针引向 pitfalls 全文；
- pitfalls 条目是**技术细节**：坑的机理、命令形态、判读方法全文。

## 条目格式（四节必备）

```
# <症状标题>
**签名**：可机器/人识别的失败形态（与 OS/驱动无关的逻辑形态）
**判别**：怎么确认是这个而不是近亲
**归责**：infra | criteria | migration | attribution | platform
**建议动作**：fix-code | fix-runner | fix-criteria | rerun | park |
  escalate（+ 参数与证据要求）
```

## 归责回路口径（消费方 = 求解循环的动作词表）

- infra：环境/基础设施错 → rerun 或 fix-runner（幂等，不烧轮次价值）
- criteria：量尺错（判据/测试期望/文档断言）→ fix-criteria（**必须
  附源码 file:line 或日志原文对照证据**，阶段末审计）
- migration：迁移代码错 → fix-code（修后双信号复验）
- attribution：账挂错 → rehang 改挂真实消费者
- platform：目标 OS 平台缺口且禁改 → park 泊车登记，勿硬修

## 适用判据

本分区收**任意目标 OS 通用的逻辑形态**（矛盾结构、判别法）；
环境特定签名（docker 文案、osdk/QEMU 特征）挂各 lineage 分区
（如 knowledge/asterinas/failures/）。

INDEX.json 为机器索引（薄格式）；条目文件 = `<id>.md`。
