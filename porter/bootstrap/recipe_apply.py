"""recipe_apply.py — 施工单（recipe）通用机械层：P2b 框架引导的落地引擎。

契约（OS/语言中立——发现由 agent 做，落地由本层做，证伪归 scaffold）：
- files: [{relpath, content}]     新建文件（仅 driver_home 内允许建目录）
- edits: [{id, file, marker, action, ...}]  幂等编辑，两形态：
    insert:  {insert, group?}   marker 已在文件 → 跳过；group（正则）给定
             → 在匹配行间按字典序插入；无同组行/无 group → 追加文件末尾
    replace: {find, replace}    marker 已在 → 跳过；find 未命中 → ⚠ 记录
- 幂等：marker 是唯一判定——修订编辑内容时换 marker/id，否则引擎跳过
- 回滚：journal 记录每次实际改动（新建文件 / 插入行号 / 替换原文），
  rollback() 精确还原——回炉轮重 apply 前必须先 rollback，防
  "旧内容残留 + 新 marker 跳过"的脏叠加（H 设计：发现闭环的安全网）
- 防崩：目标文件缺失 → ⚠ 记录 skipped，不抛异常（L2 守卫）
- 路径安全：relpath 拒绝绝对路径与 ".." 逃逸
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .. import log as _log

ACTION_TYPES = ("insert", "replace")


def validate_recipe(recipe: dict, driver: str) -> list[str]:
    """施工单轻校验（字段级）。返回缺陷清单（空 = 合法）。"""
    errs: list[str] = []
    if not isinstance(recipe, dict):
        return ["recipe 非对象"]
    for k in ("driver_home", "language", "files", "edits",
              "acceptance_patterns", "probe_channel"):
        if k not in recipe:
            errs.append(f"缺 {k} 节")
    if errs:
        return errs
    if recipe.get("driver") and recipe["driver"] != driver:
        errs.append(f"driver 不符: {recipe['driver']!r} ≠ {driver!r}")
    home = str(recipe.get("driver_home") or "")
    if not home or home.startswith("/") or ".." in home:
        errs.append(f"driver_home 非法: {home!r}")
    if not str(recipe.get("language") or "").strip():
        errs.append("language 为空")
    if not isinstance(recipe.get("files"), list) or not recipe["files"]:
        errs.append("files 为空（骨架至少要有驱动代码文件）")
    for i, f in enumerate(recipe.get("files") or []):
        if not isinstance(f, dict) or not f.get("relpath") \
                or f.get("content") is None:
            errs.append(f"files[{i}] 缺 relpath/content")
            continue
        rp = str(f["relpath"])
        if rp.startswith("/") or ".." in rp:
            errs.append(f"files[{i}].relpath 逃逸: {rp}")
        elif not rp.startswith(home):
            errs.append(f"files[{i}].relpath 不在 driver_home 内: {rp}")
    if not isinstance(recipe.get("acceptance_patterns"), list) \
            or not recipe["acceptance_patterns"]:
        errs.append("acceptance_patterns 为空（三信号验证需要日志特征）")
    for i, e in enumerate(recipe.get("edits") or []):
        if not isinstance(e, dict):
            errs.append(f"edits[{i}] 非对象")
            continue
        for k in ("id", "file", "marker", "action"):
            if not e.get(k):
                errs.append(f"edits[{i}] 缺 {k}")
        if e.get("action") not in ACTION_TYPES:
            errs.append(f"edits[{i}].action 非法: {e.get('action')!r}")
            continue
        if e.get("action") == "insert" and not e.get("insert"):
            errs.append(f"edits[{i}] insert 形态缺 insert 文本")
        if e.get("action") == "replace" and not (e.get("find")
                                                 and e.get("replace") is not None):
            errs.append(f"edits[{i}] replace 形态缺 find/replace")
        fp = str(e.get("file") or "")
        if fp.startswith("/") or ".." in fp:
            errs.append(f"edits[{i}].file 逃逸: {fp}")
        if e.get("action") == "insert" and e.get("group"):
            try:
                re.compile(e["group"])
            except re.error as ex:
                errs.append(f"edits[{i}].group 正则非法: {ex}")
    pc = recipe.get("probe_channel")
    if not isinstance(pc, dict) or not (pc.get("dormitory_rel")
                                        and pc.get("print_idiom")):
        errs.append("probe_channel 缺 dormitory_rel/print_idiom")
    for i, c in enumerate(recipe.get("api_claims") or []):
        if not isinstance(c, dict) or not c.get("linux_api"):
            errs.append(f"api_claims[{i}] 缺 linux_api（批注 join 键）")
    return errs


def _safe_rel(rel: str) -> bool:
    return not rel.startswith("/") and ".." not in rel


def _insert_sorted(lines: list[str], candidate: str,
                   group: str | None) -> tuple[list[str], str]:
    """插入并返回 (新行表, 位置描述)。group 正则匹配行间按字典序插入；
    无 group 或无同组行 → 追加末尾（尾部比旧引擎的 insert(0) 安全）。"""
    if group:
        pat = re.compile(group)
        out: list[str] = []
        inserted = False
        pos = len(lines)
        for i, ln in enumerate(lines):
            if pat.match(ln) and not inserted \
                    and ln.strip() >= candidate.strip():
                out.append(candidate)
                inserted = True
                pos = len(out) - 1
            out.append(ln)
        if not inserted:
            has_group = any(pat.match(ln) for ln in out)
            out.append(candidate)
            pos = len(out) - 1
            return out, ("group-tail" if has_group else "tail")
        return out, f"line-{pos}"
    out = list(lines)
    out.append(candidate)
    return out, "append"


def apply_recipe(target_os: Path, recipe: dict,
                 journal_path: Path | None = None) -> dict:
    """照单施工。返回 {created, edits_applied, skipped, warnings, journal}。

    journal 同时落盘（journal_path 给定时）——供回炉轮 rollback。
    """
    home = str(recipe["driver_home"])
    journal: dict = {"files": [], "edits": []}
    created: list[str] = []
    edits_applied: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []

    for f in recipe.get("files") or []:
        rp = str(f["relpath"])
        path = target_os / rp
        if path.exists():
            continue
        if not rp.startswith(home):
            skipped.append(rp)
            warnings.append(f"拒绝在 driver_home 外建目录: {rp}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(f["content"]), encoding="utf-8")
        created.append(rp)
        journal["files"].append(rp)

    for e in recipe.get("edits") or []:
        eid = str(e.get("id") or "?")
        fp = str(e.get("file") or "")
        marker = str(e.get("marker") or "")
        path = target_os / fp
        if not path.exists():
            skipped.append(f"{eid}@{fp}")
            warnings.append(f"⚠ 编辑目标文件不存在，跳过: {eid} @ {fp}")
            continue
        text = path.read_text(encoding="utf-8")
        if marker and marker in text:
            edits_applied.append(f"{eid}(已在)")     # 幂等命中
            continue
        if e.get("action") == "insert":
            lines = text.splitlines(keepends=True)
            ins = str(e["insert"])
            if not ins.endswith("\n"):
                ins += "\n"
            new, where = _insert_sorted(lines, ins, e.get("group"))
            path.write_text("".join(new), encoding="utf-8")
            edits_applied.append(f"{eid}({where})")
            journal["edits"].append({"id": eid, "file": fp,
                                     "action": "insert",
                                     "inserted": ins, "where": where})
        else:                                   # replace
            find = str(e.get("find") or "")
            if find not in text:
                skipped.append(f"{eid}@{fp}")
                warnings.append(f"⚠ find 未命中（树漂移？），跳过: {eid}"
                                f" @ {fp}")
                continue
            path.write_text(text.replace(find, str(e["replace"]), 1),
                            encoding="utf-8")
            edits_applied.append(f"{eid}(replace)")
            journal["edits"].append({"id": eid, "file": fp,
                                     "action": "replace",
                                     "original": find,
                                     "current": str(e["replace"])})

    if journal_path is not None and (journal["files"] or journal["edits"]):
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        journal_path.write_text(json.dumps(journal, ensure_ascii=False,
                                           indent=2), encoding="utf-8")
    for w in warnings:
        _log.console_line(f"[porter] P2b: {w}")
    return {"created": created, "edits_applied": edits_applied,
            "skipped": skipped, "warnings": warnings, "journal": journal}


def rollback(target_os: Path, journal_path: Path) -> bool:
    """按 journal 精确还原（回炉轮重 apply 前调用）。返回是否有还原动作。"""
    if not journal_path.exists():
        return False
    try:
        j = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    for e in reversed(j.get("edits") or []):
        path = target_os / str(e.get("file") or "")
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        if e.get("action") == "replace":
            new = str(e.get("original") or "")
            # 只回滚 journal 记录的那次替换：把替换文换回原文（一次）
            cur = str(e.get("current") or "")
            if not cur:
                continue
            if cur in text:
                path.write_text(text.replace(cur, new, 1),
                                encoding="utf-8")
        else:                                  # insert
            ins = str(e.get("inserted") or "")
            if ins.endswith("\n"):
                ins = ins[:-1]
            if ins and ins + "\n" in text:
                lines = text.splitlines(keepends=True)
                out = [ln for ln in lines if ln.rstrip("\n") != ins]
                # 只移除一行（防误删同文行）
                removed = False
                out2: list[str] = []
                for ln in lines:
                    if not removed and ln.rstrip("\n") == ins:
                        removed = True
                        continue
                    out2.append(ln)
                path.write_text("".join(out2), encoding="utf-8")
    for rp in reversed(j.get("files") or []):
        path = target_os / str(rp)
        try:
            if path.is_file():
                path.unlink()
                parent = path.parent
                # 剪空目录（止于 target_os，不越界）
                while parent != target_os and parent.is_dir() \
                        and not any(parent.iterdir()):
                    parent.rmdir()
                    parent = parent.parent
        except OSError:
            pass
    try:
        journal_path.unlink()
    except OSError:
        pass
    _log.console_line("[porter] P2b: 上一轮施工已回滚（journal 还原）")
    return True
