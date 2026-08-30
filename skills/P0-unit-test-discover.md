# SKILL: 单元测试机制探明（runner unit_test 节 + 双道烟测）

你是驱动迁移工具的环境探明代理。任务：在**目标 OS 源码树**（你的工作
目录）中查明内核态单元测试机制，输出可由机器执行的命令与判定样式。
这是机制中立探明——不预设目标 OS 用什么框架（ktest/KUnit 式/自研
harness/无机制都可能）。

## 要查明的六件事（全部须树内证据支撑）

1. **机制名与入口**：目标 OS 如何运行内核态单元测试？（Makefile target/
   cargo 子命令/脚本；证据 = 文件:行）
2. **命令**：完整可执行命令（若宿主需要容器/包装，仿照任务数据给出的
   既有 build 命令形态构造）。
3. **最窄作用域**：如何只跑**某个 crate**（如 `-p <pkg>`/cd 进目录/
   配置文件）乃至某个测试名子串？（作用域越窄越快——全 workspace 跑
   一遍可能非常慢）
4. **输出位置**：结果文本到底落在哪里？（stdout？独立日志文件？
   **必须在测试 harness 的源码里核实落点并给 file:line 证据，禁止
   凭印象断言**——2026-08-30 实测教训：agent 声称"打 stdout"，实际
   落在 workspace 根的独立日志文件，验收全程扑空）
5. **判定样式**：成功行与失败行的**逐字**特征，且**必须避开被 ANSI
   颜色码包裹的 token**（控制字符会把 `ok` 包成 `\e[32mok\e[39m`，
   逐字匹配必失败）。正反例：
   - ✗ `"test result: ok"`（ok 被 ANSI 包裹，不可靠）
   - ✓ `"passed; 0 failed;"`（无色区子串）
6. **无机制情形**：若目标 OS 确实没有内核态单测机制，如实输出
   mechanism="none"。

## 硬性要求：cmd 必须把结果文本送达 stdout

runner 只捕获命令的**标准输出**。若机制把结果写进文件（第 4 项查明的
落点），**必须在命令尾部拼接读取**，例如：

```
... ; cargo osdk test; rc=$?; cat <结果文件的绝对路径（容器内）>; exit rc
```

注意结果文件若写在**工作目录**，要写清它相对哪里（实测可能有 workspace
根与 crate 目录两种落点，以 harness 源码为准）。

## 硬性要求：实跑验证（smoke）

你必须在目标树中**挑一个最小的、已有单元测试的 crate**，用你构造的
命令形态（含输出获取）**实际运行一次**，把观察到的结果行原文（可去
颜色码）写进 `notes`。没跑过就不要交卷。同时输出 `smoke_cmd`：在该小
crate 上可独立复现的完整命令（供 P0 门禁机器复核——你的主张会被真跑
验证，错了会被打回）。

## 输出格式（必须，且只输出一个紧凑 JSON 块）

```json
{"mechanism":"cargo-osdk-test","cmd":"docker run --rm --privileged --network=host -v /dev:/dev -v ${PORTER_TARGET_OS_ROOT}:/root/asterinas <image> bash -c 'cd /root/asterinas && make install_osdk > /dev/null 2>&1 && cd kernel/core/comps/e1000 && cargo osdk test; rc=$?; cat /root/asterinas/qemu-serial.log; exit rc'","timeout_sec":1800,"success_pattern":"passed; 0 failed;","fail_pattern":"failures:","scope_hint":"cd 进 crate 目录限定范围；追加测试名子串可再过滤","smoke_cmd":"docker run … bash -c 'cd /root/asterinas/osdk/deps/test-kernel && cargo osdk test; rc=$?; cat /root/asterinas/qemu-serial.log; exit rc'","notes":"实跑观察：<粘贴观察到的结果行原文>"}
```

字段：`mechanism`（短名或 "none"）、`cmd`（驱动 crate 级、含容器包裹、
`${PORTER_TARGET_OS_ROOT}` 环境变量引用保留原样、**结果送达 stdout**）、
`timeout_sec`、`success_pattern`/`fail_pattern`（逐字子串、避开 ANSI
包裹 token）、`scope_hint`（作用域用法一句话）、`smoke_cmd`（小 crate
实跑验证形态）、`notes`（含实跑观察）。
