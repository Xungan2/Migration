"""resolve.py — P1 依赖解环编排（skills/P1-resolve.md 的执行器）。

流程：
  ① 符号扫描（symbol.py v2）→ 依赖图 → 环检测
  ② 无环 → 拓扑序落盘 modules/deps.json → 结束
  ③ 有环 → agent 修复轮（SKILL + 环报告 + 相关片段 [+ 历史搬运]）：
     agent 输出 moves JSON → 机器校验（片段存在 + 守恒）→ 应用到 plan →
     重新抽取 → 回 ①
  超上限（MAX_ROUNDS）→ 人工升级（环报告 + 历史）→ exit 3

不变量的机器证明：每轮应用前后，把片段展开为单行后的 (src, 行号)
多重集完全一致——普通搬运与 split 拆分都保持行集不变（零删改）。
"""

from __future__ import annotations

import json
import re
import shutil
from collections import Counter
from pathlib import Path

from ..common import agent
from . import fragments as frag_mod
from .symbol import scan_module_dir

MAX_ROUNDS = 3

# 拆分策略注入模板（路径+导读，不注入策略正文；{strategy_path} 代入实路径）
STRATEGY_PROMPT_PATH = Path(__file__).parent / "resolve_strategy_prompt.md"

_RANGE_RE = re.compile(r"\s*(\d+)\s*-\s*(\d+)\s*")


def _parse_range(spec: str) -> tuple[int, int] | None:
    """'250-299' -> (250, 299)；非法返回 None。1-based 含端点。"""
    m = _RANGE_RE.fullmatch(spec or "")
    if not m:
        return None
    a, b = int(m.group(1)), int(m.group(2))
    if a < 1 or b < a:
        return None
    return a, b


# ---------- 依赖图 ----------

def _build_graph(ws: Path) -> dict:
    mods_root = ws / "P1" / "modules"
    mod_dirs = sorted(d for d in mods_root.iterdir()
                      if d.is_dir() and (d / "module.json").exists())
    defs: dict[str, dict[str, list[str]]] = {}
    refs: dict[str, set[str]] = {}
    for d in mod_dirs:
        defs[d.name], refs[d.name] = scan_module_dir(d)

    # 所有权：优先 .c 定义（函数体所在），其次 .h
    owner: dict[str, str] = {}
    dup: dict[str, list[str]] = {}
    for m in sorted(defs):
        for sym, locs in sorted(defs[m].items()):
            if sym in owner:
                dup.setdefault(sym, [owner[sym]]).append(m)
            else:
                owner[sym] = m
    # 修正：同名时把所有权给含 .c 定义的一方
    for sym, mods_ in dup.items():
        c_mods = [m for m in mods_ + [owner[sym]]
                  if any(l.endswith(".c:" + l.split(".c:")[-1]) or ".c:" in l
                         for l in defs.get(m, {}).get(sym, []))]
        if c_mods:
            owner[sym] = sorted(c_mods)[0]

    edges: dict[str, dict[str, list[str]]] = {}
    for m, rs in refs.items():
        for sym in sorted(rs):
            t = owner.get(sym)
            if t and t != m:
                edges.setdefault(m, {}).setdefault(t, []).append(sym)
    return {"defs": defs, "refs": refs, "owner": owner,
            "edges": edges, "dup": dup}


def _find_cycles(edges: dict[str, dict[str, list[str]]]) -> list[list[str]]:
    graph = {m: set(ts) for m, ts in edges.items()}
    nodes = set(graph)
    for m in list(graph):
        nodes |= graph[m]
    color = {n: 0 for n in nodes}
    stack: list[str] = []
    cycles: list[list[str]] = []

    def dfs(u: str):
        color[u] = 1
        stack.append(u)
        for v in sorted(graph.get(u, ())):
            if color.get(v, 0) == 0:
                dfs(v)
            elif color.get(v) == 1:
                i = stack.index(v)
                nodes = stack[i:]
                k = nodes.index(min(nodes))
                canon = nodes[k:] + nodes[:k] + [nodes[k]]
                if canon not in cycles:
                    cycles.append(canon)
        stack.pop()
        color[u] = 2

    for n in sorted(nodes):
        if color[n] == 0:
            dfs(n)
    return cycles


def _topo_order(edges: dict[str, dict[str, list[str]]]) -> list[str]:
    nodes = set(edges)
    for ts in edges.values():
        nodes |= set(ts)
    indeg = {n: 0 for n in nodes}
    dependents: dict[str, set[str]] = {n: set() for n in nodes}
    for m, ts in edges.items():
        for t in ts:
            if t != m:
                indeg[m] += 1
                dependents[t].add(m)
    ready = sorted(n for n, k in indeg.items() if k == 0)
    order: list[str] = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for d in sorted(dependents[n]):
            indeg[d] -= 1
            if indeg[d] == 0:
                ready.append(d)
        ready.sort()
    return order


