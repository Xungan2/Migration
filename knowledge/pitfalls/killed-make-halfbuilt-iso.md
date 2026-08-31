# 杀 make run_kernel 留半成品——后续 boot 内核零输出

**结论**：`timeout`/手动 kill 掉 `make run_kernel`（其内部含 build）会
留下半成品状态（OSDK 构建缓存指纹 / ISO 残缺）。后续 boot 的表象：
UEFI 正常起（"WARNING: no console will be available to OS" 是**正常噪音**
——正常 boot 也有此行），随后**无任何内核输出**（连 earlycon 都没有），
最终超时。

**判别**：与正常日志比对开头——正常流在 UEFI 行后 ~0.6s 即出现
`[Info] Component initialize complete` 起的组件日志；零内核行 = 半成品
而非代码 bug。

**修法**：完整跑一次 `make kernel`（不设短 timeout、不中断）重烤即愈。
不要急着怀疑自己的驱动改动。

**预防**：跑 QEMU 的命令 timeout 给足（boot 9s + 自测类秒级窗口 +
关机余量；执行模式给 120s+）；构建类命令严格按增量/全量分级
（AGENTS.md 超时纪律——超时杀构建的代价远超多等）。

**近亲**：`cargo osdk test` 缓存参数被全量重建清空导致 ktest 静默
（见 ktest-console-args 条目）——同为"杀/重建弄丢 OSDK 烤进去的参数"
家族。
