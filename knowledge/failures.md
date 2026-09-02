# knowledge/failures.md — 失败签名活文档（§15 子系统 A）

> 用途：triage 规则库的人工晋升目标。升级报告自动附 `signature_candidates`
> （agent 建议，**未经人工晋升不得进本表**）；晋升 = 人工把候选改写为
> 下述条目形态并入册。条目 = 症状特征 + 判别方法 + 所属回路 + 实证案例
> + 首现排查代价（教训定价，防止"重新破案"）。
> 素材上游：知识库目录 pitfalls/（技术坑单页）、defects.json history。

## 签名库（六案例首批入册）

### SIG-01 docker 镜像/资源锁（infra）

- **症状**：build/boot 命令长时间挂起或立即失败，输出含
  `resource temporarily unavailable` / `database is locked` /
  容器名冲突。
- **判别**：rc≠0 + 上述特征文本；同命令此前刚跑过（events 可查）。
- **回路**：infra → 幂等重跑，不计 attempts。
- **实证**：e2e-test-retry 早期——镜像锁卡 25min。
- **首现代价**：25min（重跑即愈类，浪费在等待上）。

### SIG-02 ktest 静默 / console 缓存参数清空（infra·环境）

- **症状**：ktest rc==0（isa-debug-exit 准确）但 success_pattern 缺失、
  控制台无内核输出——输出型判据全体假 FAIL。
- **判别**：rc==0 ∧ 输出空/缺特征 ∧（触发过全量重建：Cargo.toml 变更/
  缓存清空）。对照 events：重建前同命令输出正常。
- **回路**：infra → 修 ut 命令显式 `--kcmd-args console=ttyS0 earlycon`
  后重跑（e2e §14 定谳修复，runner.json unit_test notes）。
- **实证**：e2e §14 ktest 静默。
- **首现代价**：**~3h**（其中 ≥2h 为"现场自毁+无轨迹"——本体系的
  直接动机；events+快照在场目标 <1h）。

### SIG-02b 杀 make 留半成品 ISO（infra·make 形态变体）

- **症状**：重烤后 UEFI 起但内核无输出（boot 假 FAIL）；此前有被
  timeout 杀掉的 run_kernel。
- **判别**：boot 日志止于 UFI/BdsDxe 行 + events 有 killed cmd 记录。
- **回路**：infra → 完整 `make kernel` 一次即愈。
- **实证**：P6 §16 沉淀③；pitfalls/killed-make-halfbuilt-iso.md（现居知识库目录）。
- **首现代价**：~30min（P6 会话）。

### SIG-03 测试期望错 / 判据正则错（criteria）

- **症状**：单测 FAIL 但被测函数与 Linux C 语义一致；或 L3 判据正则
  与日志实际形态失配（ANSI 色码边界、字面量差异）。
- **判别**：**逐分支对照 C 源码重推期望**（update_itr 模式：测试作者
  只推了一层分支）；证据 = C file:line + 推演。
- **回路**：criteria → 自动修正（强制源码行号证据，标 auto-fixed）。
  仅限工作区判定数据（criteria.json/判据正则）；需改目标树测试代码的
  → 检出后升级人工。
- **实证**：update_itr 两处期望错（§14 其他沉淀）。
- **首现代价**：~40min。

### SIG-04 计划/文档过期型假缺陷（criteria·文档错）

- **症状**：计划/遗留清单声称"缺 X 必须补"，但代码实测已演进到位。
- **判别**：**对照代码实测核计划**——grep 调用点拿 file:line，与文档
  断言对照（RESET-HW-STALE：os_probe.rs:765 与 e1000_reset 内均已调
  reset_hw）。
- **回路**：criteria → 闭账 stale + 附 file:line 证据；可附低成本
  补强判据（如 reset 基线断言）。
- **实证**：RESET-HW-STALE（defects.json，status=fixed closed-stale）。
- **首现代价**：~20min（但若不核代码直接"修复"会引入真 bug）。

### SIG-05 复合迁移 bug（migration）

- **症状**：多个独立缺陷叠加，单变量排查各自"部分有效"。
- **判别**：分解为独立链逐条验证（RX-PATH 三重叠加：configure_rx 未
  接线 + watchdog 无调用方 + QEMU 无 LBM——三条独立证据链在
  defects.json history）。
- **回路**：migration → attempts 带证据回炉，逐条清偿。
- **实证**：RX-PATH（defects.json 完整链）。
- **首现代价**：~2h（P6 会话，双工具破案）。

### SIG-06 平台缺口（platform → 泊车）

- **症状**：设备侧链路已证正常但消费侧恒零；根因在目标 OS/OSTD 禁改
  平台文件。
- **判别**：逐环节对照证明上游正常直到断点（INTX-DELIVERY：ICR=0x14
  ∧ GSI=17 正确 ∧ irq_count≡0 → 断点在平台 IRQ 交付层；ioapic.rs
  电平触发缺失，本树核实）；或栈侧存根属平台文件（eth0-iface-wiring）。
- **回路**：platform → 泊车 + platform_patches.json 登记（P7 上游素材）
  + 判据 park。
- **实证**：INTX-DELIVERY / eth0-iface-wiring（§16）。
- **首现代价**：~1h（其中实证链构造本身是产出——成为 P7 提案素材）。

## 候选区（agent 自动附上来的，待人工晋升）

（暂空——升级报告产生 `signature_candidates` 后由编排器追加到此处，
人工改写为正式条目后移入上表。）
