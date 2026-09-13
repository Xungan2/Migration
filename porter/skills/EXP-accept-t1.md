# SKILL: accept Tier1 事实提取（§1–§4）

从 exp-mono/ledger.json、runner.md 与 exp-mono/logs/ 提取已经真实执行的
模块编译、单测、镜像编译、自启动证据，为每节交付一对文件：
`<N>-<slug>.json` 与同名 `.check.py`（N=1..4）。只引用日志中可核实的
命令和结果；没有证据就写 `status: blocked`，不要猜测或运行新实验。

JSON 至少包含 `status`、`commands`（按执行顺序，可为空）、`expect`、
`evidence`；消费脚本接受 JSON 路径，读取 `PORTER_EVIDENCE_DIR` 下已归档
日志并以 exit 0/非零表示通过/失败，stdout 输出证据。脚本应可重复运行，
不得修改源码树。每节完成后在最终回复输出：
`{"status":"done","deliverable":"<acceptance绝对路径>","notes":"..."}`。
