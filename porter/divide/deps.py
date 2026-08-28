"""deps.py — 依赖图计算、迁移序、环检测、粒度护栏、报告生成（纯脚本）。

输入：P1/modules/ 目录（fragments.py 产物）
输出：deps.json（边/序/环处理/外部符号清单/护栏结果）+ report.md（人读）
"""

from __future__ import annotations

import json
from pathlib import Path

from .symbol import scan_module_dir


def compute_deps(ws: Path) -> dict:
    mods_root = ws / "P1" / "modules"
    module_dirs = sorted(d for d in mods_root.iterdir()
                         if d.is_dir() and d.name != "misc" and (d / "module.json").exists())
    misc_root = mods_root / "misc"

    # 各模块定义/引用集
    defs: dict[str, dict[str, list[str]]] = {}
    refs: dict[str, set[str]] = {}
    sizes: dict[str, int] = {}
    for d in module_dirs:
        defs[d.name], refs[d.name] = scan_module_dir(d)
        sizes[d.name] = sum(len(f.read_text(errors="replace").splitlines())
                            for f in d.glob("*.c")) + \
                        sum(len(f.read_text(errors="replace").splitlines())
                            for f in d.glob("*.h"))

    # misc 也参与符号解析（可被依赖；它不被迁移但物理上存在）
    misc_defs: dict[str, list[str]] = {}
    if misc_root.exists():
        md, _ = scan_module_dir(misc_root)
        misc_defs = md

    # 符号归属总表
    owner: dict[str, str] = {}
    dup: dict[str, list[str]] = {}
    for mod, dmap in defs.items():
        for sym in dmap:
            if sym in owner:
                dup.setdefault(sym, [owner[sym]]).append(mod)
            else:
                owner[sym] = mod

    # 依赖边：模块 B 引用符号 → 定义属模块 A（≠B，含 misc）→ 边 B->A
    edges: dict[str, set[str]] = {m: set() for m in defs}
    edge_symbols: dict[str, dict[str, list[str]]] = {}  # B -> A -> [符号]
    external: dict[str, list[str]] = {}   # 悬空符号 → 使用它的模块
    for mod, rset in refs.items():
        for sym in rset:
            tgt = owner.get(sym)
            if tgt is None:
                external.setdefault(sym, []).append(mod)
            elif tgt != mod:
                edges[mod].add(tgt)
                edge_symbols.setdefault(mod, {}).setdefault(tgt, []).append(sym)
    # 边符号截断（每边最多 8 个样本，防 deps.json 膨胀）
    edge_symbols = {b: {a: s[:8] for a, s in d.items()}
                    for b, d in edge_symbols.items()}

    # 环检测（DFS）
    def find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
        color: dict[str, int] = {m: 0 for m in graph}
        stack: list[str] = []
        cycles: list[list[str]] = []

        def dfs(u: str):
            color[u] = 1
            stack.append(u)
            for v in sorted(graph.get(u, ())):
                if color.get(v, 0) == 1:
                    i = stack.index(v)
                    cycles.append(stack[i:] + [v])
                elif color.get(v, 0) == 0:
                    dfs(v)
            stack.pop()
            color[u] = 2

        for m in sorted(graph):
            if color[m] == 0:
                dfs(m)
        return cycles

    cycles = find_cycles(edges)

    # 拓扑排序（Kahn；同层启发式：基础先行——名称权重仅为稳定次序，不预设语义）
    # 拓扑排序（Kahn；环模块单列，可排序部分照常输出）
    order: list[str] = []
    indeg: dict[str, int] = {}
    dependents: dict[str, set[str]] = {m: set() for m in edges}
    for m, deps_set in edges.items():
        indeg[m] = len([d for d in deps_set if d in edges])
        for d in deps_set:
            if d in dependents:
                dependents[d].add(m)
    ready = sorted([m for m, k in indeg.items() if k == 0])
    while ready:
        m = ready.pop(0)
        order.append(m)
        for dep in sorted(dependents[m]):
            indeg[dep] -= 1
            if indeg[dep] == 0:
                ready.append(dep)
        ready.sort()
    cyclic_modules = sorted(set(edges) - set(order))
    has_cycle = bool(cyclic_modules)

    # 粒度护栏
    guardrail = {"oversize": {m: n for m, n in sizes.items() if n > 500},
                 "undersize": {m: n for m, n in sizes.items() if n < 50},
                 "sizes": sizes}

    result = {
        "modules": list(defs.keys()),
        "edges": {m: sorted(s) for m, s in edges.items()},
        "edge_symbols": edge_symbols,
        "order": order,
        "cycles": cycles,
        "acyclic": not has_cycle,
        "duplicate_symbols": dup,
        "external_symbols": {k: sorted(set(v)) for k, v in
                             sorted(external.items())},
        "guardrail": guardrail,
    }
    (ws / "P1" / "modules" / "deps.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_report(ws, result)
    return result


def _write_report(ws: Path, r: dict) -> None:
    sizes = r["guardrail"]["sizes"]
    lines = ["# 模块划分报告（脚本生成）", "",
             f"模块数：{len(r['modules'])}（另有 misc）",
             f"依赖无环：{'是 ✅' if r['acyclic'] else '否 ❌（见下方环）'}", "",
             "## 迁移顺序", ""]
    for i, m in enumerate(r["order"], 1):
        deps = ", ".join(r["edges"][m]) or "（无）"
        lines.append(f"{i}. **{m}**（{sizes.get(m, '?')} 行；依赖：{deps}）")
    if r["cycles"]:
        lines += ["", "## ⚠️ 循环依赖（须处理）", ""]
        for c in r["cycles"]:
            lines.append(f"- {' → '.join(c)}")
    if r["guardrail"]["oversize"]:
        lines += ["", "## ⚠️ 超规模模块（>500 行，须拆）", ""]
        for m, n in r["guardrail"]["oversize"].items():
            lines.append(f"- {m}: {n} 行")
    if r["duplicate_symbols"]:
        lines += ["", "## ⚠️ 跨模块重名符号（检查归属唯一性）", ""]
        for sym, mods in list(r["duplicate_symbols"].items())[:20]:
            lines.append(f"- {sym}: {mods}")
    ext = r["external_symbols"]
    lines += ["", f"## 外部符号（内核 API/类型，共 {len(ext)} 项——任务B 原材料）", "",
              ", ".join(sorted(ext)[:80]) + ("..." if len(ext) > 80 else ""), ""]
    (ws / "P1" / "modules" / "report.md").write_text("\n".join(lines), encoding="utf-8")
