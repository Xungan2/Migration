"""porter/main.py — 工具总入口。

用法：

    python3 porter/main.py p0 \
        --linux-driver /path/to/linux/drivers/net/ethernet/intel/e1000 \
        --target-os    /path/to/asterinas \
        --materials  /path/to/notes.md  --materials /path/to/docs-dir \
        --name my-first-port \
        [--category net] [--workroot ./migrations]

--materials 可多次（开发者提供的自由资料：文档/笔记/配置，形态不限；
可完全省略——agent 将仅凭目标 OS 源码树提取）。

T3 为多轮 agent×探测循环（≤3 轮自动修正）；仍未完成则生成
reports/human_questions.md 并以退出码 3 暂停——把答案写入工作区
answers.md 后重跑即进入 R4 答案整合轮。

子命令 = 阶段（p0 现已实现；p1..p6 随各阶段实现加入）。

目录约定：
    porter/common/   跨阶段共用脚本（agent 调用抽象等）
    porter/env/      P0 专属（目标环境接入与验证）
    （未来阶段各自建子目录，届时命名）

工作区产物：project.json=项目身份（幂等真值源）；runner.json=机器可执行
命令；reports/=步骤结论（门禁数据源/人工升级历史回放/人读报告）；logs/
=agent 与命令的原始输出（审计）。

各步幂等：产物存在即跳过。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 使 `porter.xxx` 包可导入（脚本直跑场景：python3 porter/main.py）
_TOOL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_TOOL_ROOT))

from porter.env import category as t2      # noqa: E402
from porter.env import extract as t3       # noqa: E402
from porter.env import inputs as t1        # noqa: E402
from porter.env import gate as t5          # noqa: E402


def cmd_p0(args) -> int:
    workroot = Path(args.workroot).resolve()
    ws = workroot / args.name
    proj_path = ws / "project.json"

    # T1 输入解析
    if not proj_path.exists():
        t1.init_workspace(
            name=args.name,
            linux_driver=Path(args.linux_driver),
            target_os=Path(args.target_os),
            materials=[Path(m) for m in (args.materials or [])],
            workroot=workroot)
    else:
        print(f"[porter] T1: 复用已有工作区 {ws}")

    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    linux_driver = Path(proj["linux_driver"])
    target_os = Path(proj["target_os"])
    materials = [Path(m) for m in proj.get("materials", [])]

    # T2 类别识别
    if proj.get("category"):
        print(f"[porter] T2: 复用 category={proj['category']}")
    else:
        t2.write_result(ws, t2.identify_category(linux_driver, ws,
                                                 override=args.category))
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    cats = proj.get("category") or []

    # T3 环境信息提取（多轮循环 + 探测交织 + 人工升级/答案整合）
    rc = t3.extract_env(ws, target_os, materials, cats)
    if rc != 0:
        return rc        # 3=需人工（填 answers.md 后重跑）；1=失败

    # T5 门禁
    return 0 if t5.run_gate(ws) else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="porter",
        description="driver_migration_tool — Linux→任意目标OS 驱动迁移工具")
    sub = ap.add_subparsers(dest="phase", required=True)

    p0 = sub.add_parser("p0", help="P0：开发环境门禁（输入解析→类别→探测→脚手架→门禁）")
    p0.add_argument("--linux-driver", required=True)
    p0.add_argument("--target-os", required=True)
    p0.add_argument("--materials", action="append", default=None,
                    metavar="PATH",
                    help="开发者资料（可多次：文档/笔记/目录，形态不限；可省略）")
    p0.add_argument("--name", required=True, help="迁移项目名（工作区目录名）")
    p0.add_argument("--category", default=None,
                    help="人工指定类别（逗号分隔多标签；缺省由 agent 识别）")
    p0.add_argument("--workroot", default="./migrations")
    p0.set_defaults(func=cmd_p0)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
