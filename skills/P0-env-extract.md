# SKILL: P0 三个验证 loop

骨架已经施工。本次只负责任务数据指定的一个 loop：编译 → 启动/设备注入 →
单测。产出可执行配置和调用手册，编排器实际执行验收，失败证据回到本会话修正。

## 工作步骤

1. 按用户提示、开发资料、目标树的顺序查明命令。先读任务数据中的骨架
   manifest/recipe，确认 driver_home、构建接线、认领日志和最小测试。
   资料与实测冲突时以实测为准，在「不确定项」记录差异。
2. 查询文件与源码可直接执行；编译、启动和测试命令通过下面的测试请求
   交给编排器执行。前序已收敛命令按只读材料参考。
3. 按当前 loop 的契约写 JSON 与 Markdown。调用手册须包含任务数据下发的
   全部标题，逐项写明依据、备选、失败原因、修正和不确定项，附 file:line。
4. 收到失败指针先读完整证据，修正命令、参数或骨架。允许修正骨架及其接线，
   保持零驱动业务功能；新增源码/接线文件同步补入 manifest 的 created/source_paths，
   更新测试与认领契约。源码改变会使前序验收失效，编排器先从编译重新验证。
   重写片段须有实际变化，避免原样重放。只有编排器终验 PASS 才完成。

## 文件协议

探索命令时，在指定请求路径写裸 JSON，然后消息只回「已写入 <路径>」：

```json
{"cmd": "<完整命令>", "timeout_sec": 600}
```

一次一个请求，超时可省略。完整执行结果由编排器落文件，下一段消息给路径。
完成时写指定的 `<cap>.json` 与 `<cap>.md`。build、unit_test 写单节对象；
boot 写同时包含 `boot` 和 `inject_device` 的对象。所有路径、命令与日志
特征来自当前目标树；树根在 shell 中使用 `${PORTER_TARGET_OS_ROOT}`。
环境、工具链、容器及参数都在完整命令和手册中定义。

资源耗尽时，按消息指定路径写「已完成 / 未完成 / 给开发者的问题」，
问题须附尝试与证据。人工回答会作为续跑的最高优先级输入。

## 编译 loop

按顺序验收：编译方法完整 → 骨架进入编译范围 → 局部模块产物 → 完整镜像。

```json
{
  "cmd": "<完整镜像构建命令>",
  "module_cmd": "<仅构建骨架模块与依赖的命令>",
  "scope_cmd": "<读取真实编译器依赖/构建元数据，逐行输出编译的源码路径>",
  "module_artifacts": ["<模块产物路径>"],
  "image_artifacts": ["<完整镜像产物路径>"],
  "timeout_full_sec": 3600,
  "timeout_inc_sec": 600,
  "success_pattern": null
}
```

产物路径相对目标树或宿主绝对路径；列全镜像产物。局部命令不得创建或更新
完整镜像，原有镜像允许保留。模块产物跟随 OS 原生格式（.so/.a/.rlib 等）。
`scope_cmd` 读取本次局部构建的真实编译元数据，输出 manifest.created 中
骨架源码的路径，每行一个，相对目标树或宿主绝对路径；不以目录扫描、
构建清单成员声明或 echo 人工列举替代编译范围证据。跨容器路径须转为宿主
或相对路径。局部构建失败时不会运行完整构建。

## 启动/设备注入 loop

同一 session 同时定义启动方式和注入方式：

```json
{
  "boot": {
    "cmd": "<启动命令，guest 串口输入连接进程 stdin>",
    "timeout_sec": 900,
    "log_file": "<guest 日志路径或 null>",
    "log_is_stdout": false,
    "success_pattern": "<内核启动成功特征>",
    "panic_pattern": "<内核失败特征>",
    "interaction": {
      "prompt_pattern": "<guest shell 就绪的正则>",
      "shutdown_cmd": "<guest shell 中关机的命令>"
    }
  },
  "inject_device": {
    "mechanism": "env",
    "env": {"<变量名>": "<含 <DEVICE_ARGS> 的值>"},
    "cmd_suffix": null,
    "example_args": {"<类别>": "<设备参数>"},
    "driver_success_pattern": "<骨架真实识别/认领该设备才打印的特征>",
    "driver_fail_pattern": null
  }
}
```

- guest shell 须支持 `printf`。编排器等待提示符后，从 stdin 发送随机标记
  命令，检查本轮独立响应行，再发送关机命令；进程须正常退出。不能把
  仅启动完成、宿主输出或命令回显当作交互成功。
- 命令也供后续非交互消费者使用，需提供足够长的自动关机兜底；P0 主动
  交互关机应先于兜底。容器命令须保持 stdin 通路，日志必须来自 guest。
- 注入机制支持 env 或 cmd；后者使用 `cmd_suffix`，载体须含 `<DEVICE_ARGS>`。
  从源码查明启动命令确实消费载体。example_args 覆盖目标类别；网络类
  同时提供 `net-user`（带用户态网络后端），供后续端到端测试。
- 骨架全部 acceptance_patterns 须命中；driver_success_pattern 必填，绑定
  设备认领，不能只用注册成功行。编排器在同一 loop 内再跑无注入的基线，
  认领特征必须在注入轮出现、基线轮不出现。纯软件框架通过创建/注入其
  逻辑设备或实例验证认领，不要求驱动业务 I/O。

## 单测 loop

使用目标 OS 原生机制，验证骨架所属模块的最小测试：

```json
{
  "mechanism": "<原生测试机制>",
  "cmd": "<后续模块单测使用的完整命令形态>",
  "smoke_cmd": "<运行骨架最小测试的完整命令>",
  "scope": "<manifest.driver_home 的原值>",
  "test_names": ["<骨架源码中定义且测试输出会列出的测试名>"],
  "timeout_sec": 1800,
  "success_pattern": "<测试全部通过特征>",
  "fail_pattern": "<失败特征或 null>"
}
```

结果送到 stdout；命令只运行该模块的最小测试，输出必须列出 test_names。
测试名须在骨架源码中存在。允许专用测试内核，无须系统 shell；ELF 格式
不意味着可用 QEMU 用户态。`mechanism=none` 无法满足 P0 单测验收；查明
缺失条件后依照资源耗尽协议报告。门禁消费此 loop 的结果，不重复执行。
