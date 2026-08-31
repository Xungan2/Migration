# cargo osdk test 必须显式 --kcmd-args console=ttyS0 earlycon

**结论**：裸 `cargo osdk test` 默认继承 make 流程烤进**构建缓存**的
`--kcmd-args`（console=hvc0/earlycon）。任何触发全量重建的变更（如
Cargo.toml 加依赖）会清空缓存参数 → ktest 内核静默：**测试照跑、
isa-debug-exit 退出码照准**，但输出型判据（按日志 grep 的）全体假 FAIL。
耗时 ~3h 破案（e2e-test-retry §14）。

**修法**：ktest 命令**显式**传
`--kcmd-args="console=ttyS0" --kcmd-args="earlycon"`
（对齐 `make ktest` 的 CONSOLE=ttyS0）。结果打在 **workspace 根
qemu-serial.log**（非 stdout），命令尾部 `cat` 该文件；成功特征用
`passed; 0 failed;`（`test result: ok` 的 ok 被 ANSI 包裹不可逐字匹配），
判定前须去 ANSI；判据正则优先字面量（`\beth0\b` 类会跨 ANSI 色码边界
失配）。

**适用面**：所有跑 `cargo osdk test` 的自动化（runner.json unit_test 节
形态）；e2e-test-retry 的 runner 已固化此形态，可直接参照。
