"""porter/main.py — 工具总入口。

用法：

    python3 porter/main.py p0 \
        --linux-driver /path/to/linux/drivers/net/ethernet/intel/e1000 \
        --target-os    /path/to/asterinas \
        --materials  /path/to/notes.md  --materials /path/to/docs-dir \
        --output-dir /path/to/my-first-port \
        [--category net]

--materials 可多次（开发者提供的自由资料：文档/笔记/配置，形态不限；
可完全省略——agent 将仅凭目标 OS 源码树提取）。

T3 为多轮 agent×探测循环（≤3 轮自动修正）；仍未完成则生成
reports/human_questions.md 并以退出码 3 暂停——把答案写入工作区
answers.md 后重跑即进入 R4 答案整合轮。

子命令 = 阶段（p0 现已实现；p1..p6 随各阶段实现加入）。

目录约定：
    porter/common/   跨阶段共用脚本（agent 调用抽象等）
    porter/env/      P0 专属（目标环境接入与验证）
    porter/divide/   P1 专属（拆分策略→模块划分→依赖解环）

工作区布局（output_dir 为用户指定根目录）：
    <output_dir>/
    ├── project.json          项目身份（幂等真值源，跨阶段共享）
    ├── runner.json           机器可执行命令（P0 产出，跨阶段共享）
    ├── answers.md            人工填的 T3 答案
    ├── P0/
    │   ├── logs/             P0 各步 agent/命令原始输出
    │   └── reports/          P0 步骤结论（T3 轮次/探测/门禁报告）
    ├── P1/
    │   ├── strategy.md       拆分策略分析（P1S 产出）
    │   ├── logs/             P1 各步日志
    │   ├── reports/          P1 步骤结论（plan/环报告/知识报告）
    │   ├── modules/          物理切分模块（P1D 产出/P1R 重切）
    ├── P2/
    │   ├── mapping.json      API 映射真值源（P2a 起，P3 增量累积）
    │   ├── mapping.md        人读渲染（域分节表 + 换思路 + 接线清单）
    │   ├── logs/             P2 agent/验收原始输出
    │   └── reports/          spine_api / 映射报告 / 骨架清单 / 验收
    ├── P3/<M>/               垂直循环·分析（使用面/增量映射/gap 分类/
    │   ├── reports/            判据草案/探针注册表/报告）
    │   └── logs/
    ├── P4/<M>/               垂直循环·生产（fill/迁移切片/轮末快速冒烟）
    │   ├── reports/            （fill.json / migration.json；旧编号期还
    │   └── logs/                 有 acceptance.json——P5 只读兼容）
    ├── P5/<M>/               垂直循环·模块级验收（L1/L2/L0/L3 + 累积回归
    │   ├── reports/            + deferred；acceptance.json / report.md）
    │   └── logs/
    ├── loop_state.json       循环状态机（order + 每模块 phase/attempts）
    ├── deferred.json         deferred 判据登记（消费者落地时清偿）
    ├── platform_patches.json fill/register-fill 登记（P7 上游补丁素材）
    ├── human_questions.md    exit 3 人工关口问题（answers.md 承接）
    └── answers.md            人工答案（T3/loop 共用，被消费的节自动移除）

各步幂等：产物存在即跳过。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# 使 `porter.xxx` 包可导入（脚本直跑场景：python3 porter/main.py）
_TOOL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_TOOL_ROOT))

from porter.bootstrap import run as p2        # noqa: E402
from porter.divide import resolve as p1r     # noqa: E402
from porter.divide import run as p1a        # noqa: E402
from porter.divide import strategy as p1s    # noqa: E402
from porter.env import category as t2      # noqa: E402
from porter.env import extract as t3      # noqa: E402
from porter.env import inputs as t1      # noqa: E402
from porter.env import gate as t5         # noqa: E402
from porter.loop import run as loop_mod   # noqa: E402


def cmd_p0(args) -> int:
    ws = Path(args.output_dir).resolve()
    proj_path = ws / "project.json"

    # T1 输入解析
    if not proj_path.exists():
        t1.init_workspace(
            output_dir=ws,
            linux_driver=Path(args.linux_driver),
            target_os=Path(args.target_os),
            materials=[Path(m) for m in (args.materials or [])])
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


def cmd_p1_strategy(args) -> int:
    """P1 拆分策略选择（agent 产出策略分析 strategy.md → 人工审阅放行）。"""
    ws = Path(args.output_dir).resolve()
    proj_path = ws / "project.json"
    if not proj_path.exists():
        print(f"[porter] 工作区不存在：{ws}（先跑 p0）")
        return 2
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    driver_root = Path(proj["linux_driver"])
    if not driver_root.is_dir():
        print(f"[porter] linux_driver 路径无效: {driver_root}")
        return 2
    return p1s.run_strategy(ws, driver_root)


def cmd_p1_resolve(args) -> int:
    """P1 依赖解环（扫描→环报告→agent 搬运循环→拓扑序落盘 deps.json）。"""
    ws = Path(args.output_dir).resolve()
    proj_path = ws / "project.json"
    if not proj_path.exists():
        print(f"[porter] 工作区不存在：{ws}（先跑 p0）")
        return 2
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    driver_root = Path(proj["linux_driver"])
    if not driver_root.is_dir():
        print(f"[porter] linux_driver 路径无效: {driver_root}")
        return 2
    strategy = Path(args.strategy).resolve() if args.strategy else None
    return p1r.run_resolve(ws, driver_root, strategy_path=strategy)


def cmd_p1_divide(args) -> int:
    """P1 任务A：模块划分（agent 方案 × 脚本抽取/分析 × 修正循环）。"""
    ws = Path(args.output_dir).resolve()
    proj_path = ws / "project.json"
    if not proj_path.exists():
        print(f"[porter] 工作区不存在：{ws}（先跑 p0）")
        return 2
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    driver_root = Path(proj["linux_driver"])
    if not driver_root.is_dir():
        print(f"[porter] linux_driver 路径无效: {driver_root}")
        return 2
    return p1a.run_divide(ws, driver_root)


def cmd_p1_promote(args) -> int:
    """样例草稿晋升（沉淀）：temp/splits/strategies → knowledge/...。"""
    return p1s.promote_sample(args.driver)


def _p2_context(args):
    """P2 公共上下文：工作区 + 驱动/目标树路径（缺则 None 元组）。"""
    ws = Path(args.output_dir).resolve()
    proj_path = ws / "project.json"
    if not proj_path.exists():
        print(f"[porter] 工作区不存在：{ws}（先跑 p0）")
        return ws, None, None
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    driver_root = Path(proj["linux_driver"])
    target_os = Path(proj["target_os"])
    if not driver_root.is_dir() or not target_os.is_dir():
        print(f"[porter] 路径无效: {driver_root} / {target_os}")
        return ws, None, None
    return ws, driver_root, target_os


def cmd_p2_promote(args) -> int:
    """P2 映射知识晋升：temp/maps → knowledge/maps（同名=版本更新替换）。"""
    from porter.bootstrap import knowledge as kn
    return kn.promote_map(args.driver, target=args.target)


def _loop_module(args):
    """p3/p4/loop 公共：工作区校验 + 可选 --module。"""
    ws = Path(args.output_dir).resolve()
    if not (ws / "project.json").exists():
        print(f"[porter] 工作区不存在：{ws}（先跑 p0）")
        return ws, False
    return ws, True


def cmd_p3(args) -> int:
    """P3(M)：使用面提取 + 增量映射 + gap 处置分类 + 判据草案 + 探针。"""
    from porter.loop import p3 as p3_mod
    from porter.loop.state import LoopState
    ws, ok = _loop_module(args)
    if not ok:
        return 2
    st = LoopState(ws)
    if not st.load_or_init():
        return 2
    module = args.module or st.pointer()
    if module is None:
        print("[porter] p3: 全部模块已完成")
        return 0
    print(f"[porter] p3: 目标模块 {module}"
          + ("（--module 指定）" if args.module else "（断点指针）"))
    rc = p3_mod.run_p3(ws, module, st.order)
    if rc == 0 and st.phase_of(module) in (None, "pending"):
        st.set_phase(module, "p3")     # 单跑 p3 记检查点；p4 由 loop/p4 推进
    return rc


def cmd_p4(args) -> int:
    """P4(M)：fill 统一阶段 + 切片迁移 + 轮末快速冒烟（验收归 P5）。"""
    from porter.loop import p4 as p4_mod
    from porter.loop.state import LoopState
    ws, ok = _loop_module(args)
    if not ok:
        return 2
    st = LoopState(ws)
    if not st.load_or_init():
        return 2
    module = args.module or st.pointer()
    if module is None:
        print("[porter] p4: 全部模块已完成")
        return 0
    if st.phase_of(module) == "pending":
        print(f"[porter] p4: 模块 {module} 尚未跑 P3（先 p3 或用 loop）")
        return 2
    rc = p4_mod.run_p4(ws, module, st.order)
    if rc == 0:
        st.set_phase(module, "p5")   # 模块级验收归 P5(M)
    return rc


def cmd_p5(args) -> int:
    """P5(M)：模块级验收（L1/L2/L0/L3 + 累积回归 + deferred 登记/清偿）。"""
    from porter.loop import p5 as p5_mod
    from porter.loop.state import LoopState
    ws, ok = _loop_module(args)
    if not ok:
        return 2
    st = LoopState(ws)
    if not st.load_or_init():
        return 2
    module = args.module or st.pointer()
    if module is None:
        print("[porter] p5: 全部模块已完成")
        return 0
    phase = st.phase_of(module)
    if phase in ("pending", "p3", "p4"):
        print(f"[porter] p5: 模块 {module} 尚未跑 P4（先 p4 或用 loop）")
        return 2
    if phase == "done":
        acc = p5_mod.acceptance_path(ws, module)
        if acc.exists():
            try:
                if json.loads(acc.read_text(encoding="utf-8")).get("pass"):
                    print(f"[porter] p5: {module} 验收已 PASS——复用 {acc}")
                    return 0
            except (OSError, json.JSONDecodeError):
                pass
        print(f"[porter] p5: {module} 已 done——按显式请求重跑模块级验收")
    rc = p5_mod.run_p5(ws, module, st.order)
    if rc == 0 and phase != "done":
        st.set_phase(module, "done")
    return rc


def cmd_loop(args) -> int:
    """垂直循环：P3(M)→P4(M)→P5(M) ×N（拓扑序，断点重入，异常 exit 3）。"""
    ws, ok = _loop_module(args)
    if not ok:
        return 2
    return loop_mod.run_loop(ws, module=args.module,
                             max_modules=args.max_modules)


def cmd_p6(args) -> int:
    """P6 系统验收（聚合 / --finalize-l4 / --execute [--l4] / defects 账本）。"""
    from porter.loop import p6 as p6_mod
    ws = Path(args.output_dir).resolve()
    if not (ws / "project.json").exists():
        print(f"[porter] 工作区不存在：{ws}（先跑 p0）")
        return 2

    # defects 账本子操作（P6-5 缺陷修复循环消费；与模式互斥）
    if args.defect_add:
        try:
            e = p6_mod.add_defect(ws, args.defect_add, args.title or "",
                                  args.evidence or "")
            print(f"[porter] P6: 缺陷登记 {e['id']}（{e['title']}）")
            return 0
        except ValueError as ex:
            print(f"[porter] P6: {ex}")
            return 1
    if args.defect_close:
        try:
            e = p6_mod.close_defect(ws, args.defect_close,
                                    args.root_cause or "",
                                    args.fix or "",
                                    args.regression or "")
            print(f"[porter] P6: 缺陷闭账 {e['id']}（根因/修复/回归证据"
                  "四字段完整）")
            return 0
        except ValueError as ex:
            print(f"[porter] P6: {ex}")
            return 1
    if args.defect_park:
        try:
            e = p6_mod.park_defect(ws, args.defect_park, args.reason or "")
            print(f"[porter] P6: 缺陷泊车 {e['id']}")
            return 0
        except ValueError as ex:
            print(f"[porter] P6: {ex}")
            return 1
    if args.defect_list:
        for e in p6_mod.load_defects(ws)["defects"]:
            print(f"[porter] P6: {e['status']:<10} {e['id']:<44} "
                  f"{e['title']}")
        return 0
    if args.defect_diagnose:
        return p6_mod.diagnose_defect(ws, args.defect_diagnose)

    return p6_mod.run_p6(ws, execute_flag=args.execute, l4=args.l4,
                         finalize_flag=args.finalize_l4)


def cmd_p7(args) -> int:
    """P7 终态报告：聚合 + baseline diff + platform_patches 台账（提案定稿）。"""
    from porter.loop import p7 as p7_mod
    ws = Path(args.output_dir).resolve()
    if not (ws / "project.json").exists():
        print(f"[porter] 工作区不存在：{ws}（先跑 p0）")
        return 2
    return p7_mod.run_p7_cli(
        ws, patch_register=args.patch_register, title=args.title or "",
        rationale=args.rationale or "", patch_status=args.patch_status,
        status_to=args.to or "", doc=args.doc, note=args.note or "")


def _parse_device_ids(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    ids = [s.strip() for s in raw.split(",") if s.strip()]
    return ids or None


def cmd_p2(args) -> int:
    """P2 全流程：引导映射（2a）→ 全局骨架（2b）→ 验收（build/boot+日志）。"""
    ws, driver_root, target_os = _p2_context(args)
    if driver_root is None:
        return 2
    return p2.run_p2(ws, driver_root, target_os,
                     device_ids=_parse_device_ids(args.device_ids))


def cmd_p2_map(args) -> int:
    """P2a 引导映射（agent 分批 + 机器校验；断点重入幂等）。"""
    from porter.bootstrap import mapping as p2a
    ws, driver_root, target_os = _p2_context(args)
    if driver_root is None:
        return 2
    return p2a.run_map(ws, driver_root, target_os)


def cmd_p2_skeleton(args) -> int:
    """P2b 骨架生成（幂等；--device-ids 覆盖默认收敛）。"""
    from porter.bootstrap import skeleton as p2b
    ws, _driver_root, target_os = _p2_context(args)
    if target_os is None:
        return 2
    return p2b.run_skeleton(ws, target_os,
                            device_ids=_parse_device_ids(args.device_ids))


def cmd_p2_probes(args) -> int:
    """P2c 探针预生成（幂等补跑；存量工作区前置化入口）。"""
    from porter.bootstrap import pregen as p2c
    ws, _driver_root, target_os = _p2_context(args)
    if target_os is None:
        return 2
    return p2c.run_pregen(ws, target_os, max_batches=args.max_batches)


def _parse_knowledge_table(rpt: Path) -> list[str]:
    """解析 P1-knowledge.md「本步样例草稿与价值判定」表格的数据行
    （跳过表头与 |---| 分隔行）。"""
    rows: list[str] = []
    in_sec = False
    seen_sep = False
    for ln in rpt.read_text(encoding="utf-8").splitlines():
        if ln.startswith("## "):
            if in_sec:
                break
            in_sec = ln.strip() == "## 本步样例草稿与价值判定"
            continue
        if not (in_sec and ln.startswith("|")):
            continue
        body = ln.replace("|", "").strip()
        if body and set(body) <= {"-", " "}:      # |---|---| 分隔行
            seen_sep = True
            continue
        if not seen_sep:                          # 表头行
            continue
        rows.append(ln)
    return rows


def _write_p1_final_report(ws: Path) -> Path:
    """P1 末尾汇总报告（只读既有产物，零副作用）→ P1/reports/report.md。"""
    p1 = ws / "P1"

    # temp 样例草稿添加清单
    klg = p1 / "reports" / "P1-knowledge.md"
    if klg.exists():
        rows = _parse_knowledge_table(klg)
        temp_block = (["| 条目文件 | 驱动名 | Linux 目录 | 文件数 | 状态 | 价值判定 |",
                       "|---|---|---|---|---|---|"] + rows) if rows \
            else ["（P1-knowledge.md 中无草稿记录——temp 分区无添加）"]
    else:
        temp_block = ["（未找到 P1-knowledge.md——strategy 步未跑或产物被清）"]

    # divide 摘要
    plan_path = p1 / "reports" / "P1D_plan.json"
    if plan_path.exists():
        try:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            n_mod = len(plan.get("modules") or [])
            n_frag = sum(len(f.get("fragments") or [])
                         for m in plan.get("modules") or []
                         for f in m.get("files") or [])
            divide_lines = [f"- 模块数：{n_mod}", f"- 片段总数：{n_frag}",
                            f"- 依据：`{plan_path}`"]
        except (json.JSONDecodeError, OSError) as e:
            divide_lines = [f"- ⚠️ P1D_plan.json 解析失败：{e}"]
    else:
        divide_lines = ["- （未找到 P1D_plan.json——divide 步未跑或产物被清）"]

    # resolve 摘要
    deps_path = p1 / "modules" / "deps.json"
    if deps_path.exists():
        try:
            deps = json.loads(deps_path.read_text(encoding="utf-8"))
            cycles = deps.get("cycles") or []
            cyc = "[]（无环）" if not cycles else json.dumps(
                cycles, ensure_ascii=False)
            order = deps.get("order") or []
            topo = " → ".join(order) if order else "（空）"
            resolve_lines = [f"- cycles：{cyc}", f"- 拓扑序：{topo}",
                             f"- 依据：`{deps_path}`"]
        except (json.JSONDecodeError, OSError) as e:
            resolve_lines = [f"- ⚠️ deps.json 解析失败：{e}"]
    else:
        resolve_lines = ["- （未找到 deps.json——resolve 步未跑或产物被清）"]

    lines = [
        "# P1 汇总报告", "",
        f"- 工作区: {ws.name}",
        f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## temp 样例草稿添加清单", "",
        *temp_block, "",
        "## divide 摘要", "",
        *divide_lines, "",
        "## resolve 摘要", "",
        *resolve_lines, "",
        "## 沉淀决策（人工）", "",
        "若认为本次策略有沉淀价值，执行：",
        "",
        "    python3 porter/main.py p1-promote --driver <条目文件名或驱动名>",
        "",
        "语义：temp → knowledge；真重复拒绝、构成不同自动改名并入、",
        "同名多条目歧义时列候选要求指定条目文件名。",
    ]
    rpt = p1 / "reports" / "report.md"
    rpt.parent.mkdir(parents=True, exist_ok=True)
    rpt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rpt


def cmd_p1(args) -> int:
    """P1 全流程：strategy → divide → resolve（直通，末尾生成汇总报告）。"""
    ws = Path(args.output_dir).resolve()
    proj_path = ws / "project.json"
    if not proj_path.exists():
        print(f"[porter] 工作区不存在：{ws}（先跑 p0）")
        return 2
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    driver_root = Path(proj["linux_driver"])
    if not driver_root.is_dir():
        print(f"[porter] linux_driver 路径无效: {driver_root}")
        return 2
    for step in (p1s.run_strategy, p1a.run_divide, p1r.run_resolve):
        rc = step(ws, driver_root)   # resolve 第三参 strategy_path 缺省 None，兼容
        if rc != 0:
            return rc
    rpt = _write_p1_final_report(ws)
    print(f"[porter] P1: 全流程完成，报告 → {rpt}")
    return 0


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
    p0.add_argument("--output-dir", required=True,
                    help="迁移工作区根目录（各阶段在内部建 P0/、P1/ 等子目录）")
    p0.add_argument("--category", default=None,
                    help="人工指定类别（逗号分隔多标签；缺省由 agent 识别）")
    p0.set_defaults(func=cmd_p0)

    p1all = sub.add_parser("p1", help="P1 全流程：strategy → divide → resolve（直通，末尾汇总报告）")
    p1all.add_argument("--output-dir", required=True, help="迁移工作区根目录（须先跑过 p0）")
    p1all.set_defaults(func=cmd_p1)

    p1s = sub.add_parser("p1-strategy", help="P1 拆分策略选择（agent 分析→strategy.md 待人工审阅）")
    p1s.add_argument("--output-dir", required=True, help="迁移工作区根目录（须先跑过 p0）")
    p1s.set_defaults(func=cmd_p1_strategy)

    p1r = sub.add_parser("p1-resolve", help="P1 依赖解环（环检测→agent 搬运循环→拓扑序）")
    p1r.add_argument("--output-dir", required=True, help="迁移工作区根目录（须先跑过 p1-divide）")
    p1r.add_argument("--strategy", default=None,
                     help="拆分策略文件路径（注入 agent prompt）；缺省 <P1>/strategy.md")
    p1r.set_defaults(func=cmd_p1_resolve)

    p1d = sub.add_parser("p1-divide", help="P1 任务A：模块划分（物理切分+依赖分析）")
    p1d.add_argument("--output-dir", required=True, help="迁移工作区根目录（须先跑过 p0）")
    p1d.set_defaults(func=cmd_p1_divide)

    p1p = sub.add_parser("p1-promote", help="样例草稿晋升：temp → knowledge（沉淀，P1 完成后人工决定执行）")
    p1p.add_argument("--driver", required=True, help="要晋升的驱动名或条目文件名（同名多条目时须给条目文件名）")
    p1p.set_defaults(func=cmd_p1_promote)

    def _add_device_ids(sp):
        sp.add_argument("--device-ids", default=None, metavar="V:D[,V:D...]",
                        help="PCI 设备 ID 收敛清单（如 0x8086:0x100e；"
                             "缺省用 P1 策略默认 QEMU 目标）")

    p2all = sub.add_parser("p2", help="P2 全流程：引导映射 → 全局骨架 → 验收")
    p2all.add_argument("--output-dir", required=True, help="迁移工作区根目录（须先跑过 p0/p1）")
    _add_device_ids(p2all)
    p2all.set_defaults(func=cmd_p2)

    p2m = sub.add_parser("p2-map", help="P2a 引导映射（agent 分批小调用；断点重入幂等）")
    p2m.add_argument("--output-dir", required=True, help="迁移工作区根目录")
    p2m.set_defaults(func=cmd_p2_map)

    p2s = sub.add_parser("p2-skeleton", help="P2b 全局骨架生成（幂等）")
    p2s.add_argument("--output-dir", required=True, help="迁移工作区根目录")
    _add_device_ids(p2s)
    p2s.set_defaults(func=cmd_p2_skeleton)

    p2pr = sub.add_parser("p2-probes", help="P2c 探针预生成（幂等补跑；风险主张前置验证，P3 探针步骤退化为补新）")
    p2pr.add_argument("--output-dir", required=True, help="迁移工作区根目录")
    p2pr.add_argument("--max-batches", type=int, default=None,
                      help="本次最多生成批数（≤5 条/批；缺省全量）")
    p2pr.set_defaults(func=cmd_p2_probes)

    p2p = sub.add_parser("p2-promote", help="映射知识晋升：temp/maps → knowledge/maps（P2 末/循环中人工决定执行）")
    p2p.add_argument("--driver", required=True, help="要晋升的驱动名")
    p2p.add_argument("--target", default=None, help="目标 OS 名（同名歧义时必须指定）")
    p2p.set_defaults(func=cmd_p2_promote)

    def _add_loop_common(sp):
        sp.add_argument("--output-dir", required=True, help="迁移工作区根目录（须先跑过 p0/p1/p2）")
        sp.add_argument("--module", default=None, help="目标模块名（缺省 = loop_state 断点指针）")

    p3cmd = sub.add_parser("p3", help="P3(M)：使用面提取 + 增量映射 + gap 分类 + 判据草案 + 探针")
    _add_loop_common(p3cmd)
    p3cmd.set_defaults(func=cmd_p3)

    p4cmd = sub.add_parser("p4", help="P4(M)：fill 统一 + 切片迁移 + 轮末快速冒烟")
    _add_loop_common(p4cmd)
    p4cmd.set_defaults(func=cmd_p4)

    p5cmd = sub.add_parser("p5", help="P5(M)：模块级验收 L1/L2/L0/L3 + 累积回归 + deferred")
    _add_loop_common(p5cmd)
    p5cmd.set_defaults(func=cmd_p5)

    loopcmd = sub.add_parser("loop", help="垂直循环：P3(M)→P4(M)→P5(M) ×N（拓扑序/断点重入/泊车绕行/异常 exit 3）")
    _add_loop_common(loopcmd)
    loopcmd.add_argument("--max-modules", type=int, default=None,
                         help="本次最多完成的模块数（首切片验证用）")
    loopcmd.set_defaults(func=cmd_loop)

    p6cmd = sub.add_parser("p6", help="P6 系统验收：聚合健康 / --finalize-l4 定稿门 / --execute [--l4] 执行重测 / defects 账本")
    p6cmd.add_argument("--output-dir", required=True, help="迁移工作区根目录")
    p6cmd.add_argument("--execute", action="store_true",
                       help="执行模式：一轮 build+SLIRP boot+ktest 判全部判据 + deferred 清偿")
    p6cmd.add_argument("--l4", action="store_true",
                       help="（随 --execute）按定稿后的 l4_criteria.json 判全部 L4 判据")
    p6cmd.add_argument("--finalize-l4", action="store_true",
                       help="L4 判据定稿门（按 porter/config.json 审核门停车或续跑）")
    p6cmd.add_argument("--defect-add", default=None, metavar="ID",
                       help="登记缺陷（需 --title / --evidence）")
    p6cmd.add_argument("--title", default=None)
    p6cmd.add_argument("--evidence", default=None)
    p6cmd.add_argument("--defect-close", default=None, metavar="ID",
                       help="闭账缺陷（需 --root-cause/--fix/--regression，四字段强制）")
    p6cmd.add_argument("--root-cause", default=None)
    p6cmd.add_argument("--fix", default=None)
    p6cmd.add_argument("--regression", default=None)
    p6cmd.add_argument("--defect-park", default=None, metavar="ID",
                       help="泊车缺陷（需 --reason）")
    p6cmd.add_argument("--reason", default=None)
    p6cmd.add_argument("--defect-list", action="store_true",
                       help="列出 defects.json 账本")
    p6cmd.add_argument("--defect-diagnose", default=None, metavar="ID",
                       help="缺陷诊断（§15 挂载③/D1 步）：triage→处置→"
                            "有界诊断→升级报告，全程 defects history 落账")
    p6cmd.set_defaults(func=cmd_p6)

    p7cmd = sub.add_parser("p7", help="P7 终态报告：聚合 + baseline diff + 补丁提案台账")
    p7cmd.add_argument("--output-dir", required=True, help="迁移工作区根目录")
    p7cmd.add_argument("--patch-register", default=None, metavar="GAP",
                       help="登记新补丁提案（status=proposed；需 --title；提案文档 P7/reports/patches/<GAP>.md 人工撰写）")
    p7cmd.add_argument("--title", default=None)
    p7cmd.add_argument("--rationale", default=None)
    p7cmd.add_argument("--patch-status", default=None, metavar="GAP",
                       help="补丁状态流转（配 --to planned|proposed|closed）")
    p7cmd.add_argument("--to", default=None, metavar="STATUS")
    p7cmd.add_argument("--doc", default=None)
    p7cmd.add_argument("--note", default=None,
                       help="（closed 时）处置理由入档")
    p7cmd.set_defaults(func=cmd_p7)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
