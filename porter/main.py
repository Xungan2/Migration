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
from porter import log as _log


def _print_kb_guidance() -> None:
    from porter.bootstrap import kb as _kb
    existing = sorted(
        d.name for d in _kb.KB_ROOT.iterdir()
        if d.is_dir() and d.name not in ("base", "temp")
    ) if _kb.KB_ROOT.is_dir() else []
    _log.console_line("[porter] p0: 未指定知识库目录——须显式选择（知识子系统）：")
    print("  新建（复制 base 工具随附知识）：--kb new <名>")
    print("  新建（空目录）：                --kb new <名> --kb-empty")
    print("  从全局库种子化（沉淀复用）：    --kb use <名>")
    if existing:
        print(f"  既有全局库：{', '.join(existing)}")
    print("  （知识库物化在 <ws>/knowledge/，随工作区 git 统一入库；"
          "promote 后自动同步回全局库）")


def _p0_kb_decision(args, proj_path: Path) -> tuple[int | None, str | None]:
    """p0 的知识库目录决策。返回 (rc, kb_name)；rc 非 None 时直接返回。

    只做参数校验与复用判定，不物化（物化需工作区存在，延后到 T1 之后
    的 select_kb(ws=...)）。
    """
    from porter.bootstrap import kb as _kb
    kb_arg = getattr(args, "kb", None)
    if kb_arg:
        mode, name = kb_arg
        err = _kb.validate_kb_arg(mode, name)
        if err:
            _log.console_line(f"[porter] --kb: {err}")
            return 2, None
        return None, name
    if proj_path.exists():
        try:
            proj = json.loads(proj_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            proj = {}
        if proj.get("kb_dir"):
            _log.console_line(f"[porter] p0: 复用已记录知识库目录 "
                  f"kb_dir={proj['kb_dir']}")
            return None, None
    _print_kb_guidance()
    return 2, None


def _record_kb(proj_path: Path, kb_name: str) -> None:
    """把 kb_dir 记入 project.json（幂等）。"""
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    if proj.get("kb_dir") == kb_name:
        return
    proj["kb_dir"] = kb_name
    proj_path.write_text(
        json.dumps(proj, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    _log.console_line(f"[porter] p0: 知识库目录已记录 kb_dir={kb_name}")


def cmd_p0(args) -> int:
    ws = Path(args.output_dir).resolve()
    proj_path = ws / "project.json"

    # 知识库目录选择（显式必填；rc 2 逼选择——知识子系统定案）
    kb_rc, kb_name = _p0_kb_decision(args, proj_path)
    if kb_rc is not None:
        return kb_rc

    # T1 输入解析
    if not proj_path.exists():
        t1.init_workspace(
            output_dir=ws,
            linux_driver=Path(args.linux_driver),
            target_os=Path(args.target_os),
            materials=[Path(m) for m in (args.materials or [])])
    else:
        _log.console_line(f"[porter] T1: 复用已有工作区 {ws}")

    # 知识库物化（工作区就绪后；--kb 给出时。kb 进 <ws>/knowledge/，
    # 随工作区 git 统一入库——vcs 统一管理版）
    if kb_name is not None:
        from porter.bootstrap import kb as _kb
        d = _kb.select_kb(args.kb[0], args.kb[1],
                          empty=getattr(args, "kb_empty", False),
                          ws=ws)
        if d is None:
            return 2
        _record_kb(proj_path, kb_name)

    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    linux_driver = Path(proj["linux_driver"])
    target_os = Path(proj["target_os"])
    materials = [Path(m) for m in proj.get("materials", [])]

    # VCS 登记（目标树并行仓 baseline + porter 分支 + 工作区 git init；
    # 幂等，resume 只补齐分支）
    try:
        from porter.common import vcs as _vcs
        _vrc = _vcs.hook_p0(ws, target_os,
                            branch=getattr(args, "os_branch", None))
        if _vrc != 0:
            return _vrc
    except Exception as _e:
        _log.console_line(f"[porter] p0: ⚠️ vcs 登记异常（不阻塞）：{_e}")

    # T2 类别识别
    if proj.get("category"):
        _log.console_line(f"[porter] T2: 复用 category={proj['category']}")
    else:
        t2.write_result(ws, t2.identify_category(linux_driver, ws,
                                                 override=args.category))
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    cats = proj.get("category") or []

    # T3 环境信息提取（多轮循环 + 探测交织 + 人工升级/答案整合）
    # 先消费 answers.md 的 @ 关口答案（新协议）
    try:
        from porter.loop import gates as _gates
        _gates.process_answered_gates(ws)
    except Exception:
        pass
    rc = t3.extract_env(ws, target_os, materials, cats)
    if rc != 0:
        return rc        # 3=需人工（按表单作答后重跑）；1=失败

    # T5 门禁（FAIL = 环境坏 → panic 关口：agent 只备料，人修环境）
    if t5.run_gate(ws):
        # runbook 域收成（T5 过后 runner 定型——固定知识定点产出）
        try:
            from porter.bootstrap import runbook as _rb
            _rb.draft_runbook(ws)
        except Exception as _e:
            _log.console_line(f"[porter] p0: ⚠️ runbook 草稿刷新失败（不影响主流程）：{_e}")
        # CP0 环境审：非阻塞 memo（runner 复核建议，复活死标志
        # meta.reviewed 的意图）+ digest——T3 阻塞问题已是 panic 关口
        from porter.loop import gates as _gates3
        _gates3.checkpoint_run(
            ws, "CP0", register=[{
                "id": "cp0.runner_review", "kind": "memo",
                "gate_type": "decision", "phase": "P0", "checkpoint": "CP0",
                "blocking": False,
                "question": ("runner.json 驱动全流水线（构建/启动/测试命令"
                             "）——建议扫一眼 P0/reports/memo.md 与 "
                             "runner.json 再进入 P1（非阻塞建议）。"),
                "context_files": ["runner.json", "P0/reports/memo.md"],
                "answer_form": [{"field": "note", "type": "text",
                                  "required": False}]}],
            blocking=False)
        # vcs：P0 阶段末工作区 commit（best-effort）
        try:
            from porter.common import vcs as _vcs
            _vcs.commit_workspace(ws, "P0: done", phase="P0")
        except Exception:
            pass
        return 0
    from porter.loop import gates as _gates2
    return _gates2.panic(ws, {
        "id": "p0.t5.env_gate", "kind": "fact", "gate_type": "physical",
        "phase": "P0",
        "question": ("P0 门禁 FAIL：未动过的目标树单测烟测未过——验证"
                     "基线本身是坏的，后续所有机器判定都会把环境故障"
                     "误判为迁移故障。请修复开发环境后重跑 p0。"
                     "报告见 P0/reports/p0_report.md。"),
        "context_files": ["P0/reports/p0_report.md"],
        "answer_form": [
            {"field": "note", "type": "text", "required": False,
             "hint": "修复说明（留档）"}],
    })


def _log_bind(ws: Path, mount: str) -> None:
    """log 子系统入口 init（观测扩全：补齐无 bind 的子命令入口）。"""
    try:
        from porter import log as _log
        _log.bind(ws, mount)
    except Exception:
        pass


def cmd_p1_strategy(args) -> int:
    """P1 拆分策略选择（agent 产出策略分析 strategy.md → 人工审阅放行）。"""
    ws = Path(args.output_dir).resolve()
    proj_path = ws / "project.json"
    if not proj_path.exists():
        _log.console_line(f"[porter] 工作区不存在：{ws}（先跑 p0）")
        return 2
    _log_bind(ws, "p1")
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    driver_root = Path(proj["linux_driver"])
    if not driver_root.is_dir():
        _log.console_line(f"[porter] linux_driver 路径无效: {driver_root}")
        return 2
    return p1s.run_strategy(ws, driver_root)


def cmd_p1_resolve(args) -> int:
    """P1 依赖解环（扫描→环报告→agent 搬运循环→拓扑序落盘 deps.json）。"""
    ws = Path(args.output_dir).resolve()
    proj_path = ws / "project.json"
    if not proj_path.exists():
        _log.console_line(f"[porter] 工作区不存在：{ws}（先跑 p0）")
        return 2
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    driver_root = Path(proj["linux_driver"])
    if not driver_root.is_dir():
        _log.console_line(f"[porter] linux_driver 路径无效: {driver_root}")
        return 2
    strategy = Path(args.strategy).resolve() if args.strategy else None
    return p1r.run_resolve(ws, driver_root, strategy_path=strategy)


def cmd_p1_divide(args) -> int:
    """P1 任务A：模块划分（agent 方案 × 脚本抽取/分析 × 修正循环）。"""
    ws = Path(args.output_dir).resolve()
    proj_path = ws / "project.json"
    if not proj_path.exists():
        _log.console_line(f"[porter] 工作区不存在：{ws}（先跑 p0）")
        return 2
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    driver_root = Path(proj["linux_driver"])
    if not driver_root.is_dir():
        _log.console_line(f"[porter] linux_driver 路径无效: {driver_root}")
        return 2
    # CP1 拆分审（#5 必人四点：strategy.md 人工审阅关口，修 H5）
    from porter.loop import gates as _gates
    _gates.process_answered_gates(ws)
    if (ws / "P1" / "strategy.md").exists():
        rc = _gates.strategy_checkpoint(ws)
        if rc != 0:
            return rc
    _log_bind(ws, "p1")
    return p1a.run_divide(ws, driver_root)


def _kb_dir_for_promote(ws_raw) -> "Path | None":
    """promote 类命令的知识库目录解析：工作区 project.json → kb_dir。

    缺 project.json / 未记录 kb_dir → 打印指引返回 None（调用方 rc 1）。
    """
    from porter.bootstrap import kb as _kb
    ws = Path(ws_raw).resolve() if ws_raw else None
    if ws is None or not (ws / "project.json").exists():
        _log.console_line("[porter] 需 --output-dir 指向迁移工作区（解析知识库目录）")
        return None
    kb_dir = _kb.kb_dir_for(ws)
    if kb_dir is None:
        _log.console_line(f"[porter] 工作区 {ws} 未记录知识库目录（kb_dir）——"
              "新工作区请用 p0 --kb 显式指定；旧工作区请补 "
              '"kb_dir": "<knowledge/ 下的目录名>" 到 project.json')
        return None
    return kb_dir


def cmd_kb(args) -> int:
    """知识库审核/分类/晋升 CLI（随机知识后段；CP5 材料见 checkpoints/）。"""
    from porter.bootstrap import review as _rv
    ws = Path(args.output_dir).resolve() if args.output_dir else None
    if ws is None or not (ws / "project.json").exists():
        _log.console_line("[porter] kb: 需 --output-dir 指向迁移工作区")
        return 2
    _log_bind(ws, "kb")
    acted = False
    if args.classify:
        acted = True
        rc = _rv.classify_candidates(ws, ids=args.ids or None)
        if rc != 0:
            return rc
    if args.promote:
        acted = True
        ids = (args.ids if args.ids
               else [c["id"] for c in _cand_load(ws)])
        if args.promote == "all" and not ids:
            _log.console_line("[porter] kb promote: 无候选")
        for cid in ids:
            rc = _rv.promote_candidate(ws, cid, to=args.to)
            if rc != 0:
                return rc
    if args.reject:
        acted = True
        rc = _rv.reject_candidate(ws, args.reject)
        if rc != 0:
            return rc
    if not acted or args.list:
        mat = _rv.build_cp5_material(ws)
        _log.console_line(f"[porter] kb: 备审材料已刷新 {mat}")
        for c in _cand_load(ws):
            print(f"  - {c['id']}（建议类 {c.get('suggested_class')}）："
                  f"{c['draft'][:80]}")
    _kb_sync_and_commit(ws, acted)
    return 0


def _cand_load(ws: Path) -> list[dict]:
    from porter.bootstrap import candidates as _c
    return _c.load_candidates(ws)


def _kb_sync_and_commit(ws_raw, acted: bool) -> None:
    """promote 类命令收尾：知识库沉淀回全局 + 工作区 commit（best-effort）。"""
    try:
        from porter.common import vcs as _vcs
        from porter.bootstrap import kb as _kb
        if acted and _kb.sync_to_global(Path(ws_raw)):
            _log.console_line("[porter] kb: 已同步回全局知识库（跨迁移复用）")
        if acted:
            _vcs.commit_workspace(Path(ws_raw), "kb: promote", phase="kb")
    except Exception:
        pass


def cmd_p1_promote(args) -> int:
    """样例草稿晋升（沉淀）：temp/splits → 本次知识库目录。"""
    kb_dir = _kb_dir_for_promote(args.output_dir)
    if kb_dir is None:
        return 1
    rc = p1s.promote_sample(args.driver, kb_dir)
    _kb_sync_and_commit(args.output_dir, rc == 0)
    return rc


def _p2_context(args):
    """P2 公共上下文：工作区 + 驱动/目标树路径（缺则 None 元组）。"""
    ws = Path(args.output_dir).resolve()
    proj_path = ws / "project.json"
    if not proj_path.exists():
        _log.console_line(f"[porter] 工作区不存在：{ws}（先跑 p0）")
        return ws, None, None
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    driver_root = Path(proj["linux_driver"])
    target_os = Path(proj["target_os"])
    if not driver_root.is_dir() or not target_os.is_dir():
        _log.console_line(f"[porter] 路径无效: {driver_root} / {target_os}")
        return ws, None, None
    return ws, driver_root, target_os


def cmd_p2_promote(args) -> int:
    """P2 映射知识晋升：temp/maps → 知识库目录/maps（同名=版本更新替换）。"""
    from porter.bootstrap import knowledge as kn
    kb_dir = _kb_dir_for_promote(args.output_dir)
    if kb_dir is None:
        return 1
    rc = kn.promote_map(args.driver, kb_dir, target=args.target)
    _kb_sync_and_commit(args.output_dir, rc == 0)
    return rc


def _loop_module(args):
    """p3/p4/loop 公共：工作区校验 + 可选 --module。"""
    ws = Path(args.output_dir).resolve()
    if not (ws / "project.json").exists():
        _log.console_line(f"[porter] 工作区不存在：{ws}（先跑 p0）")
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
        _log.console_line("[porter] p3: 全部模块已完成")
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
        _log.console_line("[porter] p4: 全部模块已完成")
        return 0
    if st.phase_of(module) == "pending":
        _log.console_line(f"[porter] p4: 模块 {module} 尚未跑 P3（先 p3 或用 loop）")
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
        _log.console_line("[porter] p5: 全部模块已完成")
        return 0
    phase = st.phase_of(module)
    if phase in ("pending", "p3", "p4"):
        _log.console_line(f"[porter] p5: 模块 {module} 尚未跑 P4（先 p4 或用 loop）")
        return 2
    if phase == "done":
        acc = p5_mod.acceptance_path(ws, module)
        if acc.exists():
            try:
                if json.loads(acc.read_text(encoding="utf-8")).get("pass"):
                    _log.console_line(f"[porter] p5: {module} 验收已 PASS——复用 {acc}")
                    return 0
            except (OSError, json.JSONDecodeError):
                pass
        _log.console_line(f"[porter] p5: {module} 已 done——按显式请求重跑模块级验收")
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
        _log.console_line(f"[porter] 工作区不存在：{ws}（先跑 p0）")
        return 2

    # defects 账本子操作（P6-5 缺陷修复循环消费；与模式互斥）
    if args.defect_add:
        try:
            e = p6_mod.add_defect(ws, args.defect_add, args.title or "",
                                  args.evidence or "")
            _log.console_line(f"[porter] P6: 缺陷登记 {e['id']}（{e['title']}）")
            return 0
        except ValueError as ex:
            _log.console_line(f"[porter] P6: {ex}")
            return 1
    if args.defect_close:
        try:
            e = p6_mod.close_defect(ws, args.defect_close,
                                    args.root_cause or "",
                                    args.fix or "",
                                    args.regression or "")
            _log.console_line(f"[porter] P6: 缺陷闭账 {e['id']}（根因/修复/回归证据"
                  "四字段完整）")
            return 0
        except ValueError as ex:
            _log.console_line(f"[porter] P6: {ex}")
            return 1
    if args.defect_park:
        try:
            e = p6_mod.park_defect(ws, args.defect_park, args.reason or "")
            _log.console_line(f"[porter] P6: 缺陷泊车 {e['id']}")
            return 0
        except ValueError as ex:
            _log.console_line(f"[porter] P6: {ex}")
            return 1
    if args.defect_list:
        for e in p6_mod.load_defects(ws)["defects"]:
            _log.console_line(f"[porter] P6: {e['status']:<10} {e['id']:<44} "
                  f"{e['title']}")
        return 0
    if args.defect_diagnose:
        return p6_mod.diagnose_defect(ws, args.defect_diagnose)
    if args.defect_fix:
        return p6_mod.fix_defect(ws, args.defect_fix)

    rc = p6_mod.run_p6(ws, execute_flag=args.execute, l4=args.l4,
                       finalize_flag=args.finalize_l4,
                       draft_flag=args.draft_l4)
    if rc == 0 and args.execute:
        try:                            # vcs：P6 execute 后 commit（best-effort）
            from porter.common import vcs as _vcs
            _vcs.commit_target(ws, "P6: execute", phase="P6")
            _vcs.commit_workspace(ws, "P6: execute done", phase="P6")
        except Exception:
            pass
    return rc


def cmd_p7(args) -> int:
    """P7 终态报告：聚合 + baseline diff + platform_patches 台账（提案定稿）。"""
    from porter.loop import p7 as p7_mod
    ws = Path(args.output_dir).resolve()
    if not (ws / "project.json").exists():
        _log.console_line(f"[porter] 工作区不存在：{ws}（先跑 p0）")
        return 2
    _log_bind(ws, "p7")
    # CP4 验收审：缺陷自动闭账的决策债未清 → 停车批审（防证据链注水）
    from porter.loop import gates as _gates
    _gates.process_answered_gates(ws)
    ledger = _gates.GateLedger(ws).load()
    fix_debt = [g for g in ledger.pending_review()
                if g["id"].startswith("p6.defect.fix.")]
    if fix_debt:
        rc = _gates.checkpoint_run(ws, "CP4", register=[{
            "id": "cp4.defect_review", "kind": "approval",
            "gate_type": "failure", "phase": "P6", "checkpoint": "CP4",
            "question": (f"{len(fix_debt)} 条求解循环（--defect-diagnose）"
                         "自动闭账待批审"
                         "（四字段+build/boot 证据）——逐条核对后放行；"
                         "否决单条用 `## @p6.defect.fix.<ID>` "
                         "verdict: veto。"),
            "context_files": ["defects.json", "checkpoints/CP4_digest.md"],
            "answer_form": [{"field": "verdict", "type": "enum",
                             "options": ["approve", "reject"],
                             "required": True}]}])
        if rc != 0:
            return rc
    rc = p7_mod.run_p7_cli(
        ws, patch_register=args.patch_register, title=args.title or "",
        rationale=args.rationale or "", patch_status=args.patch_status,
        status_to=args.to or "", doc=args.doc, note=args.note or "")
    if rc == 0:
        # CP5 沉淀审（知识面扩容：候选队列 + temp 草稿 + KB 健康报告）
        try:
            from porter.bootstrap import review as _rv
            kb_mat = _rv.build_cp5_material(ws)
        except Exception as _e:
            kb_mat = None
            _log.console_line(f"[porter] CP5: ⚠️ 知识备审材料生成失败：{_e}")
        _gates.checkpoint_run(
            ws, "CP5", register=[{
                "id": "cp5.promote", "kind": "memo", "gate_type": "decision",
                "phase": "P7", "checkpoint": "CP5", "blocking": False,
                "question": ("知识晋升提醒：随机知识候选、temp 各域草稿、"
                             "KB 健康报告——详见知识备审材料；"
                             "用 porter kb promote / p1/p2-promote 沉淀。"),
                "context_files": (["checkpoints/CP5_knowledge.md",
                                   "knowledge/temp/maps/INDEX.json"]
                                  if kb_mat else
                                  ["knowledge/temp/maps/INDEX.json"]),
                "answer_form": [{"field": "note", "type": "text",
                                 "required": False}]}],
            blocking=False)
        # vcs：P7 阶段末 commit + 可移植导出（bundle → <ws>/exports/）
        try:
            from porter.common import vcs as _vcs
            _vcs.commit_workspace(ws, "P7: done", phase="P7")
            _m = _vcs.export_all(ws)
            if _m:
                _log.console_line(f"[porter] vcs: 可移植导出 → {_m['dir']}"
                      f"（{_m['branch'] or '默认分支'}，"
                      f"{len(_m['files'])} 个 bundle + manifest.json）")
        except Exception as _e:
            _log.console_line(f"[porter] vcs: ⚠️ 导出失败（不阻塞）：{_e}")
    return rc


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
    _log_bind(ws, "p2")
    return p2a.run_map(ws, driver_root, target_os)


def cmd_p2_skeleton(args) -> int:
    """P2b 骨架生成（幂等；--device-ids 覆盖默认收敛）。"""
    from porter.bootstrap import skeleton as p2b
    ws, _driver_root, target_os = _p2_context(args)
    if target_os is None:
        return 2
    _log_bind(ws, "p2")
    return p2b.run_skeleton(ws, target_os,
                            device_ids=_parse_device_ids(args.device_ids))


def cmd_p2_probes(args) -> int:
    """P2c 探针预生成（幂等补跑；存量工作区前置化入口）。"""
    from porter.bootstrap import pregen as p2c
    ws, _driver_root, target_os = _p2_context(args)
    if target_os is None:
        return 2
    _log_bind(ws, "p2")
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
        "    python3 porter/main.py p1-promote --output-dir <工作区> "
        "--driver <条目文件名或驱动名>",
        "",
        "语义：temp → 本次知识库目录（project.json.kb_dir）；真重复拒绝、",
        "构成不同自动改名并入、同名多条目歧义时列候选要求指定条目文件名。",
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
        _log.console_line(f"[porter] 工作区不存在：{ws}（先跑 p0）")
        return 2
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    driver_root = Path(proj["linux_driver"])
    if not driver_root.is_dir():
        _log.console_line(f"[porter] linux_driver 路径无效: {driver_root}")
        return 2
    from porter.loop import gates as _gates
    _gates.process_answered_gates(ws)
    rc = p1s.run_strategy(ws, driver_root)
    if rc != 0:
        return rc
    rc = _gates.strategy_checkpoint(ws)  # CP1：直通路径也必须过审（修 H5）
    if rc != 0:
        return rc
    for step in (p1a.run_divide, p1r.run_resolve):
        rc = step(ws, driver_root)   # resolve 第三参 strategy_path 缺省 None，兼容
        if rc != 0:
            return rc
    rpt = _write_p1_final_report(ws)
    _log.console_line(f"[porter] P1: 全流程完成，报告 → {rpt}")
    try:                                # vcs：P1 阶段末 commit（best-effort）
        from porter.common import vcs as _vcs
        _vcs.commit_workspace(ws, "P1: done", phase="P1")
    except Exception:
        pass
    return 0


def cmd_gate(args) -> int:
    """关口 CLI（S6 便利层；协议本体 = 账本 + answers.md，纯文件也可用）。"""
    from porter.loop import gates as gates_mod
    ws = Path(args.output_dir).resolve()
    if not (ws / "project.json").exists():
        _log.console_line(f"[porter] 工作区不存在：{ws}（先跑 p0）")
        return 2
    ledger = gates_mod.GateLedger(ws).load()

    if args.gate_cmd == "list":
        rows = []
        for g in ledger.gates:
            if args.status == "open" and g.get("status") not in ("open",
                                                                 "invalid"):
                continue
            if args.status == "debt" and not (
                    g.get("status") == "applied"
                    and g.get("answered_by") in ("agent", "policy")):
                continue
            rows.append(g)
        if not rows:
            _log.console_line(f"[porter] gate: 无 {args.status} 关口")
            return 0
        print(f"{'ID':<44} {'状态':<9} {'车道':<11} 类型/应答者")
        for g in sorted(rows, key=lambda x: (x.get("status") or "",
                                             x["id"])):
            print(f"{g['id']:<44} {g.get('status', '?'):<9} "
                  f"{g.get('lane', '?'):<11} {g.get('kind', '?')}"
                  f"/{g.get('answered_by') or '—'}")
        print(f"\n{gates_mod.summary_line(ws)}；作答：answers.md `## @<ID>`"
              "（表单见 human_questions.md）")
        return 0

    if args.gate_cmd == "show":
        g = ledger.find(args.gate_id)
        if g is None:
            _log.console_line(f"[porter] gate: 关口不存在: {args.gate_id}")
            return 2
        print(json.dumps(g, ensure_ascii=False, indent=2))
        return 0

    if args.gate_cmd == "answer":
        g = ledger.find(args.gate_id)
        if g is None:
            _log.console_line(f"[porter] gate: 关口不存在: {args.gate_id}")
            return 2
        if not args.set:
            _log.console_line("[porter] gate: 需要至少一个 --set field=value")
            return 2
        lines = [f"## @{args.gate_id}"]
        for kv in args.set:
            field, _, val = kv.partition("=")
            lines.append(f"{field.strip()}: {val.strip()}")
        with (ws / "answers.md").open("a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(lines) + "\n")
        _log.console_line(f"[porter] gate: 答案已写入 answers.md（{args.gate_id}）")
        applied, invalid = gates_mod.process_answered_gates(ws, ledger)
        if invalid:
            _log.console_line(f"[porter] gate: ⚠️ 校验失败 {invalid} 条——错误见 "
                  "human_questions.md 渲染面")
            gates_mod.render_human_questions(ws, ledger)
            return 1
        if applied:
            g2 = ledger.find(args.gate_id)
            print(f"[porter] gate: ✔ 已应用（{g2.get('resolution', '')}"
                  f"[:200]）" if g2 else "已应用")
        return 0

    if args.gate_cmd == "review":
        cp = args.cp or "REVIEW"
        p = gates_mod.checkpoint_digest(ws, cp, ledger)
        _log.console_line(f"[porter] gate: 批审材料 → {p}")
        gates_mod.render_human_questions(ws, ledger)
        _log.console_line("[porter] gate: 逐条否决：answers.md `## @<债项ID>` "
              "verdict: veto；批量放行：`## @cp.debt.<n>` verdict: approve")
        return 0
    return 2


def cmd_log(args) -> int:
    """log CLI（观测查询面：tail/show/timeline/runs——docs/sub-systems/log.md §查询）。

    全部为 events.jsonl 的派生读；debug / resume 定位 / agent 运行考古
    的统一入口。
    """
    from porter import log as log_mod
    ws = Path(args.output_dir).resolve()
    if not (ws / "events.jsonl").exists():
        _log.console_line(f"[porter] log: 工作区无 events.jsonl（{ws}）——尚未记录")
        return 1

    if args.log_cmd == "tail":
        evs = log_mod.query.events(ws, kind_prefix=args.kind,
                                   subject=args.subject,
                                   phase=args.phase, module=args.module,
                                   limit=args.n)
        if not evs:
            _log.console_line("[porter] log: 无匹配事件")
            return 0
        for e in evs:
            bits = [str(e.get("time") or "?")[:19],
                    str(e.get("kind") or "?"),
                    str(e.get("phase") or e.get("mount") or "")]
            if e.get("module"):
                bits.append(str(e["module"]))
            if e.get("subject"):
                bits.append(str(e["subject"]))
            bits.append(str(e.get("summary") or "")[:100])
            print("  ".join(b for b in bits if b))
        _log.console_line(f"[porter] log: 共 {len(evs)} 条（events.jsonl）")
        return 0

    if args.log_cmd == "runs":
        rs = log_mod.query.runs(ws, subject=args.subject,
                                last_n=args.n)
        if not rs:
            _log.console_line("[porter] log: 无 agent 运行记录")
            return 0
        for r in rs:
            rc = "运行中" if r["rc"] is None else f"rc={r['rc']}"
            dur = f"{r['duration_sec']:.0f}s" if r.get("duration_sec") \
                is not None else "?"
            print(f"{r['run_id']}\n    {rc} {dur} "
                  f"{str(r.get('summary') or '')[:80]}")
        _log.console_line(f"[porter] log: 共 {len(rs)} 次 agent 运行（show <run_id> 看全文）")
        return 0

    if args.log_cmd == "show":
        rs = log_mod.query.runs(ws, last_n=10 ** 6)
        hit = next((r for r in rs if r["run_id"] == args.run_id), None) \
            or next((r for r in rs if r["run_id"].endswith(args.run_id)),
                    None)
        if hit is None:
            _log.console_line(f"[porter] log: run 不存在: {args.run_id}（用 runs 列出）")
            return 2
        print(json.dumps({k: v for k, v in hit.items()}, ensure_ascii=False,
                         indent=2))
        log_p = ws / str(hit.get("log") or "")
        if log_p.is_file():
            print(f"\n----- {log_p} 尾 {args.n} 行 -----")
            print("\n".join(log_p.read_text(
                encoding="utf-8", errors="replace")
                .splitlines()[-args.n:]))
        if hit.get("prompt"):
            pp = ws / str(hit["prompt"])
            if pp.is_file():
                print(f"\n----- 输入归档 {pp}（头 20 行）-----")
                print("\n".join(pp.read_text(
                    encoding="utf-8", errors="replace")
                    .splitlines()[:20]))
        return 0

    if args.log_cmd == "timeline":
        rows = log_mod.query.timeline(ws, module=args.module,
                                      limit=args.n)
        if not rows:
            _log.console_line("[porter] log: 无事件")
            return 0
        for t in rows:
            print(f"{str(t['time'] or '?')[:19]}  "
                  f"{str(t['kind'] or '?'):<16} "
                  f"{str(t['phase'] or ''):<6} "
                  f"{str(t['subject'] or ''):<24} "
                  f"{str(t['summary'] or '')[:80]}")
        return 0
    return 2


def cmd_vcs(args) -> int:
    """vcs CLI：可移植导出（bundle 集）/ 导入（commit 链接回 git 仓）。"""
    from porter.common import vcs as _vcs
    if args.vcs_cmd == "export":
        if not args.output_dir:
            _log.console_line("[porter] vcs: export 需 --output-dir 指向迁移工作区")
            return 2
        ws = Path(args.output_dir).resolve()
        if not (ws / "project.json").exists():
            _log.console_line(f"[porter] 工作区不存在：{ws}（先跑 p0）")
            return 2
        m = _vcs.export_all(ws, out_dir=Path(args.out) if args.out else None)
        if not m:
            _log.console_line("[porter] vcs: 无可导出（vcs 未启用或未登记仓）")
            return 1
        for f in m["files"]:
            print(f"  - {f['bundle']}（{f['kind']}，baseline "
                  f"{str(f['baseline'])[:12] or '全量'}）")
        _log.console_line(f"[porter] vcs: 导出完成 → {m['dir']}/manifest.json"
              f"（分支 {m['branch'] or '—'}）")
        return 0
    # import
    if not args.bundle or not args.repo:
        _log.console_line("[porter] vcs: import 需 --bundle FILE --repo PATH"
              "（可选 --branch NAME）")
        return 2
    ok, detail = _vcs.import_bundle(Path(args.bundle), Path(args.repo),
                                    branch=args.branch)
    _log.console_line(f"[porter] vcs: {'✔' if ok else '✖'} {detail}")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="porter",
        description="driver_migration_tool — Linux→任意目标OS 驱动迁移工具")
    sub = ap.add_subparsers(dest="phase", required=True)
    # 路由配置护栏（S4）：启动即校验，坏配置警告但不阻塞（回落内置默认）
    try:
        from porter.loop import routing as _routing
        _warns = _routing.validate_routing(_routing.load_routing())
        for _w in _warns:
            _log.console_line(f"[porter] ⚠️ routing 配置: {_w}")
    except Exception:
        pass

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
    p0.add_argument("--kb", nargs=2, default=None, metavar=("MODE", "NAME"),
                    help="知识库目录（必填）：new <名> 新建（缺省复制 base，"
                         "--kb-empty 建空目录）或 use <名> 指定既有；"
                         "已记录 kb_dir 的工作区复用记录值")
    p0.add_argument("--kb-empty", action="store_true",
                    help="--kb new 时创建空目录（缺省复制 base 内容）")
    p0.add_argument("--kb-git", choices=["track", "ignore"], default="track",
                    help="（已退役，兼容保留）旧全局布局的 git 策略；"
                         "知识库现随工作区 git 统一入库")
    p0.add_argument("--os-branch", default=None, metavar="BRANCH",
                    help="目标 OS 仓的 porter 分支名（必须是全新分支；"
                         "缺省自动生成 porter/<驱动>-<日期>-<随机>；"
                         "工作区仓同分支名）")
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

    p1p = sub.add_parser("p1-promote", help="样例草稿晋升：temp → 本次知识库目录（沉淀，P1 完成后人工决定执行）")
    p1p.add_argument("--output-dir", required=True, help="迁移工作区根目录（解析本次知识库目录 kb_dir）")
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

    p2p = sub.add_parser("p2-promote", help="映射知识晋升：temp/maps → 知识库目录/maps（P2 末/循环中人工决定执行）")
    p2p.add_argument("--output-dir", required=True, help="迁移工作区根目录（解析本次知识库目录 kb_dir）")
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
    p6cmd.add_argument("--defect-fix", default=None, metavar="ID",
                       help="缺陷修复（S5）：升级报告→agent 有界修码→build+boot 验证→"
                            "四字段自动闭账→CP4 批审")
    p6cmd.add_argument("--draft-l4", action="store_true",
                       help="L4 判据草案生成（deferred/__P6__+P3 e2e → agent 起草；"
                            "人审入口 = --finalize-l4 / CP3）")
    p6cmd.set_defaults(func=cmd_p6)

    kbcmd = sub.add_parser("kb", help="知识库审核/分类/晋升（随机知识后段；CP5 备审材料见 checkpoints/CP5_knowledge.md）")
    kbcmd.add_argument("--output-dir", required=True, help="迁移工作区根目录")
    kbcmd.add_argument("--list", action="store_true",
                       help="列出候选 + 刷新 CP5 备审材料（默认动作）")
    kbcmd.add_argument("--classify", action="store_true",
                       help="agent 批量归类候选（审核后、晋升前；PORTER_NO_AGENT=1 跳过）")
    kbcmd.add_argument("--promote", choices=["all"], default=None,
                       help="晋升候选入知识库目录（配合 --id 或缺省全部）")
    kbcmd.add_argument("--reject", default=None, metavar="ID",
                       help="拒绝候选（人判无价值，出账）")
    kbcmd.add_argument("--id", action="append", dest="ids", default=None,
                       metavar="ID", help="指定候选 id（可多次；classify/promote 用）")
    kbcmd.add_argument("--to", default=None, metavar="DOMAIN",
                       help="晋升目标子目录（缺省取建议类；覆盖时条目内留改判记录）")
    kbcmd.set_defaults(func=cmd_kb)

    gatecmd = sub.add_parser("gate", help="关口 CLI（list/show/answer/review——人工介入便利层）")
    gatecmd.add_argument("--output-dir", required=True, help="迁移工作区根目录")
    gatecmd.add_argument("gate_cmd", choices=["list", "show", "answer", "review"],
                         help="list=清单 / show=详情 / answer=作答 / review=批审材料")
    gatecmd.add_argument("gate_id", nargs="?", default=None,
                         help="（show/answer）关口 id")
    gatecmd.add_argument("--status", default="open",
                         choices=["open", "debt", "all"],
                         help="（list）过滤：open 待答 / debt 决策债 / all")
    gatecmd.add_argument("--set", action="append", default=None, metavar="F=V",
                         help="（answer）字段赋值，可多次（如 --set verdict=approve）")
    gatecmd.add_argument("--cp", default=None,
                         help="（review）检查点名（digest 文件名，缺省 REVIEW）")
    gatecmd.set_defaults(func=cmd_gate)

    logcmd = sub.add_parser("log", help="log 子系统查询面（tail/runs/show/timeline——debug 与 resume 定位）")
    logcmd.add_argument("--output-dir", required=True, help="迁移工作区根目录")
    logcmd.add_argument("log_cmd", choices=["tail", "runs", "show", "timeline"],
                        help="tail=事件尾随 / runs=agent 运行登记 / "
                             "show=运行详情+日志 / timeline=浓缩时间线")
    logcmd.add_argument("run_id", nargs="?", default=None,
                        help="（show）run id（runs 列出的 run_id，可用尾部匹配）")
    logcmd.add_argument("--kind", default=None, help="（tail）kind 前缀过滤")
    logcmd.add_argument("--subject", default=None, help="subject 前缀过滤")
    logcmd.add_argument("--module", default=None, help="module 过滤")
    logcmd.add_argument("--phase", default=None,
                        help="（tail）相位过滤（p0..p7/loop/d1）")
    logcmd.add_argument("-n", type=int, default=50,
                        help="条数（tail/timeline 缺省 50；show 的日志尾行数）")
    logcmd.set_defaults(func=cmd_log)

    vcsgrp = sub.add_parser("vcs", help="git 管理（export=可移植导出 bundle / import=导入 commit 链）")
    vcsgrp.add_argument("--output-dir", default=None,
                        help="迁移工作区（export 必填）")
    vcsgrp.add_argument("vcs_cmd", choices=["export", "import"],
                        help="export=导出可移植 bundle 集 / import=把 bundle 接回 git 仓")
    vcsgrp.add_argument("--out", default=None, metavar="DIR",
                        help="（export）导出目录（缺省 <ws>/exports/）")
    vcsgrp.add_argument("--bundle", default=None, metavar="FILE",
                        help="（import）bundle 文件路径")
    vcsgrp.add_argument("--repo", default=None, metavar="PATH",
                        help="（import）目标 git 仓路径")
    vcsgrp.add_argument("--branch", default=None, metavar="NAME",
                        help="（import）导入后创建/切换的分支名")
    vcsgrp.set_defaults(func=cmd_vcs)

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
