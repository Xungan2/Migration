"""fragments.py — P1 divide：按拆分方案物理抽取代码片段（纯脚本）。

最小可跑版语义（校验体系后续接回）：
- 致命检查保留（防抽取垃圾）：行号格式/起止非法、src 不存在、区间越界、
  dest 重名（防静默覆盖）
- 覆盖/缝隙/重叠校验暂不执行（策略裁剪的代码不迁=缝隙合法；完备性
  检查后续以"未归属区间报告"形态接回）
- 输出：modules/<name>/（片段文件 + module.json），无 misc

抽取不变量：片段内容 = 原文行区间逐字拷贝；include 块自动复制到每个
抽出的 .c/.h 文件头部（"位置变化"允许范围）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path


class DivideError(Exception):
    """方案缺陷；message 供反馈 agent。"""


_INCLUDE_RE = re.compile(r'^\s*#\s*include\b')


def split_include_block(lines: list[str]) -> tuple[int, list[str]]:
    """返回 (include 块结束行号 0-based, include 块内容)。

    include 块 = 文件头部连续的 #include 行（允许穿插注释/空行）。
    """
    seen_include = False
    i = 0
    while i < len(lines):
        l = lines[i]
        if _INCLUDE_RE.match(l):
            seen_include = True
            i += 1
        elif l.strip() == "" or l.strip().startswith(("/*", "*", "//")):
            i += 1
        else:
            break
    block_end = i if seen_include else 0
    return block_end, lines[:block_end]


def parse_lines_spec(spec: str, total: int) -> tuple[int, int]:
    """'3096-3281' -> (3095, 3281) 0-based 半开区间。"""
    m = re.fullmatch(r"\s*(\d+)\s*-\s*(\d+)\s*", spec)
    if not m:
        raise DivideError(f"lines 格式非法: {spec!r}（应为 '起-止'，1-based 含端点）")
    a, b = int(m.group(1)), int(m.group(2))
    if a < 1 or b < a:
        raise DivideError(f"lines 区间非法: {spec}（要求 1 <= 起 <= 止）")
    if b > total:
        raise DivideError(f"lines 区间越界: {spec}（文件共 {total} 行）")
    return a - 1, b


def extract_modules(ws: Path, driver_root: Path, plan: dict) -> dict:
    """按方案抽取。返回 {模块: {文件: 行数}} 摘要。

    致命检查（抛 DivideError，逐条收集一次抛出）：dest/src 缺失、dest
    重名、src 不存在、区间非法/越界、无片段。覆盖与重叠暂不检查。
    """
    errors: list[str] = []
    modules = plan.get("modules") or []
    if not modules:
        raise DivideError("方案为空：无 modules")

    # ---- 解析与致命检查 ----
    # dest 唯一性按模块内检查：dest=src 文件名时同一 src 必然出现在
    # 多个模块（各写各的 modules/<mod>/ 目录，无覆盖风险）；同一模块内
    # 两条同名 dest + 不同 src 才会静默覆盖，仍禁止。
    entries: list[dict] = []      # {module, dest, src, spans}
    seen_dest: dict[str, dict[str, str]] = {}
    src_cache: dict[str, list[str]] = {}

    for m in modules:
        mname = m.get("name") or "(未命名模块)"
        if not m.get("files"):
            errors.append(f"模块 {mname}: 无 files")
            continue
        for f in m["files"]:
            dest, src = f.get("dest"), f.get("src")
            if not dest or not src:
                errors.append(f"模块 {mname}: file 条目缺 dest/src")
                continue
            if dest in seen_dest.setdefault(mname, {}):
                errors.append(f"模块 {mname}: dest 文件重名: {dest}"
                              f"（已绑定 {seen_dest[mname][dest]}）")
                continue
            seen_dest[mname][dest] = src
            frags = f.get("fragments") or []
            if not frags:
                errors.append(f"{mname}/{dest}: 无 fragments")
                continue
            src_path = driver_root / src
            if not src_path.is_file():
                errors.append(f"src 文件不存在: {src}（被 {mname}/{dest} 引用）")
                continue
            if src not in src_cache:
                src_cache[src] = src_path.read_text(
                    encoding="utf-8", errors="replace").splitlines()
            total = len(src_cache[src])
            spans = []
            for fr in frags:
                try:
                    spans.append(parse_lines_spec(fr.get("lines", ""), total))
                except DivideError as e:
                    errors.append(f"{mname}/{dest}: {e}")
            if spans:
                entries.append({"module": mname, "dest": dest, "src": src,
                                "spans": spans})
    if errors:
        raise DivideError("方案校验失败：\n" + "\n".join(f"- {e}" for e in errors))

    # ---- 抽取写入 ----
    out_root = ws / "P1" / "modules"
    if out_root.exists():
        import shutil
        shutil.rmtree(out_root)
    summary: dict[str, dict[str, int]] = {}
    for e in entries:
        mdir = out_root / e["module"]
        mdir.mkdir(parents=True, exist_ok=True)
        lines = src_cache[e["src"]]
        _, include_block = split_include_block(lines)
        parts = ["\n".join(include_block), ""] if include_block else []
        n = 0
        for s, t in e["spans"]:
            parts.append("\n".join(lines[s:t]))
            n += t - s
        (mdir / e["dest"]).write_text("\n".join(parts) + "\n", encoding="utf-8")
        summary.setdefault(e["module"], {})[e["dest"]] = n

    # ---- module.json ----
    for m in modules:
        mname = m.get("name")
        if not mname or not m.get("files"):
            continue
        meta = {"name": mname, "function": m.get("function", ""),
                "source_map": {f["dest"]: {"src": f["src"],
                             "fragments": [fr.get("lines")
                                           for fr in f.get("fragments", [])]}
                             for f in m["files"] if f.get("dest")}}
        (out_root / mname / "module.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary
