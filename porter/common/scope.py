"""scope.py — 迁移范围闭包（scope.json）的公共读写与校验。

范围声明层（批次 2）：goals.md（用户意图，P0 拷贝入工作区）→ P1-strategy
产出闭包 NL（strategy.md「迁移范围」节）+ scope.json（文件白名单）→
CP1 人审 → 下游按白名单过滤。

不变式（用户定稿）：scope 文件必须全部位于 --linux-driver 目录内——
include/linux 等内核公共头是参考资料（agent 阅读用），永不进迁移对象。

schema（P1D_plan 风格的简化版；分组仅参考，P1D 照常自行划分）：
    {"modules": [{"name": "kebab", "function": "职责",
                  "files": ["a.c", "b.h"]}, …]}
文件并集 = 硬白名单。
"""

from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path

from .. import log as _log

_DRIVER_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def driver_name_of(proj: dict) -> str:
    """驱动身份统一解析：project.json 的 driver_name > 目录名回退。

    身份层（driver_name 由 scope 提案、CP1 人审、同步入 project.json）
    之前，目录名=身份是全工具隐含假设——一个目录住多套体系时（如
    drivers/md 同时是 md-RAID 与 dm 的家）该假设破产。回退保持存量
    工作区/独立校准跑兼容。
    """
    dn = proj.get("driver_name")
    if isinstance(dn, str) and dn.strip():
        return dn.strip()
    return Path(proj["linux_driver"]).name


def load_driver_name(ws: Path) -> str | None:
    """读 <ws>/P1/scope.json 的 driver_name；无/缺 → None。"""
    p = Path(ws) / "P1" / "scope.json"
    if not p.exists():
        return None
    try:
        dn = json.loads(p.read_text(encoding="utf-8")).get("driver_name")
    except (OSError, json.JSONDecodeError):
        return None
    return dn.strip() if isinstance(dn, str) and dn.strip() else None


