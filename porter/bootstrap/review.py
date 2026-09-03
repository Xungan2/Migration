"""review.py — 知识审核面（CP5 材料）+ 分类 + 晋升/拒绝（随机知识后段）。

流水线（设计定案）：探查（candidates.py）→ **审核（人判价值，CP5 批审）**
→ **分类（定去向——agent 批量建议，人可改）→ 沉淀（promote 入子目录）**。

- build_cp5_material(ws)：CP5 备审材料——候选队列（含建议类与证据指针）
  + temp 草稿清点 + KB 健康报告（kb hits 聚合 + policy_hits 遥测 +
  veto 聚类 = B10 接班）。写 ws/checkpoints/CP5_knowledge.md。
- classify_candidates(ws, ids)：一次 agent 调用批量归类（顺序在审核后、
  沉淀前；PORTER_NO_AGENT=1 跳过退人工 --to）。改类写候选 history。
- promote_candidate(ws, cid, to)：候选 → <知识库目录>/<域> 条目
  （文件名 = ref 清洗 slug；gaps 域入 <ns>/ 嵌套；改判留档在条目尾），
  出候选账。
- reject_candidate(ws, cid)：人判无价值 → 出账。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from ..common import agent
from . import candidates as cand
from . import kb
from .. import log as _log

_CLASSIFY_TIMEOUT_SEC = 600


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _slug(ref: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]", "_", str(ref or "cand")).strip("_")
    return (s[:80] or "cand")


def _ns(ws: Path) -> str:
    try:
        proj = json.loads((Path(ws) / "project.json").read_text(
            encoding="utf-8"))
        return (f"{Path(proj['linux_driver']).name}"
                f"@{Path(proj['target_os']).name}")
    except (OSError, json.JSONDecodeError, KeyError):
        return "unknown@unknown"


# ---------- CP5 材料 ----------

def _health_report(ws: Path, kb_dir: Path | None) -> list[str]:
    lines: list[str] = []
    # KB hits（已审条目使用热度；INDEX 已折叠 + 旁车未折叠的合并值）
    if kb_dir is not None:
        side = kb.load_hits_sidecar(kb_dir)
        rows: list[tuple[str, str, int]] = []
        for dom in kb.DOMAINS:
            idx = kb.load_index(kb.domain_kb(dom, kb_dir)) or []
            for e in idx:
                if isinstance(e, dict) and e.get("file"):
                    h = (int(e.get("hits", 0) or 0)
                         + int(side.get(f"{dom}/{e['file']}", 0)))
                    rows.append((dom, str(e["file"]), h))
        hot = [r for r in rows if r[2] > 0]
        cold = [r for r in rows if r[2] == 0]
        lines.append(f"- 已审条目 {len(rows)}：被咨询 {len(hot)} / "
                     f"零咨询 {len(cold)}")
        for dom, f, h in sorted(hot, key=lambda r: -r[2])[:8]:
            lines.append(f"  - [{dom}] {f}（hits {h}）")
        if cold:
            lines.append("  - 零咨询（该复核或下架）："
                         + "，".join(f"[{d}]{f}" for d, f, _ in cold[:8]))
    # policy_hits（规则有效性遥测——B10 保留）
    ph = ws / "policy_hits.json"
    if ph.exists():
        try:
            hits = json.loads(ph.read_text(encoding="utf-8")).get("hits", {})
            if hits:
                lines.append("- policy 规则命中："
                             + "，".join(f"{k}×{v}" for k, v in
                                         sorted(hits.items(),
                                                key=lambda kv: -kv[1])[:8]))
        except (OSError, json.JSONDecodeError):
            pass
    # veto 聚类（自动层边界证据）
    try:
        from ..loop import gates as gates_mod
        led = gates_mod.GateLedger(ws).load()
        vetoed = [g for g in led.gates if g.get("status") == "vetoed"]
        if vetoed:
            lines.append(f"- 被否决的自动决策 {len(vetoed)} 条（边界证据，"
                         "理由可写成规则/知识）：")
            for g in vetoed[:8]:
                r = (g.get("answer") or {}).get("rationale") or ""
                lines.append(f"  - {g['id']}"
                             + (f"：{r[:80]}" if r else ""))
    except Exception:
        pass
    return lines


def build_cp5_material(ws: Path) -> Path:
    """写 CP5 知识备审材料 → ws/checkpoints/CP5_knowledge.md。"""
    ws = Path(ws)
    kb_dir = kb.kb_dir_for(ws)
    out = ws / "checkpoints" / "CP5_knowledge.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# CP5 知识备审材料", "", f"- 生成：{_now()}", ""]

    cands_ = cand.load_candidates(ws)
    lines += ["## 候选队列（随机知识——待审核）", ""]
    if not cands_:
        lines.append("（无候选）")
    for c in cands_:
        lines.append(f"### {c['id']}（建议类：{c.get('suggested_class')}"
                     f"；来源：{c['source']['hook']} / {c['source']['ref']}）")
        lines.append(f"- 草稿：{c['draft'][:400]}")
        if c.get("evidence"):
            lines.append(f"- 证据：{', '.join(c['evidence'][:4])}")
        lines.append(f"- 处置：`porter kb promote --output-dir <ws> "
                     f"--id {c['id']}`（可加 `--to <域>` 改类）/ "
                     f"`porter kb reject --id {c['id']}`")
        lines.append("")

    lines += ["## temp 草稿清点（固定域收成）", ""]
    any_draft = False
    for dom in kb.DOMAINS:
        idx = kb.load_index(kb.domain_temp(dom, ws=ws)) or []
        if idx:
            any_draft = True
            lines.append(f"- {dom}：{len(idx)} 条（"
                         + "；".join(str(e.get("file")) for e in idx[:5])
                         + ("…" if len(idx) > 5 else "") + "）")
    if not any_draft:
        lines.append("（各域无草稿）")

    lines += ["", "## KB 健康报告", ""]
    health = _health_report(ws, kb_dir)
    lines += health or ["（无数据）"]
    lines += ["", "## 审核后动作", "",
              "- 批量归类（agent 建议，供 promote 的 --to 参考）："
              "`porter kb classify --output-dir <ws>`",
              "- 晋升：`porter kb promote --output-dir <ws> "
              "--id <id> [--to <域>]`；固定域草稿：p1/p2-promote"]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


# ---------- 分类（agent 批量，审核后、沉淀前） ----------

_CLASSIFY_PROMPT = """你是驱动迁移工具的知识库管理员。以下是候选知识条目，
请为每条判断它属于哪个知识子目录（分类决定将来哪些 agent 会查到它）：

