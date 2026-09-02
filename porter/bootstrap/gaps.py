"""gaps.py — gaps 域收成（gap 处置决策 + fill 成败 → knowledge/temp/gaps）。

粒度：**一个 API 一个文件**（文件名即 API 名——"这个 API 以前 fill
失败过吗" = 查文件名存在性，零内容解析；见 prior_entry）。
命名空间：temp 跨迁移共享 → `temp/gaps/<驱动>@<目标>/<api>.md`，
INDEX 行的 file 字段携带相对路径 `<ns>/<api>.md`。

进料口两个（设计定案）：
  固定收成：P3 各模块 gap_decisions.json（agent 分类 + 人工关口富集
            的 strategy/instruction/evidence/rationale）∪ P4 fill 成败
            （fill.json + platform_patches 的 status/reason）；
  随机候选：fill 失败原因等经审核晋升（探查钩子/CP5 接入）。

收成点：与 maps 同点（p3._refresh_drafts 统一触发；P3 的 exit-3
人工路径也刷新——H18 修复覆盖本域）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import kb
from .. import log as _log

_API_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_api(api: str) -> str:
    """API 名 → 文件名（安全字符外的替换为 _；C 标识符通常不变）。"""
    return _API_SAFE.sub("_", str(api))[:120] or "unnamed"


def _load(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _collect_decisions(ws: Path) -> dict[str, dict]:
    """全部模块的 gap 决策按 api 归并（富字段优先：带 rationale/answered 者
    覆盖同类裸条目；模块序确定性遍历）。"""
    out: dict[str, dict] = {}

    def _rich(d: dict) -> int:
        return (2 if d.get("rationale") else 0) + \
               (1 if d.get("answered") else 0)

    for dec_path in sorted(ws.glob("P3/*/reports/gap_decisions.json")):
        data = _load(dec_path) or {}
        for d in data.get("decisions", []):
            api = d.get("linux_api")
            if not api:
                continue
            cur = out.get(api)
            if cur is None or _rich(d) >= _rich(cur):
                out[api] = d
    return out


def _fill_outcomes(ws: Path) -> dict[str, dict]:
    """fill 成败（fill.json results ∪ platform_patches 的 status/reason）。"""
    out: dict[str, dict] = {}
    pp = _load(ws / "platform_patches.json")
    for p in (pp or {}).get("patches", []):
        out[p.get("gap", "")] = {"status": p.get("status"),
                                 "reason": p.get("reason") or "",
                                 "files": p.get("files") or []}
    for fp in sorted(ws.glob("P4/*/reports/fill.json")):
        res = (_load(fp) or {}).get("results", {})
        for api, r in res.items():
            patch = (r or {}).get("patch") or {}
            out[api] = {"status": r.get("status"),
                        "reason": patch.get("reason") or "",
                        "files": patch.get("files") or []}
    return out


def draft_gaps(ws: Path) -> int:
    """收成：工作区全部 gap 决策 + fill 成败 → temp/gaps/<ns>/<api>.md。

    幂等：同命名空间整区重建（INDEX 行同步替换）；其他命名空间不动。
    返回 0（无决策也算成功——空收成）。
    """
    proj = _load(ws / "project.json")
    if not proj:
        return 1
    ns = f"{Path(proj['linux_driver']).name}@{Path(proj['target_os']).name}"
    decisions = _collect_decisions(ws)
    fills = _fill_outcomes(ws)

    gdir = kb.domain_temp("gaps")
    ns_dir = gdir / ns
    if decisions:
        ns_dir.mkdir(parents=True, exist_ok=True)
    for f in ns_dir.glob("*.md"):
        f.unlink()
    rows: list[dict] = []
    for api, d in sorted(decisions.items()):
        fo = fills.get(api) or {}
        fill_line = "—（未走 fill）"
        if fo:
            reason = str(fo.get("reason") or "").strip()
            fill_line = (f"{fo.get('status', '?')}"
                         + (f"——{reason[:200]}" if reason else ""))
        body = [
            f"# {api} —— gap 处置：{d.get('strategy', '?')}", "",
            f"- 驱动@目标：{ns}",
            f"- 策略：{d.get('strategy', '?')}",
            f"- 处置指令：{d.get('instruction') or '—'}",
            f"- 证据：{d.get('evidence') or '—'}",
            f"- 理由（人工裁定留档）：{d.get('rationale') or '—'}",
            f"- fill 结果：{fill_line}",
        ]
        (ns_dir / f"{sanitize_api(api)}.md").write_text(
            "\n".join(body) + "\n", encoding="utf-8")
        desc = (f"{api}：{d.get('strategy', '?')}——"
                f"{str(d.get('instruction') or '')[:80]}")
        if fo.get("status") == "fell-back":
            desc += "（fill 曾失败）"
        rows.append({"file": f"{ns}/{sanitize_api(api)}.md",
                     "desc": desc, "hits": 0})

    # INDEX：替换本 ns 的行，保留其他命名空间与 hits
    idx = kb.load_index(gdir) or []
    kept_hits = {e["file"]: int(e.get("hits", 0) or 0)
                 for e in idx if isinstance(e, dict) and e.get("file")}
    idx = [e for e in idx if not (isinstance(e, dict)
                                  and str(e.get("file", ""))
                                  .startswith(f"{ns}/"))]
    for r in rows:
        r["hits"] = kept_hits.get(r["file"], 0)
    idx.extend(rows)
    kb.save_index(gdir, idx)
    if rows:
        _log.console_line(f"[porter] gaps 知识: 草稿已刷新 knowledge/temp/gaps/{ns}/"
              f"（{len(rows)} 个 API；含 fill 结果 {sum(1 for a in decisions if a in fills)} 条）")
    return 0


def prior_entry(kb_dir: Path | None, api: str) -> Path | None:
    """该 API 是否有历史 gap 处置/fill 记录（文件名存在性，零内容解析）。

    检索已审分区（知识库目录/gaps/<ns>/<api>.md）与草稿分区
    （temp/gaps/<ns>/<api>.md）；命中返回文件路径（调用方可给 agent 读）。
    """
    fname = f"{sanitize_api(api)}.md"
    roots: list[Path] = []
    if kb_dir is not None:
        roots.append(kb.domain_kb("gaps", kb_dir))
    roots.append(kb.domain_temp("gaps"))
    for d in roots:
        if not d.is_dir():
            continue
        hits = sorted(d.glob(f"*/{fname}"))
        if hits:
            return hits[0]
    return None
