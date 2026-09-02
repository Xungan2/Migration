"""extract_spine.py — P2a 主轴 API 提取（纯脚本，零 agent）。

输入：P1/modules/<M>/ 物理切分模块（MVP 全集）。
方法：
  1. 用 common/symbol.scan_file 扫全部模块源文件 → defs/refs
  2. external = union(refs) − union(defs)   # 驱动自有符号已剔除
  3. 定位 Linux 内核树根（自 driver_root 逐级向上找 include/linux），
     单遍扫描内核头文件建立 标识符→头文件 倒排索引
  4. 每个外部符号定域：优先命中"驱动实际 include 的头文件"，其次任意
     头文件（include/linux > include/net > uapi > 其他），无命中入
     unresolved（P3(M) 人工/agent 兜底）
输出：P2/reports/spine_api.json（幂等：存在即跳过）。

已知取舍：
- 倒排索引按"词出现"而非"声明语法"，跨头同名会多候选——定域只求
  分批连贯，不求精确归属；映射条目以 agent 在目标 OS 树核实为准。
- 模块物理切分已过 P1D/P1R 审定，其 refs 集即该驱动的内核 API 使用面。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..common.symbol import _IDENT, _KEYWORDS, scan_file
from .. import log as _log

_IDENT_RE = _IDENT  # 复用 symbol.py 的标识符正则与关键字表

# 头文件优先级（定域用）：路径前缀 → 权重，小者优
_HEADER_PRIORITY = [
    ("include/linux/", 0),
    ("include/net/", 1),
    ("include/asm-generic/", 2),
    ("include/uapi/", 3),
]


def _find_kernel_root(driver_root: Path) -> Path | None:
    """自 driver_root 逐级向上找含 include/linux 的目录。"""
    for d in [driver_root, *driver_root.parents]:
        if (d / "include" / "linux").is_dir():
            return d
    return None


def _scan_includes(path: Path) -> set[str]:
    """提取文件的 #include 目标（<> 形态，保留原样如 linux/pci.h）。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r'#include\s*<([^>]+)>', text))


def _header_index(kernel_root: Path) -> dict[str, list[str]]:
    """单遍扫描内核头文件 → {标识符: [头文件相对路径]}。

    只扫 .h；跳过明显的非 API 目录（tools/scripts/documentation）。
    头文件相对 kernel_root 记路径（如 linux/pci.h、net/devlink.h）。
    """
    index: dict[str, set[str]] = {}
    include_root = kernel_root / "include"
    n_files = 0
    for h in include_root.rglob("*.h"):
        rel = h.relative_to(include_root).as_posix()
        if any(seg in rel for seg in ("tools/", "scripts/", "documentation/")):
            continue
        try:
            text = h.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # 头文件体量小且语法简单：直接标识符全集入倒排（无符号语义）
        for m in _IDENT_RE.findall(text):
            index.setdefault(m, set()).add(rel)
        n_files += 1
    _log.console_line(f"[porter] P2a: 内核头文件索引完成——{n_files} 个头文件, "
          f"{len(index)} 个标识符")
    return {k: sorted(v) for k, v in index.items()}


def _domain_of(sym: str, hdr_idx: dict[str, list[str]],
               driver_includes: set[str]) -> str | None:
    """为外部符号定域。返回头文件路径（域键）或 None。"""
    cands = hdr_idx.get(sym)
    if not cands:
        return None
    incl_hits = [c for c in cands if c in driver_includes]
    pool = incl_hits or cands
    def prio(c: str) -> tuple[int, int]:
        for prefix, w in _HEADER_PRIORITY:
            if c.startswith(prefix):
                return (w, len(c.split("/")))
        return (9, len(c.split("/")))
    return sorted(pool, key=prio)[0]


def _orig_driver_defs(driver_root: Path) -> set[str]:
    """原始（未切分）驱动源文件的定义集——用于区分"内部裁剪噪声"。

    P1 物理切分会 TRIM 裁剪块：定义被删、其他模块对它的引用还在。
    这类符号既非内核 API 也非待映射对象，须从外部面剔除：
    在原始树有定义而在模块切分中无定义 → internal_cut。
    """
    defs: set[str] = set()
    for f in sorted(driver_root.glob("*.c")) + sorted(driver_root.glob("*.h")):
        d, _r, _p = scan_file(f)
        defs |= set(d)
    return defs