# ---------- 报告与 prompt ----------

def _fragment_inventory(plan: dict) -> list[str]:
    inv = []
    for m in plan.get("modules", []):
        for f in m.get("files", []):
            for fr in f.get("fragments", []):
                inv.append((f["src"], fr["lines"], m["name"],
                            f["dest"], fr.get("symbol", "")))
    return inv


def _make_report(cycles: list[list[str]], edges: dict, plan: dict,
                 dup: dict) -> str:
    on_cycle: set[str] = set()
    for c in cycles:
        on_cycle |= set(c)
    lines = ["## 环报告", ""]
    for i, c in enumerate(cycles, 1):
        lines.append(f"环 {i}: " + " → ".join(c))
        for a, b in zip(c[:-1], c[1:]):
            syms = edges.get(a, {}).get(b, [])
            lines.append(f"  边 {a} → {b} 由 {syms} 产生")
    lines += ["", "## 环上模块的片段（搬运只能引用这些及 plan 中其他真实片段）", ""]
    for src, ls, mod, dest, sym in _fragment_inventory(plan):
        if mod in on_cycle:
            lines.append(f"{mod}/{dest} ← {src}:{ls}"
                         f"{f' ({sym[:40]})' if sym else ''}")
    if dup:
        lines += ["", "## 重复定义符号（供参考；所有权已按 .c 定义侧归属）", ""]
        for sym, mods_ in list(dup.items())[:20]:
            lines.append(f"- {sym}: {mods_}")
    return "\n".join(lines)


def _prompt(skill: str, report: str, round_no: int,
            history: list[dict], strategy_block: str) -> str:
    p = [skill, "", "---", "", report]
    if strategy_block:
        p += ["", strategy_block]
    if round_no > 1:
        p += ["", "## 你此前各轮的搬运历史（勿回搬）", "",
              json.dumps(history, ensure_ascii=False, indent=1)]
    p += ["", "请按 SKILL 输出搬运清单（只输出一个 JSON 块）。"]
    return "\n".join(p)


# ---------- 搬运校验与应用 ----------

def _apply_moves(plan: dict, moves: list[dict]) -> tuple[dict, list[str]]:
    """校验并应用搬运（含 split 拆分型）。返回 (新 plan, 错误清单)。

    普通搬运：{"src","lines","from","to"[,"dest_file"]}
    拆分搬运：额外带 "split": [{"lines":"a-b"[,"to","dest_file"]}, ...]——
    子区间必须**恰好覆盖**原片段（升序、连续、无缝隙/重叠）；无 "to"
    的子块留在原模块，带 "to" 的搬往目标模块。守恒由调用方按行集复核。
    """
    errs: list[str] = []
    # 建 (src, lines) -> (module_idx, file_idx, frag_idx) 索引
    idx: dict[tuple[str, str], tuple[int, int, int]] = {}
    mods = plan.get("modules", [])
    for mi, m in enumerate(mods):
        for fi, f in enumerate(m.get("files", [])):
            for gi, fr in enumerate(f.get("fragments", [])):
                key = (f["src"], fr["lines"])
                if key in idx:
                    errs.append(f"plan 内片段重复: {key}")
                idx[key] = (mi, fi, gi)

    pending = []
    for mv in moves:
        key = (mv.get("src", ""), mv.get("lines", ""))
        loc = idx.get(key)
        if not loc:
            errs.append(f"片段不存在: {mv.get('src')}:{mv.get('lines')}"
                        f"（from={mv.get('from')}）——必须逐字符引用 plan")
            continue
        mi, fi, gi = loc
        if mods[mi]["name"] != mv.get("from"):
            errs.append(f"片段 {key} 属于 {mods[mi]['name']}，"
                        f"move 声明 from={mv.get('from')}")
            continue
        if "split" in mv:
            rng = _parse_range(mv.get("lines", ""))
            if rng is None:
                errs.append(f"split 原片段 lines 非法: {mv.get('lines')}")
                continue
            parts = mv.get("split")
            if not isinstance(parts, list) or len(parts) < 2:
                errs.append(f"split 至少需要 2 个子区间: {key}")
                continue
            pranges = []
            bad = False
            for pt in parts:
                r = _parse_range(pt.get("lines", ""))
                if r is None:
                    errs.append(f"split 子区间非法: {pt.get('lines')!r}")
                    bad = True
                    break
                pranges.append((r, pt))
            if bad:
                continue
            if pranges[0][0][0] != rng[0]:
                errs.append(f"split 首段须从原片段起点 {rng[0]} 开始: {key}")
                continue
            for i in range(1, len(pranges)):
                if pranges[i][0][0] != pranges[i - 1][0][1] + 1:
                    errs.append(f"split 子区间不连续（缝隙/重叠于 "
                                f"{pranges[i-1][0][1]} 与 {pranges[i][0][0]} 之间）: {key}")
                    bad = True
                    break
            if bad:
                continue
            if pranges[-1][0][1] != rng[1]:
                errs.append(f"split 末段须止于原片段终点 {rng[1]}: {key}")
                continue
            pending.append((mi, fi, gi, mv, pranges))
        else:
            pending.append((mi, fi, gi, mv, None))
    if errs:
        return plan, errs

    # 按 (mi,fi,gi) 倒序摘除，避免索引位移；
    # split 的留守子块随即插回原处（倒序下插回不影响更小索引）
    import copy
    new_plan = copy.deepcopy(plan)
    taken = []   # (spec, fragment, src)：spec 提供 to/dest_file
    for mi, fi, gi, mv, pranges in sorted(pending,
                                          key=lambda x: (x[0], x[1], x[2]),
                                          reverse=True):
        f = new_plan["modules"][mi]["files"][fi]
        fr = f["fragments"].pop(gi)
        src = f["src"]
        if pranges is None:
            taken.append((mv, fr, src))
        else:
            note = f"（拆自 {mv['lines']}）"
            insert_at = gi
            for r, pt in pranges:
                sub = {"lines": f"{r[0]}-{r[1]}",
                       "symbol": fr.get("symbol", "") + note}
                if pt.get("to"):
                    taken.append((pt, sub, src))
                else:
                    f["fragments"].insert(insert_at, sub)
                    insert_at += 1
        if not f["fragments"]:
            new_plan["modules"][mi]["files"].pop(fi)
    # 目标模块追加
    to_map = {m["name"]: m for m in new_plan["modules"]}
    for spec, fr, src in taken:
        to = spec.get("to", "")
        if to not in to_map:
            errs.append(f"目标模块不存在: {to}")
            continue
        dest = spec.get("dest_file")
        tm = to_map[to]
        entry = None
        if dest:
            for f in tm["files"]:
                if f["dest"] == dest and f["src"] == src:
                    entry = f
                    break
        else:
            # 沿用 from 侧同名 dest（同 src 约束下）
            for f in tm["files"]:
                if f["src"] == src:
                    entry = f
                    break
        if entry is None:
            tm["files"].append({"dest": dest or f"moved_{len(tm['files'])}.c",
                                "src": src, "fragments": [fr]})
        else:
            if entry["src"] != src:
                errs.append(f"dest 文件 {entry['dest']} 已绑定其他 src"
                            f"（{entry['src']} ≠ {src}），需另给 dest_file")
                continue
            entry["fragments"].append(fr)
    return new_plan, errs


def _conservation(plan_a: dict, plan_b: dict) -> bool:
    """行级守恒：片段全部展开为单行后，(src, 行号) 多重集完全一致。

    普通搬运与拆分搬运都保持行集不变；任何丢失/重复/篡改都会破坏守恒。
    """
    def inv(p):
        c: Counter = Counter()
        for m in p.get("modules", []):
            for f in m.get("files", []):
                for fr in f.get("fragments", []):
                    r = _parse_range(fr.get("lines", ""))
                    if r:
                        for ln in range(r[0], r[1] + 1):
                            c[(f["src"], ln)] += 1
                    else:  # 非法 lines 兜底按原串计（不应发生）
                        c[(f["src"], fr.get("lines", ""))] += 1
        return c
    return inv(plan_a) == inv(plan_b)


# ---------- 主入口 ----------