{kb_desc}

## 候选条目
{items}

## 任务
逐条输出分类。只输出一个紧凑 JSON 对象（一行）：
{{"items": [{{"id": "<候选id>", "class": "<子目录名>", "confidence":
"high|low"}}]}}
"""


def classify_candidates(ws: Path, ids: list[str] | None = None) -> int:
    """agent 批量归类（写回 suggested_class + history）。返回 0/1。

    PORTER_NO_AGENT=1 → 打印提示返回 0（人工经 promote --to 定案）。
    """
    if agent_lib_no_agent():
        _log.console_line("[porter] kb classify: PORTER_NO_AGENT=1——跳过 agent 归类，"
              "人工用 `kb promote --to <域>` 定案即可")
        return 0
    led = cand.load_candidates(ws)
    if ids:
        led = [c for c in led if c.get("id") in set(ids)]
    if not led:
        _log.console_line("[porter] kb classify: 无待分类候选")
        return 0
    kb_desc = "\n".join(f"- {d['subdir']}：{d['desc']}"
                        for d in kb.DOMAINS.values())
    items = "\n".join(
        f"- {c['id']}（现建议 {c.get('suggested_class')}，来源 "
        f"{c['source']['hook']}/{c['source']['ref']}）：{c['draft'][:300]}"
        for c in led)
    prompt = _CLASSIFY_PROMPT.format(kb_desc=kb_desc, items=items)
    ws = Path(ws)
    log = ws / "P7" / "logs" / "kb_classify_R1"
    log.parent.mkdir(parents=True, exist_ok=True)
    rc, out = agent.run_agent(prompt, workdir=ws, log_stem=str(log),
                              timeout_sec=_CLASSIFY_TIMEOUT_SEC)
    parsed = agent.extract_json(out) if rc == 0 else None
    if not isinstance(parsed, dict) and rc == 0:
        # 兜底：裸 JSON（extract_json 的裸文路径锚定 "moves"，不认 items）
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", out, re.DOTALL)
        try:
            obj = json.loads((m.group(1) if m else out).strip())
            parsed = obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            parsed = None
    items_out = (parsed or {}).get("items") if isinstance(parsed, dict) \
        else None
    if not isinstance(items_out, list):
        _log.console_line("[porter] kb classify: agent 输出不可解析（见 "
              f"{log}.log）——人工用 `kb promote --to` 定案")
        return 1
    ledger = cand.load_candidates(ws)      # 重读全账（按 id 回写）
    by_id = {c.get("id"): c for c in ledger}
    changed = 0
    for it in items_out:
        if not isinstance(it, dict):
            continue
        cid, cls = it.get("id"), it.get("class")
        c = by_id.get(cid)
        if not c or cls not in kb.DOMAINS:
            continue
        old = c.get("suggested_class")
        if cls != old:
            c["history"].append({"time": _now(),
                                 "event": f"归类改判 {old} → {cls}"
                                          f"（agent，conf="
                                          f"{it.get('confidence', '?')}）"})
            c["suggested_class"] = cls
            changed += 1
    if changed:
        cand._save_candidates(ws, ledger)    # noqa: SLF001（同包内部回写）
    _log.console_line(f"[porter] kb classify: 归类完成（改判 {changed}/{len(led)} 条）"
          "——定案仍在 promote（--to 可覆盖）")
    return 0


def agent_lib_no_agent() -> bool:
    import os
    return bool(os.environ.get("PORTER_NO_AGENT"))


# ---------- 晋升 / 拒绝 ----------

def _body(c: dict, domain: str, ns: str) -> str:
    src = c["source"]
    sc = c.get("scope") or {}
    lines = [
        f"# {_slug(src.get('ref', 'cand'))} —— {domain} 知识条目", "",
        c["draft"], "",
        "---", "",
        f"- 命名空间：{ns}",
        f"- 来源：{src.get('hook')} / {src.get('ref')}"
        f"（{src.get('time', '')}）",
        f"- 驱动@目标：{sc.get('driver', '?')}@{sc.get('target_os', '?')}",
    ]
    if c.get("evidence"):
        lines.append(f"- 证据：{', '.join(c['evidence'][:4])}")
    if domain != c.get("suggested_class"):
        lines.append(f"- 类别改判：建议 {c.get('suggested_class')} → "
                     f"实际 {domain}（人工，{_now()}）")
    lines.append(f"- 晋升：{_now()}")
    return "\n".join(lines) + "\n"


def promote_candidate(ws: Path, cid: str, to: str | None = None) -> int:
    """候选 → <知识库目录>/<域>/ 条目（文件 + 薄 INDEX 行），出账。"""
    ws = Path(ws)
    kb_dir = kb.kb_dir_for(ws)
    if kb_dir is None:
        _log.console_line("[porter] kb promote: 工作区未记录知识库目录（kb_dir）")
        return 1
    led = cand.load_candidates(ws)
    c = next((x for x in led if x.get("id") == cid), None)
    if c is None:
        _log.console_line(f"[porter] kb promote: 候选不存在 {cid}")
        return 1
    domain = to or c.get("suggested_class") or "pitfalls"
    if domain not in kb.DOMAINS:
        _log.console_line(f"[porter] kb promote: 未知子目录 {domain!r}"
              f"（须 {sorted(kb.DOMAINS)}）")
        return 1
    ns = _ns(ws)
    ddir = kb.domain_kb(domain, kb_dir)
    if domain == "gaps":
        rel = f"{ns}/{_slug(c['source'].get('ref', cid))}.md"
    else:
        rel = f"{ns}__{_slug(c['source'].get('ref', cid))}.md"
    tgt = ddir / rel
    k = 2
    while tgt.exists():
        stem, suf = rel[:-3], rel[-3:]
        rel = (f"{stem}__{k}{suf}" if domain != "gaps"
               else f"{stem}__{k}{suf}")
        tgt = ddir / rel
        k += 1
    tgt.parent.mkdir(parents=True, exist_ok=True)
    tgt.write_text(_body(c, domain, ns), encoding="utf-8")
    desc = f"[{ns}] {c['draft'][:96]}"
    didx = kb.load_index(ddir) or []
    didx = kb.upsert_entry(didx, rel, desc)
    folded = kb.fold_sidecar_hits(kb_dir, domain, [rel])  # 晋升折叠旁车
    for de in didx:
        if isinstance(de, dict) and de.get("file") == rel:
            de["hits"] = int(de.get("hits", 0)) + int(folded.get(rel, 0))
    kb.save_index(ddir, didx)
    cand.remove_candidate(ws, cid)
    _log.console_line(f"[porter] kb promote: {cid} 已晋升 → {tgt}")
    return 0


def reject_candidate(ws: Path, cid: str) -> int:
    if not cand.remove_candidate(Path(ws), cid):
        _log.console_line(f"[porter] kb reject: 候选不存在 {cid}")
        return 1
    _log.console_line(f"[porter] kb reject: {cid} 已出账（人判无价值）")
    return 0