def run_extract(ws: Path, driver_root: Path) -> int:
    """返回 0=成功；1=失败；2=前置缺失。幂等：产物存在即跳过。"""
    p1_modules = ws / "P1" / "modules"
    deps_path = p1_modules / "deps.json"
    if not deps_path.exists():
        _log.console_line(f"[porter] P2a: 缺少 {deps_path}（先跑 p1）")
        return 2
    p2 = ws / "P2"
    out_path = p2 / "reports" / "spine_api.json"
    if out_path.exists():
        _log.console_line(f"[porter] P2a: 复用 {out_path}（如需重做请删除该文件）")
        return 0

    module_dirs = sorted(d for d in p1_modules.iterdir()
                         if d.is_dir() and (d / "module.json").exists())
    if not module_dirs:
        _log.console_line(f"[porter] P2a: {p1_modules} 下无模块目录——失败")
        return 2

    all_defs: set[str] = set()
    all_refs: set[str] = set()
    driver_includes: set[str] = set()
    mod_of_include: dict[str, set[str]] = {}
    for mdir in module_dirs:
        for f in sorted(mdir.glob("*")):
            if f.suffix not in (".c", ".h"):
                continue
            defs, refs, _protos = scan_file(f)
            all_defs |= set(defs)
            all_refs |= refs
            for inc in _scan_includes(f):
                driver_includes.add(inc)
                mod_of_include.setdefault(inc, set()).add(mdir.name)

    external = sorted(s for s in (all_refs - all_defs)
                      if s not in _KEYWORDS and len(s) >= 3)
    orig_defs = _orig_driver_defs(driver_root)
    kernel_root = _find_kernel_root(driver_root)
    if kernel_root is None:
        _log.console_line("[porter] P2a: 未定位到 Linux 内核树根"
              f"（自 {driver_root} 向上无 include/linux）——失败")
        return 2
    hdr_idx = _header_index(kernel_root)

    domains: dict[str, dict] = {}
    unresolved: list[str] = []
    internal_cut: list[str] = []
    paste_fragments: list[str] = []
    # 宏拼接碎片检测：`E1000_##reg` 习语使源码出现裸 EECD/CTRL_EXT 等段，
    # 真身是 E1000_EECD（已在 defs）。orig_defs 按末段 '_' 建尾段集匹配。
    orig_tails = {d.rsplit("_", 1)[1] for d in orig_defs if "_" in d}
    for sym in external:
        dom = _domain_of(sym, hdr_idx, driver_includes)
        if dom is not None:
            domains.setdefault(dom, {"symbols": [], "driver_included": False})
            domains[dom]["symbols"].append(sym)
        elif sym.endswith("_") or sym in orig_tails:
            paste_fragments.append(sym)
        elif sym in orig_defs:
            # 原始树有定义、模块切分无定义 = P1 裁剪产物（定义被 TRIM、
            # 引用残留）——非映射对象，报告留档供与裁剪清单核销
            internal_cut.append(sym)
        else:
            unresolved.append(sym)
    for dom in domains:
        domains[dom]["driver_included"] = dom in driver_includes
        domains[dom]["symbols"].sort()
        domains[dom]["included_by_modules"] = sorted(
            mod_of_include.get(dom, set()))

    out = {
        "kernel_root": str(kernel_root),
        "driver_root": str(driver_root),
        "modules": [d.name for d in module_dirs],
        "domains": domains,
        "unresolved": unresolved,
        "internal_cut": internal_cut,
        "paste_fragments": paste_fragments,
        "stats": {
            "external_symbols": len(external),
            "resolved": sum(len(d["symbols"]) for d in domains.values()),
            "internal_cut": len(internal_cut),
            "paste_fragments": len(paste_fragments),
            "unresolved": len(unresolved),
            "domains": len(domains),
        },
    }
    p2.mkdir(parents=True, exist_ok=True)
    (p2 / "reports").mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    st = out["stats"]
    _log.console_line(f"[porter] P2a: 主轴 API 提取完成——外部符号 {st['external_symbols']}"
          f"（resolved {st['resolved']} / 裁剪残留 {st['internal_cut']}"
          f" / 拼接碎片 {st['paste_fragments']}"
          f" / 未解析 {st['unresolved']}），{st['domains']} 个域 → {out_path}")
    return 0