def sync_driver_name(ws: Path) -> bool:
    """scope.json 的 driver_name → project.json（幂等）。

    生成侧（run_strategy）与 CP1 放行侧各调一次——人工编辑 scope.json
    改身份后经 CP1 重批，project.json 跟随。返回是否发生了写入。
    """
    ws = Path(ws)
    dn = load_driver_name(ws)
    if dn is None:
        return False
    proj_path = ws / "project.json"
    try:
        proj = json.loads(proj_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if proj.get("driver_name") == dn:
        return False
    proj["driver_name"] = dn
    proj_path.write_text(
        json.dumps(proj, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    _log.console_line(f"[porter] scope: driver_name 已同步入 project.json"
                      f"（{dn}）")
    return True


class ScopeError(ValueError):
    """A declared migration scope is missing, invalid, or stale."""


def driver_files(driver_root: Path, files: set[str] | None = None) -> list[Path]:
    """Source inventory, with paths relative to the driver root throughout."""
    paths = ((driver_root / f for f in files) if files is not None
             else driver_root.rglob("*"))
    return sorted((p for p in paths if p.is_file() and p.suffix in (".c", ".h")),
                  key=lambda p: p.relative_to(driver_root).as_posix())


def split_strategy_output(text: str) -> tuple[str, dict | None]:
    """分离策略正文与 scope JSON 块。

    规则：取**最后一个**能解析为 dict 且含 "modules" 键的 ```json 围栏块
    作为 scope 抽出（从正文中移除）；无合规块 → (原文, None)。
    """
    pat = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)
    for m in reversed(list(pat.finditer(text))):
        try:
            parsed = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("modules"), list):
            md = pat.sub("", text).rstrip() + "\n"
            return md, parsed
    return text, None


def scope_files(scope: dict) -> set[str]:
    """scope 的文件并集（相对驱动目录的路径字符串）。"""
    files: set[str] = set()
    for mod in scope.get("modules") or []:
        if isinstance(mod, dict):
            for f in mod.get("files") or []:
                if isinstance(f, str) and f.strip():
                    files.add(f.strip())
    return files


def load_scope(ws: Path, driver_root: Path | None = None) -> set[str] | None:
    """Only legacy unscoped workspaces return None; invalid scope stops work."""
    p = Path(ws) / "P1" / "scope.json"
    proj_path = Path(ws) / "project.json"
    proj = json.loads(proj_path.read_text(encoding="utf-8")) \
        if proj_path.exists() else {}
    if driver_root is None and proj.get("linux_driver"):
        driver_root = Path(proj["linux_driver"])
    if not p.exists():
        if (Path(ws) / "goals.md").exists() or proj.get("intent_file"):
            raise ScopeError("迁移意图已声明，但 P1/scope.json 缺失；先运行 p1-strategy")
        return None
    try:
        scope = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ScopeError(f"P1/scope.json 无法读取：{e}") from e
    clean, defects = _normalize(scope, driver_root)
    if defects:
        raise ScopeError("P1/scope.json 非法：" + "；".join(defects))
    return scope_files(clean)


def _normalize(scope: dict, driver_root: Path | None, *,
               require_driver_name: bool = False) -> tuple[dict, list[str]]:
    """Normalize functional metadata and file paths without changing artifacts."""
    defects: list[str] = []
    if not isinstance(scope, dict):
        return {}, ["scope 顶层须为对象"]
    mods = scope.get("modules")
    if not isinstance(mods, list) or not mods:
        return {}, ["modules 缺失或为空"]

    dn = scope.get("driver_name")
    if require_driver_name and (not isinstance(dn, str) or not dn.strip()):
        defects.append("driver_name 缺失或为空（驱动身份，从意图提炼——"
                       "见 skills/P1-strategy.md「迁移意图与范围闭包」）")
    elif dn is not None and (not isinstance(dn, str) or
                             not _DRIVER_NAME_RE.match(dn.strip())):
        defects.append(f"driver_name 须为 kebab-case（小写字母数字与连字符）: {dn!r}")

    clean: list[dict] = []
    for i, mod in enumerate(mods):
        if not isinstance(mod, dict):
            defects.append(f"modules[{i}] 不是对象")
            continue
        name = mod.get("name")
        if not isinstance(name, str) or not name.strip():
            defects.append(f"modules[{i}].name 缺失或为空")
            continue
        files = mod.get("files")
        if not isinstance(files, list) or not files:
            defects.append(f"模块 {name}: files 缺失或为空")
            continue
        seen: set[str] = set()
        for f in files:
            if not isinstance(f, str) or not f.strip():
                defects.append(f"模块 {name}: files 含非字符串/空项")
                continue
            rel = f.strip()
            if Path(rel).is_absolute() or ".." in Path(rel).parts:
                defects.append(f"模块 {name}: 文件越出驱动目录 {rel}"
                               "（公共头是参考资料，不进迁移对象）")
                continue
            rel = Path(rel).as_posix()
            if Path(rel).suffix not in (".c", ".h"):
                defects.append(f"模块 {name}: 迁移文件须为 .c/.h：{rel}")
                continue
            if driver_root is not None:
                resolved = (driver_root / rel).resolve()
                if not resolved.is_relative_to(driver_root.resolve()):
                    defects.append(f"模块 {name}: 文件越出驱动目录 {rel}")
                    continue
                if not resolved.is_file():
                    defects.append(f"模块 {name}: 文件不存在 {rel}")
                    continue
            seen.add(rel)
        if seen:
            clean.append({"name": name.strip(),
                          "function": str(mod.get("function") or "").strip(),
                          "files": sorted(seen)})

    union = {f for m in clean for f in m["files"]}
    if union and not any(f.endswith(".c") for f in union):
        defects.append("文件并集不含任何 .c 文件")

    clean.sort(key=lambda m: m["name"])
    normalized = {"modules": clean}
    if isinstance(dn, str) and _DRIVER_NAME_RE.match(dn.strip()):
        normalized["driver_name"] = dn.strip()
    if "features" in scope:
        features = scope["features"]
        if not isinstance(features, dict):
            defects.append("features 须为对象（include/exclude/constraints 字符串列表）")
        else:
            for key in ("include", "exclude", "constraints"):
                values = features.get(key, [])
                if not isinstance(values, list) or any(
                        not isinstance(v, str) or not v.strip() for v in values):
                    defects.append(f"features.{key} 须为非空字符串组成的列表")
            if not any(d.startswith("features.") for d in defects):
                normalized["features"] = {
                    k: list(dict.fromkeys(v.strip() for v in features.get(k, [])))
                    for k in ("include", "exclude", "constraints")}
    return normalized, defects


def validate_and_normalize(scope: dict, driver_root: Path, ws: Path) -> list[str]:
    """Validate before publishing; preserve optional functional scope metadata."""
    normalized, defects = _normalize(
        scope, driver_root, require_driver_name=True)
    if defects:
        return defects
    out = ws / "P1" / "scope.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(normalized, ensure_ascii=False, indent=2)
                   + "\n", encoding="utf-8")
    _log.console_line(f"[porter] scope: 已规范化落盘 {out}"
                      f"（driver={normalized.get('driver_name')!r} / "
                      f"{len(normalized['modules'])} 模块 / "
                      f"{len(scope_files(normalized))} 文件白名单）")
    return []


def input_fingerprint(ws: Path, driver_root: Path) -> str:
    """Hash scope decisions and source bytes so resume cannot reuse stale plans."""
    files = load_scope(ws, driver_root)
    h = hashlib.sha256()
    for name in ("goals.md", "P1/strategy.md", "P1/scope.json"):
        p = ws / name
        h.update(name.encode())
        h.update(p.read_bytes() if p.exists() else b"<absent>")
    for p in driver_files(driver_root, files):
        h.update(p.relative_to(driver_root).as_posix().encode())
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def validate_plan_scope(ws: Path, driver_root: Path, plan: dict) -> None:
    files = load_scope(ws, driver_root)
    if files is not None:
        used = {f["src"] for m in plan["modules"] for f in m["files"]}
        outside = used - files
        if outside:
            raise ScopeError("plan 超出已审范围：" + ", ".join(sorted(outside)))


def cross_check(scope_set: set[str], driver_root: Path) -> list[str]:
    """scope 与 build_index 预扫的交叉核对（仅警告，不阻塞）。"""
    warns: list[str] = []
    try:
        from ..divide import index as _idx
        file_index = _idx.build_index(driver_root)
    except Exception as e:                       # 索引失败不挡 scope 本身
        return [f"build_index 预扫失败（跳过交叉核对）：{e}"]
    known = {f for f, entries in file_index.items() if entries}
    ghost = sorted(f for f in scope_set if f not in known)
    if ghost:
        warns.append(f"清单内 {len(ghost)} 文件无定义条目（可能是纯数据/"
                     f"被 include 的头，divide 将无片段可分）：{' '.join(ghost)}")
    excluded = sorted(known - scope_set)
    if excluded:
        warns.append(f"scope 排除目录内 {len(excluded)} 个含定义文件："
                     f"{' '.join(excluded)}")
    return warns