def run_resolve(ws: Path, driver_root: Path,
                strategy_path: Path | None = None) -> int:
    """返回 0=成功（deps.json 落盘）；3=需人工；1=失败。"""
    p1 = ws / "P1"
    plan_path = p1 / "reports" / "P1D_plan.json"
    if not plan_path.exists():
        print(f"[porter] P1R: 缺少 {plan_path}（先跑 p1-divide）")
        return 2
    (p1 / "logs").mkdir(parents=True, exist_ok=True)

    spath = strategy_path or (p1 / "strategy.md")
    if spath.exists():
        tpl = STRATEGY_PROMPT_PATH.read_text(encoding="utf-8")
        strategy_block = tpl.replace("{strategy_path}", str(spath.resolve()))
    else:
        print(f"[porter] P1R: 未找到策略文件 {spath}，prompt 不注入策略导读")
        strategy_block = ""

    skill = agent.load_skill("P1-resolve")
    history: list[dict] = []

    for rnd in range(1, MAX_ROUNDS + 1):
        g = _build_graph(ws)
        cycles = _find_cycles(g["edges"])
        print(f"[porter] P1R: 第 {rnd} 次扫描——{len(g['edges'])} 个模块有出边，"
              f"{len(cycles)} 个环，重复符号 {len(g['dup'])}")
        if not cycles:
            order = _topo_order(g["edges"])
            deps = {
                "modules": sorted(g["edges"]),
                "edges": {m: sorted(ts) for m, ts in g["edges"].items()},
                "edge_symbols": {m: {t: sorted(s) for t, s in ts.items()}
                                 for m, ts in g["edges"].items()},
                "order": order,
                "cycles": [],
                "duplicate_symbols": g["dup"],
            }
            (p1 / "modules" / "deps.json").write_text(
                json.dumps(deps, ensure_ascii=False, indent=2),
                encoding="utf-8")
            print(f"[porter] P1R: 无环 ✅ 拓扑序落盘 P1/modules/deps.json")
            print(f"[porter] P1R: 迁移序: {' → '.join(order)}")
            if g["dup"]:
                print(f"[porter] P1R: ⚠️ {len(g['dup'])} 个重复定义符号"
                      f"（所有权按 .c 侧，详见 deps.json）")
            return 0

        report = _make_report(cycles, g["edges"],
                              json.loads(plan_path.read_text(encoding="utf-8")),
                              g["dup"])
        (p1 / "reports" / f"P1R_report_R{rnd}.md").write_text(
            report, encoding="utf-8")
        rc, out = agent.run_agent(
            _prompt(skill, report, rnd, history, strategy_block), workdir=p1,
            log_stem=str(p1 / "logs" / f"P1R_R{rnd}"), timeout_sec=3600)
        parsed = agent.extract_json(out) if rc == 0 else None
        moves = (parsed or {}).get("moves")
        if moves is None:
            print(f"[porter] P1R: 第 {rnd} 轮输出无法解析为 moves JSON"
                  f"（见 P1/logs/P1R_R{rnd}.log）")
            continue

        old_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        new_plan, errs = _apply_moves(old_plan, moves)
        if errs:
            print(f"[porter] P1R: 第 {rnd} 轮搬运校验未过：")
            for e in errs:
                print(f"  - {e}")
            continue
        if not _conservation(old_plan, new_plan):
            print(f"[porter] P1R: 第 {rnd} 轮守恒校验失败（片段集变化）——丢弃")
            continue
        # 保存轮次审计 + 应用
        (p1 / "reports" / f"P1D_plan_R{rnd}.json").write_text(
            json.dumps(new_plan, ensure_ascii=False, indent=2),
            encoding="utf-8")
        plan_path.write_text(json.dumps(new_plan, ensure_ascii=False,
                                        indent=2), encoding="utf-8")
        try:
            frag_mod.extract_modules(ws, driver_root, new_plan)
        except frag_mod.DivideError as e:
            print(f"[porter] P1R: 搬运后重抽取失败（回滚本轮 plan）：\n{e}")
            plan_path.write_text(json.dumps(old_plan, ensure_ascii=False,
                                            indent=2), encoding="utf-8")
            frag_mod.extract_modules(ws, driver_root, old_plan)
            continue
        history.append({"round": rnd, "moves": moves})
        print(f"[porter] P1R: 第 {rnd} 轮应用 {len(moves)} 条搬运，"
              f"重新抽取完成")

    # 人工升级
    g = _build_graph(ws)
    cycles = _find_cycles(g["edges"])
    q = ["# P1 依赖解环：人工介入（自动修复未果）", "",
         f"agent 已尝试 {MAX_ROUNDS} 轮，剩余 {len(cycles)} 个环：", ""]
    q += [f"- {' → '.join(c)}" for c in cycles[:10]]
    q += ["", "历史搬运与各轮报告见 P1/reports/（P1R_report_R*.md、P1D_plan_R*.json）。",
          "处理：人工编辑 P1/reports/P1D_plan.json（直接调整片段归属，保持片段",
          "守恒）后重跑 `python3 porter/main.py p1-resolve --output-dir <项目>`。"]
    (p1 / "reports" / "human_questions.md").write_text("\n".join(q),
                                                       encoding="utf-8")
    print(f"[porter] P1R: {MAX_ROUNDS} 轮未解环 → 人工介入（exit 3）")
    return 3
