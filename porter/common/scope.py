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


def load_scope(ws: Path) -> set[str] | None:
    """读 <ws>/P1/scope.json → 文件集合；不存在/不可解析 → None（=全目录）。"""
    p = Path(ws) / "P1" / "scope.json"
    if not p.exists():
        return None
    try:
        scope = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    files = scope_files(scope)
    return files or None


def validate_and_normalize(scope: dict, driver_root: Path, ws: Path) -> list[str]:
    """校验 scope 并把规范化版本写回 <ws>/P1/scope.json。

    返回缺陷清单（空 = 合法且已回写规范化版）。非法时**不落盘**——
    避免坏白名单进入下游；调用方决定如何呈现。
    """
    defects: list[str] = []
    mods = scope.get("modules")
    if not isinstance(mods, list) or not mods:
        return ["modules 缺失或为空"]

    dn = scope.get("driver_name")
    if not isinstance(dn, str) or not dn.strip():
        defects.append("driver_name 缺失或为空（驱动身份，从意图提炼——"
                       "见 skills/P1-strategy.md「迁移意图与范围闭包」）")
    elif not _DRIVER_NAME_RE.match(dn.strip()):
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
            resolved = (driver_root / rel).resolve()
            try:
                resolved.relative_to(driver_root.resolve())
            except ValueError:
                defects.append(f"模块 {name}: 文件越出驱动目录 {rel}"
                               "（公共头是参考资料，不进迁移对象）")
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

    if defects:
        return defects

    clean.sort(key=lambda m: m["name"])
    out = ws / "P1" / "scope.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    normalized = {"modules": clean}
    if isinstance(dn, str) and _DRIVER_NAME_RE.match(dn.strip()):
        normalized["driver_name"] = dn.strip()
    out.write_text(json.dumps(normalized, ensure_ascii=False, indent=2)
                   + "\n", encoding="utf-8")
    _log.console_line(f"[porter] scope: 已规范化落盘 {out}"
                      f"（driver={normalized.get('driver_name')!r}"
                      f" / {len(clean)} 模块 / {len(union)} 文件白名单）")
    return []


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
