"""index.py — P1 divide：定义索引 + 归属强制 + 机械展开（纯脚本）。

三个职责（设计文档 ~/.local/share/opencode/plans/divide-refactor.md §2）：
1. build_index：grep 扩充模式逐行扫描驱动 *.c/*.h，解析出每文件有序
   定义清单（起始行 / 类型 / 符号名 / [被引用符号]）。类型：
   func/var/struct/enum/union/typedef/define = 可分配；
   reg（注册/元数据宏）/ fwd（前向声明）/ chunk（未解析名）= 机器归属。
2. 归属规则机器强制：注册宏行 → 被引用符号的模块（被裁则随裁）；
   前向声明 → 被声明函数的模块；chunk → 相邻可分配条目的模块。
3. expand：合并各文件分配 → 逐 (模块, src) 聚合片段（dest = src 文件名）
   → 产出与旧版完全相同 schema 的 P1D_plan.json + 审计信息。

解析用正则即可，不追求编译器级精确——索引是给 agent 看的事实清单；
解析不出的命中行降级为 chunk（随相邻条目归属，不产生缝隙）。
注释扩展：定义起点向上覆盖紧邻连续注释行（kernel 文档注释随函数走）；
行间空隙进前一片段尾部，无害。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# ---------- 命中模式（plan §2.1 + 实测补充 typedef/union/unsigned/long/ ----------
# irqreturn_t/netdev_tx_t：plan 模式漏掉 hw.h 顶层 typedef enum/union 定义）

_HIT_RE = re.compile(
    r"^(?:#define\b|module_\w+\s*\(|MODULE_\w+\s*\(|EXPORT_\w+\s*\(|"
    r"DEFINE_\w+\s*\(|static\b|const\b|enum\b|struct\b|typedef\b|union\b|"
    r"s8\b|s16\b|s32\b|s64\b|u8\b|u16\b|u32\b|u64\b|int\b|void\b|char\b|"
    r"bool\b|unsigned\b|long\b|irqreturn_t\b|netdev_tx_t\b)"
)

_REG_RE = re.compile(r"^(module_\w+|MODULE_\w+|EXPORT_\w+)\s*\(")
_DEFN_MACRO_RE = re.compile(r"^(DEFINE_\w+)\s*\(")
_DEFINE_RE = re.compile(r"\s*#\s*define\s+([A-Za-z_]\w*)")
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')
_COMMENT_RE = re.compile(
    r"^\s*(/\*.*?\*/|/\*.*|\*.*\*/|\*.*|//.*)$")

# 名称/引用提取时过滤的修饰符与类型词
_NOISE = {
    "static", "const", "volatile", "inline", "extern", "unsigned", "signed",
    "auto", "register", "typedef", "struct", "union", "enum",
    "int", "char", "bool", "long", "short", "void", "float", "double",
    "u8", "u16", "u32", "u64", "s8", "s16", "s32", "s64", "uint", "ulong",
    "ushort", "size_t", "true", "false", "NULL", "perm", "mode_t",
    "irqreturn_t", "netdev_tx_t", "__le16", "__le32", "__le64",
    "__be16", "__be32", "__be64",
    # kernel 属性注解（可夹在名字与 = ; ( [ 之间，不得抢占符号名）
    "__read_mostly", "__init", "__exit", "__maybe_unused",
    "__always_unused", "__used", "__aligned", "__packed", "__weak",
    "__percpu", "__rcu", "__cold", "__section", "__attribute__",
}


@dataclass
class Entry:
    line: int                     # 1-based 定义/命中行
    kind: str                     # func|var|struct|enum|union|typedef|define|fwd|reg|chunk
    symbol: str                   # 符号名（reg=宏名；chunk=空）
    refs: list[str] = field(default_factory=list)  # 被引用符号（reg/宏定义型）
    start: int = 0                # 注释扩展后起点（1-based，后填）
    end: int = 0                  # 定义结束行（= 下一 entry.start - 1，后填）


# ---------- 文本工具 ----------

def _strip_strings(s: str) -> str:
    return _STRING_RE.sub('""', s)


def _ref_idents(s: str) -> list[str]:
    """提取引用符号：去字符串后取非类型词标识符，保序去重。"""
    out: list[str] = []
    for t in _IDENT_RE.findall(s):
        if t not in _NOISE and t not in out:
            out.append(t)
    return out


def _signature(lines: list[str], i: int, limit: int = 8) -> tuple[str, str]:
    """从命中行起拼接文本，直到出现括号外首个 ; { = 之一。
    返回 (text, terminator)。"""
    depth = 0
    parts: list[str] = []
    for j in range(i, min(i + limit, len(lines))):
        s = _strip_strings(lines[j])
        parts.append(s)
        for ch in s:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif depth == 0 and ch in ";{=":
                return " ".join(parts), ch
    return " ".join(parts), ""


def _macro_args(lines: list[str], i: int, limit: int = 8) -> str:
    """取命中行起宏调用最外层括号内的参数文本（跨续行）。"""
    depth = 0
    started = False
    parts: list[str] = []
    for j in range(i, min(i + limit, len(lines))):
        s = _strip_strings(lines[j])
        parts.append(s)
        for ch in s:
            if ch == "(":
                depth += 1
                started = True
            elif ch == ")":
                depth -= 1
        if started and depth <= 0:
            break
    text = " ".join(parts)
    m = re.match(r".*?\((.*)\)\s*;?\s*$", text)
    return m.group(1) if m else text


def _extract_name(text: str) -> str:
    """从签名文本提取被定义名。首个 '(' 早于 = ; { [ 时按函数名取
    （'(' 前最后一个标识符）；否则按变量/类型名取（首个 = ; { [ 之前
    最后一个标识符——防止初始化表达式里的调用名抢位）。空=未解析。"""
    text = _strip_strings(text)
    paren = text.find("(")
    cut_var = -1
    for ch in "=;{[":
        p = text.find(ch)
        if p >= 0 and (cut_var < 0 or p < cut_var):
            cut_var = p
    if paren >= 0 and (cut_var < 0 or paren < cut_var):
        idents = [t for t in _IDENT_RE.findall(text[:paren])
                  if t not in _NOISE]
        return idents[-1] if idents else ""
    cut = cut_var if cut_var >= 0 else len(text)
    idents = [t for t in _IDENT_RE.findall(text[:cut])
              if t not in _NOISE]
    return idents[-1] if idents else ""


# ---------- 分类 ----------

def _classify(lines: list[str], i: int) -> Entry:
    """对命中行（0-based i）分类。解析不出名字 → chunk。"""
    raw = lines[i]
    n = i + 1

    m = _DEFINE_RE.match(raw)
    if m:
        return Entry(line=n, kind="define", symbol=m.group(1))

    m = _REG_RE.match(raw.strip())
    if m:
        return Entry(line=n, kind="reg", symbol=m.group(1),
                     refs=_ref_idents(_macro_args(lines, i)))

    m = _DEFN_MACRO_RE.match(raw.strip())
    if m:  # DEFINE_*：定义首参符号（如 DEFINE_MUTEX），可分配
        idents = _ref_idents(_macro_args(lines, i))
        return Entry(line=n, kind="var",
                     symbol=idents[0] if idents else "",
                     refs=idents[1:])

    text, term = _signature(lines, i)
    name = _extract_name(text)
    if not name:
        return Entry(line=n, kind="chunk", symbol="")

    # 大写宏名打头的调用（static SIMPLE_DEV_PM_OPS(x, a, b); 等）：
    # 定义首参符号，其余参数为引用提示
    if name.isupper() and text.find("(") >= 0:
        idents = _ref_idents(_macro_args(lines, i))
        if idents and idents[0] != name:
            return Entry(line=n, kind="var", symbol=idents[0],
                         refs=idents[1:])
        return Entry(line=n, kind="chunk", symbol="")

    # 类型块：struct/union/enum X {
    m2 = re.match(
        r"^(?:static\s+|const\s+)*(struct|union|enum)\s+([A-Za-z_]\w*)\s*\{",
        text.strip())
    if m2 and term == "{":
        return Entry(line=n, kind=m2.group(1), symbol=m2.group(2))

    if "(" in text and name and _ident_before_paren(text, name):
        kind = "fwd" if term == ";" else "func"
        return Entry(line=n, kind=kind, symbol=name)
    return Entry(line=n, kind="var", symbol=name)


def _ident_before_paren(text: str, name: str) -> bool:
    """确认 name 确实位于首个 '(' 之前（而非参数里的名字）。"""
    paren = _strip_strings(text).find("(")
    return paren >= 0 and name in _strip_strings(text)[:paren]


# ---------- 注释扩展 ----------

def _extend_up(lines: list[str], hit: int, floor: int) -> int:
    """从命中行向上扩展覆盖紧邻连续注释行。返回 0-based 起点。
    floor = 前一命中行 0-based（不得越过）。"""
    k = hit - 1
    while k > floor and _COMMENT_RE.match(lines[k]):
        k -= 1
    return k + 1


# ---------- 索引构建 ----------

def build_index(driver_root: Path) -> dict[str, list[Entry]]:
    """扫描 driver_root（平铺目录）下全部 *.c/*.h，返回 {文件名: [Entry]}。
    Entry 按行序 tile 整个文件（start/end 无缝衔接）。"""
    files = sorted(driver_root.glob("*.c")) + sorted(driver_root.glob("*.h"))
    index: dict[str, list[Entry]] = {}
    for f in files:
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        entries: list[Entry] = []
        prev_hit = -1
        for i, l in enumerate(lines):
            if not _HIT_RE.match(l):
                continue
            e = _classify(lines, i)
            if entries and e.line <= entries[-1].line:
                continue  # 防御
            e.start = _extend_up(lines, i, prev_hit) + 1  # 1-based
            prev_hit = i
            entries.append(e)
        for a, b in zip(entries, entries[1:]):
            a.end = b.start - 1
        if entries:
            entries[-1].end = len(lines)
        index[f.name] = entries
    return index


def call_order(file_index: dict[str, list[Entry]]) -> list[str]:
    """agent 调用顺序：先 .c 后 .h（各自 sorted）；跳过零条目文件。"""
    cs = sorted(f for f in file_index if f.endswith(".c") and file_index[f])
    hs = sorted(f for f in file_index if f.endswith(".h") and file_index[f])
    return cs + hs


def assignable_symbols(entries: list[Entry]) -> list[str]:
    """该文件需 agent 分配的符号（去重保序）。"""
    seen: list[str] = []
    for e in entries:
        if e.kind in ("reg", "fwd", "chunk") or not e.symbol:
            continue
        if e.symbol not in seen:
            seen.append(e.symbol)
    return seen


# ---------- 切片渲染 ----------

_KIND_LEGEND = (
    "类型图例：func=函数定义 var=变量/宏定义的结构 struct/enum/union/typedef=类型块\n"
    "         define=宏  ← 以上为**可分配**条目\n"
    "         fwd=前向声明 reg=注册/元数据宏 chunk=未解析名 ← 机器归属，**不要**分配")


def render_slice(fname: str, entries: list[Entry], total: int) -> str:
    out = [f"## 文件索引：{fname}（共 {total} 行，{len(entries)} 个条目）", "",
           _KIND_LEGEND, ""]
    for e in entries:
        sym = e.symbol if e.symbol else "(未解析名)"
        if e.kind == "reg":
            refs = ",".join(e.refs) if e.refs else "无符号引用"
            out.append(f"L{e.line:<5} {e.kind:<6} {sym} -> [{refs}]")
        elif e.refs:
            out.append(f"L{e.line:<5} {e.kind:<6} {sym}"
                       f"  (引用: {','.join(e.refs)})")
        else:
            out.append(f"L{e.line:<5} {e.kind:<6} {sym}")
    return "\n".join(out)


# ---------- agent 输出校验 ----------

def validate_decision(entries: list[Entry], parsed) -> str | None:
    """校验单文件 agent 输出。None=通过；否则返回错误文本（重试反馈）。"""
    if not isinstance(parsed, dict):
        return ("输出中未找到可解析的 JSON 对象——须恰好输出一个 ```json 块，"
                "内容为 {\"whole_file\": ...} 或 {\"assignments\": ...}")
    has_wf, has_as = "whole_file" in parsed, "assignments" in parsed
    if has_wf and has_as:
        return "whole_file 与 assignments 不能同时出现（二选一）"
    if has_wf:
        mod = parsed["whole_file"]
        if not isinstance(mod, str) or not mod.strip():
            return "whole_file 的值必须是模块名字符串"
        return None
    if has_as:
        asg = parsed["assignments"]
        if not isinstance(asg, dict):
            return "assignments 必须是 {\"符号\": \"模块名\"|null} 对象"
        missing = [s for s in assignable_symbols(entries) if s not in asg]
        if missing:
            shown = ", ".join(missing[:120])
            more = f" …等共 {len(missing)} 个" if len(missing) > 120 else ""
            return ("assignments 缺以下符号（必须全覆盖；裁剪的显式给 null）：\n"
                    f"  {shown}{more}")
        bad = [s for s, v in asg.items()
               if v is not None
               and not (isinstance(v, str) and v.strip())]
        if bad:
            return (f"以下符号的值非法（须为模块名字符串或 null）："
                    f"{', '.join(bad[:20])}")
        return None
    return "JSON 缺少 whole_file 或 assignments 字段（二选一）"


# ---------- 归属解析 + 机械展开 ----------

def _resolve_file(entries: list[Entry], dec: dict | None,
                  gmap: dict[str, str | None]) -> list[tuple[Entry, str | None]]:
    """解析单文件每条目归属。返回 [(entry, module|None)]，None=不迁。"""
    if dec and "whole_file" in dec:
        return [(e, dec["whole_file"]) for e in entries]
    asg = (dec or {}).get("assignments") or {}
    resolved: list[tuple[Entry, str | None]] = []
    last_mod: str | None = None  # 最近一个可分配条目的归属（含 None=裁）
    for e in entries:
        if e.kind not in ("reg", "fwd", "chunk") and e.symbol:
            mod = asg.get(e.symbol)
            last_mod = mod
            resolved.append((e, mod))
            continue
        if e.kind == "reg":
            # 规则1：注册宏 → 被引用符号的模块；被裁随裁
            hit = [r for r in e.refs if r in gmap]
            if hit:
                resolved.append((e, gmap[hit[0]]))
            else:  # 无符号引用（MODULE_LICENSE 等）→ 前一可分配条目
                resolved.append((e, last_mod))
        elif e.kind == "fwd":
            # 规则2：前向声明 → 被声明函数的模块（可跨文件）
            resolved.append((e, gmap.get(e.symbol, last_mod)))
        else:  # chunk
            resolved.append((e, last_mod))
    # 文件头部机器条目（无先行可分配）→ 跟随其后首个非空归属
    first_known = next((m for _, m in resolved if m is not None), None)
    for k, (e, m) in enumerate(resolved):
        if m is None and e.kind in ("reg", "fwd", "chunk"):
            before_any = all(x.kind in ("reg", "fwd", "chunk")
                             for x, _ in resolved[:k])
            if before_any:
                resolved[k] = (e, first_known)
    return resolved


def expand(file_index: dict[str, list[Entry]], decisions: dict[str, dict],
           module_desc: dict[str, str] | None = None,
           strategy_text: str = "") -> tuple[dict, str]:
    """把分配表机械展开为 plan JSON（schema 与旧版完全一致）+ 审计报告。

    decisions: {文件名: {"whole_file": 模块} |
                          {"assignments": {符号: 模块|null}}}
    返回 (plan_dict, audit_md)。
    """
    module_desc = module_desc or {}

    # 1) 全局 符号→模块（供 fwd/reg 跨文件引用解析；None=裁剪）
    gmap: dict[str, str | None] = {}
    for fname, dec in decisions.items():
        if not dec or "whole_file" in dec:
            continue
        asg = dec.get("assignments") or {}
        for sym in assignable_symbols(file_index.get(fname, [])):
            if sym in asg:
                gmap[sym] = asg[sym]

    # 2) 逐文件解析 → 逐 (模块, src) 聚合片段（相邻同模块合并）
    runs: dict[str, dict[str, list[tuple[int, int, list[str]]]]] = {}
    trimmed: list[tuple[str, str]] = []          # (文件, 符号) 显式 null
    followed: list[str] = []                     # 机器归属说明行
    unassigned: list[str] = []                   # 未分配区间说明行
    no_decision: list[str] = []

    for fname in sorted(file_index):
        entries = file_index[fname]
        if not entries:
            continue
        dec = decisions.get(fname)
        if dec is None:
            no_decision.append(fname)
            dec = {}
        resolved = _resolve_file(entries, dec, gmap)
        explicit = ((dec.get("assignments") or {})
                    if "whole_file" not in dec else {})

        if "whole_file" in dec:
            mod = dec["whole_file"]
            total = entries[-1].end
            runs.setdefault(mod, {}).setdefault(fname, []) \
                .append((1, total, ["(整文件)"]))
            continue

        for e, mod in resolved:
            if (e.symbol and e.kind not in ("reg", "fwd", "chunk")
                    and explicit.get(e.symbol, "x") is None
                    and (fname, e.symbol) not in trimmed):
                trimmed.append((fname, e.symbol))
            if mod is None:
                continue
            if e.kind in ("reg", "fwd"):
                target = (",".join(e.refs) if e.kind == "reg" and e.refs
                          else e.symbol if e.kind == "fwd"
                          else "(邻近可分配条目)")
                followed.append(f"- {fname} L{e.line} {e.kind}"
                                f" {e.symbol} -> {target} → {mod}")
            fl = runs.setdefault(mod, {}).setdefault(fname, [])
            if fl and fl[-1][1] + 1 == e.start:
                s, t, syms = fl[-1]
                fl[-1] = (s, e.end, syms + [e.symbol or "?"])
            else:
                fl.append((e.start, e.end, [e.symbol or "?"]))

        # 未分配区间（含显式裁剪——审计区分标注）
        cur = None
        for e, mod in resolved + [(None, None)]:
            if mod is None and e is not None:
                if cur:
                    cur = (cur[0], e.end)
                else:
                    cur = (e.start, e.end)
            elif cur:
                span = f"L{cur[0]}-L{cur[1]}"
                names = [x.symbol or x.kind for x, m in resolved
                         if m is None and x.start <= cur[1] and x.end >= cur[0]]
                unassigned.append(f"- {fname} {span}（{', '.join(names[:8])}"
                                  f"{'…' if len(names) > 8 else ''}）")
                cur = None

    # 3) plan 组装（dest = src 文件名；模块/文件排序保证确定性）
    modules = []
    for mod in sorted(runs):
        files = []
        for src in sorted(runs[mod]):
            frags = [{"lines": f"{s}-{t}",
                      "symbol": (syms[0] + (f" 等{len(syms)}个符号"
                                            if len(syms) > 1 else ""))}
                     for s, t, syms in runs[mod][src]]
            files.append({"dest": src, "src": src, "fragments": frags})
        modules.append({"name": mod,
                        "function": module_desc.get(mod, mod),
                        "files": files})
    plan = {"modules": modules}

    # 4) 审计报告
    used = sorted({m for m in runs} |
                  {v for d in decisions.values()
                   for v in ([d["whole_file"]] if "whole_file" in d
                             else [x for x in (d.get("assignments")
                                               or {}).values()
                                   if isinstance(x, str) and x])})
    unknown = [m for m in used if m not in strategy_text]
    a = ["# P1 divide 审计报告", "",
         "## 模块名与 strategy.md 比对"]
    if unknown:
        a += [f"- ⚠️ 模块 {m!r} 未在 strategy.md 文本中出现（疑似自创，请人工核对）"
              for m in unknown]
    else:
        a.append("- 全部模块名均在 strategy.md 中出现 ✓")
    a += ["", f"## 显式裁剪符号（null，共 {len(trimmed)} 个）"]
    a += [f"- {f}: {s}" for f, s in trimmed] or ["- （无）"]
    a += ["", "## 机器归属（注册宏/前向声明 → 目标模块）"]
    a += followed[:400] or ["- （无）"]
    if len(followed) > 400:
        a.append(f"- …等共 {len(followed)} 条")
    a += ["", "## 未分配区间（裁剪/未归属，供与策略裁剪计划人工对照）"]
    a += unassigned[:400] or ["- （无）"]
    if len(unassigned) > 400:
        a.append(f"- …等共 {len(unassigned)} 条")
    if no_decision:
        a += ["", "## ⚠️ 无分配决定的文件（未进 plan）",
              *[f"- {f}" for f in no_decision]]
    return plan, "\n".join(a) + "\n"
