"""import_p.py — P1 外部交付物导入（p1-import 执行器，纯脚本无 agent）。

消费外部 P1 三件套交付物（strategy.md / P1D_plan.json / deps.json），把
工具外部完成的拆分成果重建为工作区原生 P1 状态：

    plan      → P1/reports/P1D_plan.json（规范位）+ P1/modules/（物理重建）
    deps      → 仅对账用（重算为准；外部版不落盘）
    strategy  → P1/strategy.md（CP1 审对象）

机器复核哲学——不信任外部产物（A7：tool-p1-adaptation-plan §A7）：
  1. plan schema 校验（形状；深度校验复用 extract_modules 致命检查）
  2. fragments.extract_modules 重建 modules/（先读 --deps 再抽取——
     抽取会 rmtree 整个 modules/，外部 deps.json 若已放规范位会被删）
  3. resolve._build_graph 重算 → 环必须 0（有环不写 deps.json）
  4. deps 对账：modules/edges/order 三项归一化比较，不一致以重算为准
     落盘并 exit 1
  5. strategy.md 就位（缺失仅告警，不失败）
  幂等：重复导入即重建，结果一致。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import fragments as frag_mod
from . import resolve as resolve_mod
from .. import log as _log


def _validate_plan_schema(plan) -> str | None:
    """plan 轻量形状校验；返回错误信息或 None（深度校验在 extract_modules）。"""
    if not isinstance(plan, dict):
        return "plan 顶层需为对象 {\"modules\": [...]}"
    mods = plan.get("modules")
    if not isinstance(mods, list) or not mods:
        return "plan 顶层需 {\"modules\": [非空列表]}"
    for i, m in enumerate(mods):
        if not isinstance(m, dict) or not isinstance(m.get("name"), str) \
                or not m["name"]:
            return f"modules[{i}] 需有非空 name"
        files = m.get("files")
        if not isinstance(files, list) or not files:
            return f"模块 {m['name']}: files 需为非空列表"
        for j, f in enumerate(files):
            if not isinstance(f, dict):
                return f"模块 {m['name']} files[{j}] 需为对象"
            if not isinstance(f.get("dest"), str) or not f["dest"]:
                return f"模块 {m['name']} files[{j}] 缺 dest"
            if not isinstance(f.get("src"), str) or not f["src"]:
                return f"模块 {m['name']} files[{j}] 缺 src"
            frags = f.get("fragments")
            if not isinstance(frags, list) or not frags:
                return f"模块 {m['name']} files[{j}] fragments 需为非空列表"
            for k, fr in enumerate(frags):
                if not isinstance(fr, dict) \
                        or not isinstance(fr.get("lines"), str) or not fr["lines"]:
                    return f"模块 {m['name']} files[{j}] fragments[{k}] 需有 lines 字符串"
    return None


def _load_json(path: Path, what: str):
    """读取 JSON 文件；失败返回 (None, 错误信息)。"""
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except OSError as e:
        return None, f"{what} 读取失败: {e}"
    except json.JSONDecodeError as e:
        return None, f"{what} 解析失败: {e}"


def _norm_deps(d: dict) -> dict:
    """deps 归一化（容错缺失键）：modules/edges 集合化 + order 原样。"""
    edges = d.get("edges") or {}
    return {"modules": set(d.get("modules") or []),
            "edges": {m: set(ts or []) for m, ts in edges.items()},
            "order": list(d.get("order") or [])}


def _diff_deps(ext: dict, recomp: dict) -> list[str]:
    """外部 deps vs 重算 deps 的差异清单（空 = 一致）。"""
    a, b = _norm_deps(ext), _norm_deps(recomp)
    diffs: list[str] = []
    if a["modules"] != b["modules"]:
        diffs.append(f"modules 集合不一致（外部多: {sorted(a['modules'] - b['modules'])}"
                     f" / 重算多: {sorted(b['modules'] - a['modules'])}）")
    for m in sorted(set(a["edges"]) | set(b["edges"])):
        sa, sb = a["edges"].get(m, set()), b["edges"].get(m, set())
        if sa != sb:
            diffs.append(f"edges[{m}] 不一致（外部: {sorted(sa)} / 重算: {sorted(sb)}）")
    if a["order"] != b["order"]:
        diffs.append(f"order 不一致（外部: {a['order']} / 重算: {b['order']}）")
    return diffs


def run_import(ws: Path, driver_root: Path, plan_path: Path,
               deps_path: Path | None = None,
               strategy_path: Path | None = None) -> int:
    """返回 0=成功；2=前置缺失/schema 不合规；1=复核失败（抽取缺陷/环/对账不一致）。"""
    p1 = ws / "P1"

    # ---- 1. plan 读取 + schema ----
    if not plan_path.is_file():
        _log.console_line(f"[porter] P1I: plan 文件不存在: {plan_path}")
        return 2
    plan, err = _load_json(plan_path, "plan")
    if err:
        _log.console_line(f"[porter] P1I: {err}")
        return 2
    err = _validate_plan_schema(plan)
    if err:
        _log.console_line(f"[porter] P1I: plan schema 不合规: {err}")
        return 2

    # ---- 2. 外部 deps 先读进内存（extract_modules 会 rmtree 整个 modules/）----
    ext_deps = None
    if deps_path is not None:
        if not deps_path.is_file():
            _log.console_line(f"[porter] P1I: --deps 文件不存在: {deps_path}")
            return 2
        ext_deps, err = _load_json(deps_path, "deps")
        if err:
            _log.console_line(f"[porter] P1I: {err}")
            return 2

    # ---- 3. plan 落规范位（幂等覆盖）+ 物理重建 modules/ ----
    (p1 / "reports").mkdir(parents=True, exist_ok=True)
    canon = p1 / "reports" / "P1D_plan.json"
    canon.write_text(json.dumps(plan, ensure_ascii=False, indent=2),
                     encoding="utf-8")
    _log.console_line(f"[porter] P1I: plan 就位 {canon}（{len(plan['modules'])} 个模块）")
    try:
        summary = frag_mod.extract_modules(ws, driver_root, plan)
    except frag_mod.DivideError as e:
        _log.console_line(f"[porter] P1I: plan 深度校验/抽取失败（方案致命缺陷）：\n{e}")
        return 1
    total = sum(n for fm in summary.values() for n in fm.values())
    _log.console_line(f"[porter] P1I: 抽取完成——{len(summary)} 个模块 {total} 行"
          f" → P1/modules/")

    # ---- 4. 图重算 → 环守卫 ----
    g = resolve_mod._build_graph(ws)
    cycles = resolve_mod._find_cycles(g["edges"])
    if cycles:
        for c in cycles[:10]:
            _log.console_line(f"[porter] P1I: 环: {' → '.join(c)}")
        _log.console_line(f"[porter] P1I: {len(cycles)} 个环——拒绝导入"
              f"（deps.json 未写；请先在源工作区解环后重新交付）")
        return 1

    # ---- 5. 重算 deps 落盘（真值，schema 与 p1-resolve 一致）----
    order = resolve_mod._topo_order(g["edges"])
    deps = {
        "modules": sorted(g["edges"]),
        "edges": {m: sorted(ts) for m, ts in g["edges"].items()},
        "edge_symbols": {m: {t: sorted(s) for t, s in ts.items()}
                         for m, ts in g["edges"].items()},
        "order": order,
        "cycles": [],
        "duplicate_symbols": g["dup"],
    }
    deps_out = p1 / "modules" / "deps.json"
    deps_out.write_text(json.dumps(deps, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    _log.console_line(f"[porter] P1I: 重算 deps 落盘 {deps_out}")
    _log.console_line(f"[porter] P1I: 迁移序: {' → '.join(order)}")

    # ---- 6. deps 对账（以重算为准）----
    rc = 0
    if ext_deps is not None:
        diffs = _diff_deps(ext_deps, deps)
        if diffs:
            rc = 1
            _log.console_line("[porter] P1I: ⚠️ 外部 deps 与重算不一致"
                  "（以重算为准，已落盘；差异：）")
            for d in diffs:
                print(f"  - {d}")
        else:
            _log.console_line("[porter] P1I: 外部 deps 对账一致 ✅")
    if g["dup"]:
        _log.console_line(f"[porter] P1I: ⚠️ {len(g['dup'])} 个重复定义符号"
              f"（所有权按 .c 侧，详见 deps.json）")

    # ---- 7. strategy 就位（CP1 审对象；缺失仅告警）----
    if strategy_path is not None:
        if not strategy_path.is_file():
            _log.console_line(f"[porter] P1I: ⚠️ --strategy 文件不存在: "
                  f"{strategy_path}（跳过）")
        else:
            p1.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(strategy_path, p1 / "strategy.md")
            _log.console_line(f"[porter] P1I: strategy 就位 {p1 / 'strategy.md'}")
    elif not (p1 / "strategy.md").exists():
        _log.console_line("[porter] P1I: ⚠️ 未提供 --strategy 且 P1/strategy.md"
              " 不存在（CP1 拆分审对象缺失——建议补齐）")
    return rc
