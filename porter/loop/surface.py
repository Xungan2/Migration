"""surface.py — P3(M) 使用面提取（纯脚本，plan §3.2）。

M 的外部符号 = refs(M) − defs(M)，四分类：
  1. cross_module   其他模块定义（∩ 全模块 defs）——非 OS API，走 deps 边
  2. mapped         全局映射表已有条目（按 verdict 细分；gap 单列供处置分类）
  3. noise          非映射对象：裁剪残留（原树有定义/切分无）、宏拼接碎片
                    （E1000_##reg 习语的裸段）、纯字段访问位出现（.sym/->sym）
  4. missing        真缺失——P3(M) agent 增量映射的输入（按头文件域分组）

另附 #include 清单与每符号使用位置（file:line，截 5 条）供 agent 上下文。
输出：P3/<M>/reports/surface.json（幂等：存在即复用）。

P2a 提取（extract_spine）是并集视角；本模块面是模块视角——两者互补：
并集 resolved 的符号此处应为 mapped；并集 unresolved 的符号落在
missing/noise（模块视角能借字段访问/裁剪交叉核验收掉大片尾巴）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from ..common.symbol import _IDENT, _clean_source, scan_file
from .. import log as _log

MAX_LOC = 5            # 每符号使用位置上报上限


def _scan_includes(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r"#include\s*<([^>]+)>", text))


def _all_module_defs(p1_modules: Path) -> set[str]:
    defs: set[str] = set()
    for mdir in sorted(p1_modules.iterdir()):
        if not (mdir / "module.json").exists():
            continue
        for f in sorted(mdir.glob("*")):
            if f.suffix in (".c", ".h"):
                d, _r, _p = scan_file(f)
                defs |= set(d)
    return defs


def _orig_defs(driver_root: Path,
               scope: set[str] | None = None) -> set[str]:
    """原始驱动源文件定义集（噪音三分类基线）。

    scope（范围声明层白名单）在场时只扫闭包内文件——否则同目录无关
    体系符号会污染 internal_cut/orig_tails 判定。
    """
    defs: set[str] = set()
    for f in sorted(driver_root.glob("*.c")) + sorted(driver_root.glob("*.h")):
        if scope is not None and f.name not in scope:
            continue
        d, _r, _p = scan_file(f)
        defs |= set(d)
    return defs


def _occurrences(f: Path, ext: set[str]) -> tuple[dict[str, list[str]],
                                                  set[str], set[str]]:
    """单文件三产物：符号使用位置 / 纯字段访问位符号 / 一般位符号。

    字段访问位 = 前驱非空白字符为 '.' 或 '-'（-> 的 '-'）。某符号在文件中
    **全部**出现都在字段访问位 → 归入 field_only（模块级聚合后再判）。
    """
    clean = _clean_source(f.read_text(encoding="utf-8", errors="replace"))
    locs: dict[str, list[str]] = {}
    field_only: set[str] = set()
    general: set[str] = set()
    for ln_no, line in enumerate(clean.splitlines(), 1):
        for m in _IDENT.finditer(line):
            s = m.group(1)
            if s not in ext:
                continue
            locs.setdefault(s, []).append(f"{f.name}:{ln_no}")
            i = m.start() - 1
            while i >= 0 and line[i] in " \t":
                i -= 1
            # 字段访问位：`.sym` 或 `->sym`（前驱是 '.'，或前两字符 '->'；
            # 比较符 '>' 后裸贴标识符在 C 中罕见，可接受）
            if i >= 0 and (line[i] == "." or (i >= 1 and line[i - 1:i + 1]
                                              == "->")):
                field_only.add(s)
            else:
                general.add(s)
    return locs, field_only, general


def _header_index_cached(ws: Path, kernel_root: Path | None) -> dict | None:
    """头文件倒排索引（缺域判定用）。首次构建后缓存 P2/reports/。"""
    cache = ws / "P2" / "reports" / "header_index.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    if kernel_root is None:
        return None
    from ..bootstrap.extract_spine import _header_index
    idx = _header_index(kernel_root)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    return idx


def _domain_map(ws: Path, spine: dict, kernel_root: Path | None) -> dict:
    """符号→域：spine 域优先，缺则头文件索引兜底。"""
    dom: dict[str, str] = {}
    if spine:
        for d, v in (spine.get("domains") or {}).items():
            for s in v.get("symbols") or []:
                dom.setdefault(s, d)
    idx = _header_index_cached(ws, kernel_root)
    if idx:
        driver_includes: set[str] = set()
        p1 = ws / "P1" / "modules"
        for mdir in sorted(p1.iterdir()):
            if not (mdir / "module.json").exists():
                continue
            for f in sorted(mdir.glob("*")):
                if f.suffix in (".c", ".h"):
                    driver_includes |= _scan_includes(f)
        from ..bootstrap.extract_spine import _domain_of
        for s in (spine.get("unresolved") or []):
            d = _domain_of(s, idx, driver_includes)
            if d:
                dom[s] = d
    return dom


def extract_surface(ws: Path, driver_root: Path, module: str,
                    force: bool = False) -> tuple[dict, int]:
    """提取模块使用面。返回 (surface_dict, rc)：0 成功 / 2 前置缺失。

    幂等：surface.json 存在且非 force 时直接复用。
    """
    mdir = ws / "P1" / "modules" / module
    if not (mdir / "module.json").exists():
        _log.console_line(f"[porter] P3: 模块不存在 {mdir}")
        return {}, 2
    out_dir = ws / "P3" / module / "reports"
    out_path = out_dir / "surface.json"
    if out_path.exists() and not force:
        return json.loads(out_path.read_text(encoding="utf-8")), 0

    files = sorted(f for f in mdir.glob("*") if f.suffix in (".c", ".h"))
    if not files:
        _log.console_line(f"[porter] P3: 模块 {module} 无源文件")
        return {}, 2

    defs: set[str] = set()
    refs: set[str] = set()
    includes: set[str] = set()
    locs: dict[str, list[str]] = {}
    field_only: set[str] = set()
    general: set[str] = set()
    for f in files:
        d, r, _p = scan_file(f)
        defs |= set(d)
        refs |= r
        includes |= _scan_includes(f)
    external = {s for s in refs - defs if len(s) >= 3}
    for f in files:
        flocs, ffield, fgen = _occurrences(f, external)
        for s, ll in flocs.items():
            locs.setdefault(s, []).extend(ll)
        field_only |= ffield
        general |= fgen

    alldefs = _all_module_defs(ws / "P1" / "modules")
    from ..common import scope as _scope
    orig = _orig_defs(driver_root, scope=_scope.load_scope(ws))
    spine_path = ws / "P2" / "reports" / "spine_api.json"
    spine = (json.loads(spine_path.read_text(encoding="utf-8"))
             if spine_path.exists() else {})
    mapping = json.loads((ws / "P2" / "mapping.json").read_text(
        encoding="utf-8")) if (ws / "P2" / "mapping.json").exists() \
        else {"entries": []}
    entries = {e["linux_api"]: e for e in mapping.get("entries", [])}

    cross_module = sorted(external & alldefs)
    rest = external - alldefs

    # 噪音三分：裁剪残留 / 拼接碎片 / 纯字段访问
    orig_tails = {d.rsplit("_", 1)[1] for d in orig if "_" in d}
    noise_cut = sorted(s for s in rest if s in orig)
    noise_paste = sorted(s for s in rest - set(noise_cut)
                         if s.endswith("_") or s in orig_tails)
    noise_field = sorted(s for s in rest - set(noise_cut) - set(noise_paste)
                         if s in field_only and s not in general)
    noise = {"internal_cut": noise_cut, "paste_fragments": noise_paste,
             "field_only": noise_field}

    os_api = rest - set(noise_cut) - set(noise_paste) - set(noise_field)
    mapped = sorted(s for s in os_api if s in entries)
    missing = sorted(s for s in os_api if s not in entries)

    dom = _domain_map(ws, spine, _kernel_root(driver_root))
    missing_by_domain: dict[str, list[str]] = {}
    for s in missing:
        missing_by_domain.setdefault(dom.get(s, "unresolved"), []).append(s)

    by_verdict: dict[str, list[str]] = {}
    gap_entries = []
    for s in mapped:
        e = entries[s]
        by_verdict.setdefault(e["verdict"], []).append(s)
        if e["verdict"] == "gap":
            gap_entries.append({"linux_api": s, "target": e.get("target", ""),
                                "notes": e.get("notes", ""),
                                "risk": e.get("risk", ""),
                                "confidence": e.get("confidence", "")})

    surface = {
        "module": module,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "includes": sorted(includes),
        "files": [f.name for f in files],
        "stats": {
            "external": len(external),
            "cross_module": len(cross_module),
            "os_api": len(os_api),
            "mapped": len(mapped),
            "missing": len(missing),
            "noise": sum(len(v) for v in noise.values()),
        },
        "cross_module": cross_module,
        "noise": noise,
        "mapped_by_verdict": by_verdict,
        "gaps": gap_entries,
        "missing_by_domain": missing_by_domain,
        "usage_locations": {s: locs.get(s, [])[:MAX_LOC]
                            for s in [*mapped, *missing]},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(surface, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    st = surface["stats"]
    _log.console_line(f"[porter] P3: {module} 使用面——外部 {st['external']}"
          f"（跨模块 {st['cross_module']} / 已映射 {st['mapped']}"
          f" / 缺失 {st['missing']} / 噪音 {st['noise']}）→ {out_path}")
    return surface, 0


def _kernel_root(driver_root: Path) -> Path | None:
    from ..bootstrap.extract_spine import _find_kernel_root
    return _find_kernel_root(driver_root)
