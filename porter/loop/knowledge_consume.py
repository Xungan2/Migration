"""knowledge_consume.py — maps 域消费侧（P3 增量映射的历史映射提示注入）。

目录模型（kb.py）：已审条目在**本次迁移的知识库目录**
（project.json["kb_dir"]，p0 --kb 指定）的 maps/ 下；temp 草稿不注入
（未审中间态不参与行为——与知识子系统信任分层一致）。

路由（旧格式 INDEX 驱动，两级）：
  1. 精确：<驱动名>@<目标OS名>（本驱动历史轮沉淀——活文档最新版）
  2. 类别回退：同目标 OS 且 category 有交集的其他驱动条目

注入规则（沉淀规范同款铁律）：
  - 只注入与当前批次**域相同**的条目（域过滤）
  - 渲染为"仅提示"块：条目是经源码核实的主张，消费 agent 必须重核实，
    禁止照抄 evidence（"核实后抄入、不跨驱动复用未验证结论"）
  - 每次命中 knowledge 侧 hits+1（使用热度，供人工晋升决策参考）

注：本模块的内容注入是 maps 域的历史形态；统一检索面（INDEX 目录
注入 + agent 自取 + kb_consulted）在知识子系统迁移中接管。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..bootstrap import kb


def route_entries(kb_dir: Path, driver: str, target: str,
                  category: list[str]) -> list[dict]:
    """INDEX 路由：返回候选条目（精确匹配在前）。不修改任何状态。"""
    idx = kb.load_index(kb_dir / "maps") or []
    if not idx:
        return []
    exact = [e for e in idx if isinstance(e, dict)
             and e.get("driver_name") == driver
             and e.get("target_os") == target]
    if exact:
        return exact
    return [e for e in idx if isinstance(e, dict)
            and e.get("target_os") == target
            and set(e.get("category") or []) & set(category)]


def collect_hints(ws: Path, driver: str, target: str, category: list[str],
                  domains: list[str]) -> tuple[str, list[dict]]:
    """收集域过滤后的提示条目并计数。返回 (提示块文本, 命中的 INDEX 条目)。

    只在知识库目录侧计数（temp 草稿是未审阅中间态，不计数）。
    工作区未记录 kb_dir（旧工作区）→ 无提示。
    """
    kb_dir = kb.kb_dir_for(ws)
    if kb_dir is None:
        return "", []
    mdir = kb_dir / "maps"
    cands = route_entries(kb_dir, driver, target, category)
    if not cands or not domains:
        return "", []
    dom_set = set(domains)
    lines: list[str] = []
    hit: list[dict] = []
    for e in cands:
        jpath = mdir / e.get("entry_json", "")
        if not jpath.exists():
            continue
        try:
            table = json.loads(jpath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        picked = [e2 for e2 in table.get("entries", [])
                  if e2.get("domain") in dom_set]
        if not picked:
            continue
        hit.append(e)
        lines.append(f"# 来自 {e['entry_stem']} v{e.get('version', '?')}"
                     f"（仅提示，须重核实，禁止照抄）")
        for e2 in picked:
            tgt = (e2.get("target") or "").replace("\n", " ")[:160]
            lines.append(f"- {e2['linux_api']}（{e2.get('verdict')}）"
                         f"→ {tgt}")
        lines.append("")
    if not hit:
        return "", []
    # hits+1（使用热度）
    idx = kb.load_index(mdir) or []
    stems = {e["entry_stem"] for e in hit}
    for e in idx:
        if isinstance(e, dict) and e.get("entry_stem") in stems:
            e["hits"] = int(e.get("hits", 0)) + 1
    kb.save_index(mdir, idx)
    return "\n".join(lines), hit
