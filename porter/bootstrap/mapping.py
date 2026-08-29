"""mapping.py — P2a 引导映射编排（agent 分批小调用 + 机器校验 + 增量合并）。

流程（plan: vertical-slice-pipeline §3.1/§10 定案 1/2）：
  1. spine_api.json（缺则先跑 extract_spine）
  2. 按域分批（driver_included 域优先独立成批，其余小域按符号数聚拢，
     每批 ≤BATCH_SIZE）：agent 调用（SKILL + 域上下文 + 符号清单）
  3. 机器校验：9 字段 schema + verdict/kind/risk/confidence 枚举 +
     evidence 路径在目标 OS 树真实存在（铁律的机器化）；失败带反馈
     重试 ≤MAX_TRIES，仍败登记失败批继续（幂等：已成条目自动跳过）
  4. 类型 B 调用：跨模块换思路裁定 + 骨架接线清单
  5. 增量合并 mapping.json（linux_api 去重；notes 冲突追加）→ 渲染
     mapping.md → mapping_report.md（人工审阅关口 = 末尾报告，§10 定案 6）
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..common import agent
from . import extract_spine

BATCH_SIZE = 35          # 每批符号上限（32K 教训：小批次；输出帽已抬至 128K）
MAX_TRIES = 3            # 每批：首发 + 带反馈重试 2 次
AGENT_TIMEOUT_SEC = 900

VERDICTS = {"direct", "adapt", "gap", "not-migrated"}
KINDS = {"function", "struct", "macro", "idiom", "config"}
RISKS = {"none", "low", "med", "high"}
CONFIDENCES = {"high", "medium", "low"}

# 换思路裁定种子清单（§10 定案 1；agent 树内核实后落 redesigns）
REDESIGN_SEEDS = [
    "NAPI 模型（中断→屏蔽→轮询→重使能）",
    "qdisc 停队/发送背压",
    "sk_buff 缓冲（frags/零拷贝）",
    "workqueue 延迟工作（watchdog/reset_task）",
    "PCI 驱动注册与设备 ID 匹配形态",
    "DMA 一致性模型与 sync 时机",
    "中断处理上下文限制（probe vs softirq/中断）",
]
WIRING_ITEMS = [
    "组件注册（init_component / Components.toml）",
    "PCI 驱动注册 + 设备 ID 匹配",
    "BAR/MMIO 基础访问",
    "日志设施",
    "ktest 注册",
    "网络栈设备注册点",
]


# ---------- 批次组织 ----------

def _batches(spine: dict) -> list[tuple[str, list[str]]]:
    """(域标签, 符号列表) 批次序列。driver_included 域优先、独立成批；
    未 include 的域按符号数降序聚拢补齐成批（保持域内连续）。"""
    doms = spine["domains"]
    incl = sorted((k for k, v in doms.items() if v["driver_included"]),
                  key=lambda k: -len(doms[k]["symbols"]))
    rest = sorted((k for k, v in doms.items() if not v["driver_included"]),
                  key=lambda k: -len(doms[k]["symbols"]))
    batches: list[tuple[str, list[str]]] = []
    for k in incl:
        syms = doms[k]["symbols"]
        for i in range(0, len(syms), BATCH_SIZE):
            batches.append((k, syms[i:i + BATCH_SIZE]))
    tail: list[str] = []
    tail_domains: list[str] = []
    for k in rest:
        tail_domains.append(k)
        tail.extend(doms[k]["symbols"])
        if len(tail) >= BATCH_SIZE:
            batches.append((",".join(tail_domains), tail[:BATCH_SIZE]))
            tail = tail[BATCH_SIZE:]
            tail_domains = []
    if tail:
        batches.append((",".join(tail_domains), tail))
    return batches


# ---------- 校验 ----------

def _check_evidence(ev: str, target_os: Path) -> str | None:
    """evidence 形如 path:line（多条分号分隔）。路径须在目标树存在。
    返回错误描述或 None。"""
    if not ev:
        return None
    for one in [e.strip() for e in ev.split(";") if e.strip()]:
        path, _, line = one.rpartition(":")
        if not path or (line and not line.split("-")[0].isdigit()):
            return f"evidence 格式非法: {one!r}（须 path:line）"
        if not (target_os / path).exists():
            return f"evidence 路径在目标树不存在: {path}"
    return None


def _validate_entries(raw: list, target_os: Path,
                      domain: str) -> tuple[list[dict], list[str]]:
    """返回 (合格条目, 错误清单)。"""
    ok: list[dict] = []
    errs: list[str] = []
    if not isinstance(raw, list):
        return [], ["entries 不是数组"]
    for i, e in enumerate(raw):
        if not isinstance(e, dict):
            errs.append(f"[{i}] 非对象")
            continue
        miss = [k for k in ("linux_api", "kind", "verdict", "target",
                            "evidence", "notes", "risk", "confidence",
                            "domain") if k not in e]
        if miss:
            errs.append(f"[{i}] 缺字段 {miss}")
            continue
        problems = []
        if e["verdict"] not in VERDICTS:
            problems.append(f"verdict 非法: {e['verdict']}")
        if e["kind"] not in KINDS:
            problems.append(f"kind 非法: {e['kind']}")
        if e["risk"] not in RISKS:
            problems.append(f"risk 非法: {e['risk']}")
        if e["confidence"] not in CONFIDENCES:
            problems.append(f"confidence 非法: {e['confidence']}")
        ev = e.get("evidence", "")
        if e["verdict"] in ("direct", "adapt"):
            if not ev:
                problems.append("direct/adapt 必须有 evidence")
            else:
                p = _check_evidence(ev, target_os)
                if p:
                    problems.append(p)
        if e["domain"] != domain and domain not in e["domain"]:
            problems.append(f"domain 应为 {domain}")
        if problems:
            errs.append(f"[{e.get('linux_api', i)}] {'; '.join(problems)}")
        else:
            ok.append(e)
    return ok, errs


# ---------- mapping.json / mapping.md ----------

def _load_mapping(p2: Path) -> dict:
    path = p2 / "mapping.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"entries": [], "redesigns": [], "wiring": [],
            "meta": {"created": datetime.now().isoformat()}}


def _merge(mapping: dict, entries: list[dict]) -> int:
    """按 linux_api 去重合并；verdict/notes 分歧时追加备注。返回新增数。"""
    index = {e["linux_api"]: e for e in mapping["entries"]}
    added = 0
    for e in entries:
        old = index.get(e["linux_api"])
        if old is None:
            mapping["entries"].append(e)
            index[e["linux_api"]] = e
            added += 1
        else:
            if old["verdict"] != e["verdict"]:
                old["notes"] = (old["notes"].rstrip() +
                                f"｜分歧(P2a 批次间): {e['verdict']}:"
                                f"{e['notes']}").lstrip("｜")
    return added


def _render_md(mapping: dict, p2: Path) -> None:
    ents = mapping["entries"]
    verdict_of = {v: sum(1 for e in ents if e["verdict"] == v)
                  for v in sorted(VERDICTS)}
    lines = [
        "# API 映射（P2 引导 + P3 增量累积）", "",
        f"> 真值源 `mapping.json`；本文件为渲染产物（{datetime.now():%Y-%m-%d %H:%M}）。",
        f"> 共 {len(ents)} 条：direct {verdict_of.get('direct', 0)} / "
        f"adapt {verdict_of.get('adapt', 0)} / gap {verdict_of.get('gap', 0)}"
        f" / not-migrated {verdict_of.get('not-migrated', 0)}", "",
    ]
    by_domain: dict[str, list[dict]] = {}
    for e in sorted(ents, key=lambda x: (x["domain"], x["linux_api"])):
        by_domain.setdefault(e["domain"], []).append(e)
    for dom in sorted(by_domain):
        lines += [f"## {dom}", "",
                  "| Linux 用法 | 目标方案 | 已核实 | 备注 |",
                  "|---|---|---|---|"]
        for e in by_domain[dom]:
            v = f"`{e['linux_api']}`（{e['verdict']}）"
            note = e.get("notes") or ""
            if e.get("risk") in ("med", "high"):
                note = f"⚠risk:{e['risk']} " + note
            if e.get("confidence") != "high":
                note = f"conf:{e.get('confidence')} " + note
            lines.append(f"| {v} | {e.get('target') or ''} "
                         f"| {e.get('evidence') or '—'} | {note} |")
        lines.append("")
    lines += ["## 换思路（跨模块裁定）", "",
              "| Linux 习语 | 目标方案 | 依据 | 理由 |", "|---|---|---|---|"]
    for r in mapping["redesigns"]:
        lines.append(f"| {r.get('linux_pattern', r.get('id', ''))} "
                     f"| {r.get('target_approach', '')} "
                     f"| {r.get('evidence', '') or '—'} "
                     f"| {r.get('rationale', '')} |")
    lines += ["", "## 接线清单（骨架）", "",
              "| 项 | 目标 API | 已核实 | 备注 |", "|---|---|---|---|"]
    for w in mapping["wiring"]:
        lines.append(f"| {w.get('item', '')} | {w.get('target_api', '')} "
                     f"| {w.get('evidence', '') or '—'} | {w.get('notes', '')} |")
    lines.append("")
    (p2 / "mapping.md").write_text("\n".join(lines), encoding="utf-8")


def _save(mapping: dict, p2: Path) -> None:
    (p2 / "mapping.json").write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    _render_md(mapping, p2)


# ---------- agent 调用 ----------

def _prompt_map(skill: str, driver_root: Path, target_os: Path,
                domain: str, syms: list[str], mods: list[str],
                existing: list[str]) -> str:
    return (f"{skill}\n\n---\n\n## 背景数据\n"
            f"- 驱动：Linux 源码参考树 `{driver_root}`（仅语义参考）\n"
            f"- 目标 OS 源码树：`{target_os}` = 你的工作目录，"
            f"所有 evidence 必须在此树内核实（路径相对该树根）\n"
            f"- 域：`{domain}`"
            f"（被模块 {', '.join(mods)} include；未被直接 include 则为传递使用）\n"
            + (f"- 全局表已有条目（勿重复映射）：{', '.join(existing)}\n"
               if existing else "")
            + f"\n## 任务（类型 A：符号映射）\n映射以下 {len(syms)} 个符号"
            f"（均属 `{domain}` 域）：\n{', '.join(syms)}\n"
            f"逐符号核实后输出一个紧凑 JSON 块（entries 数组，"
            f"每条 domain 填 `{domain.split(',')[0]}`）。")


def _prompt_redesign(skill: str, driver_root: Path, target_os: Path) -> str:
    seeds = "\n".join(f"- {s}" for s in REDESIGN_SEEDS)
    wires = "\n".join(f"- {w}" for w in WIRING_ITEMS)
    return (f"{skill}\n\n---\n\n## 背景数据\n"
            f"- 驱动：Linux 源码参考树 `{driver_root}`（仅语义参考）\n"
            f"- 目标 OS 源码树：`{target_os}` = 你的工作目录，"
            f"所有 evidence 必须在此树内核实\n"
            f"\n## 任务（类型 B：换思路裁定 + 接线清单）\n"
            f"对下列 Linux 习语逐项在目标树核实替代方案，输出 redesigns：\n"
            f"{seeds}\n"
            f"并核实骨架接线点，输出 wiring（覆盖以下各项）：\n{wires}\n"
            f"输出一个紧凑 JSON 块。")


def _call_agent(prompt: str, target_os: Path, log_stem: Path) -> dict | None:
    rc, out = agent.run_agent(prompt, workdir=target_os,
                              log_stem=str(log_stem),
                              timeout_sec=AGENT_TIMEOUT_SEC)
    if rc != 0:
        return None
    parsed = agent.extract_json(out)
    return parsed if isinstance(parsed, dict) else None


# ---------- 主流程 ----------

def run_map(ws: Path, driver_root: Path, target_os: Path) -> int:
    """返回 0=成功；1=存在失败批；2=前置缺失。幂等：已成条目自动跳过。"""
    p2 = ws / "P2"
    spine_path = p2 / "reports" / "spine_api.json"
    if not spine_path.exists():
        rc = extract_spine.run_extract(ws, driver_root)
        if rc != 0:
            return rc
    spine = json.loads(spine_path.read_text(encoding="utf-8"))
    (p2 / "logs").mkdir(parents=True, exist_ok=True)

    mapping = _load_mapping(p2)
    skill = agent.load_skill("P2-bootstrap-map")
    failed: list[str] = []
    # 域归属权威映射（脚本事实）：条目 domain 一律以此覆盖，不信 agent 抄写
    # （合并批的 agent 抄写会错标，2026-08-29 质检实证 152 例）
    sym_dom = {s: d for d, v in spine["domains"].items()
               for s in v["symbols"]}

    for domain, syms in _batches(spine):
        mods = (spine["domains"].get(domain, {})
                .get("included_by_modules", []))
        dom_key = domain.split(",")[0]
        # 幂等：跳过全已成批
        have = {e["linux_api"] for e in mapping["entries"]}
        todo = [s for s in syms if s not in have]
        if not todo:
            print(f"[porter] P2a: 批 {dom_key}（{len(syms)} 符号）已全映射——跳过")
            continue
        print(f"[porter] P2a: 映射批 {dom_key}——{len(todo)}/{len(syms)} 待映射")
        feedback = ""
        base = _prompt_map(skill, driver_root, target_os, dom_key, todo,
                           mods, [s for s in syms if s in have][:10])
        got: list[dict] = []
        for attempt in range(1, MAX_TRIES + 1):
            parsed = _call_agent(base + feedback, target_os,
                                 p2 / "logs" / f"P2A_{dom_key.replace('/', '_')}_R{attempt}")
            if parsed and "entries" in parsed:
                got, errs = _validate_entries(parsed["entries"], target_os,
                                              dom_key)
                for e in got:       # 权威域覆盖（防合并批错标）
                    if e["linux_api"] in sym_dom:
                        e["domain"] = sym_dom[e["linux_api"]]
                covered = {e["linux_api"] for e in got}
                missing = [s for s in todo if s not in covered]
                if got and not missing and not errs:
                    break
                fb_lines = []
                if missing:
                    fb_lines.append("以下符号仍缺条目，补齐后重新输出全部："
                                    + ", ".join(missing))
                if errs:
                    fb_lines.append("不合格条目（修正后重新输出全部）：" +
                                    "; ".join(errs[:8]))
                feedback = ("\n\n---\n\n## 上一次输出的问题（修正后重新输出"
                            "完整 JSON）\n" + "\n".join(fb_lines))
                if attempt < MAX_TRIES:
                    _merge(mapping, got)  # 保留合格部分，重试只补缺
            else:
                feedback = ("\n\n---\n\n## 上一次输出的问题\n未见合法 JSON 块"
                            "（可能被截断）。只输出一个紧凑 JSON 对象，"
                            "整个对象写成一行。")
        if got:
            _merge(mapping, got)
        covered = {e["linux_api"] for e in mapping["entries"]}
        if [s for s in todo if s not in covered]:
            failed.append(dom_key)
            print(f"[porter] P2a: 批 {dom_key} {MAX_TRIES} 次后仍有缺口——"
                  f"登记失败，继续")
        else:
            print(f"[porter] P2a: 批 {dom_key} 完成（累计 "
                  f"{len(mapping['entries'])} 条）")
        _save(mapping, p2)      # 每批 checkpoint：中断重启不重付已完成批

    # 类型 B：换思路 + 接线（幂等：已存在则跳过）
    if not mapping["redesigns"] and not mapping["wiring"]:
        print("[porter] P2a: 换思路裁定 + 接线清单（类型 B）")
        parsed = None
        for attempt in range(1, MAX_TRIES + 1):
            parsed = _call_agent(_prompt_redesign(skill, driver_root,
                                                  target_os),
                                 target_os,
                                 p2 / "logs" / f"P2A_redesign_R{attempt}")
            if parsed and ("redesigns" in parsed or "wiring" in parsed):
                break
        if parsed:
            for r in parsed.get("redesigns") or []:
                r.setdefault("origin", "P2a")
                mapping["redesigns"].append(r)
            for w in parsed.get("wiring") or []:
                mapping["wiring"].append(w)
            ev_errs = [f"{w.get('item')}: {p}" for w in mapping["wiring"]
                       if (p := _check_evidence(w.get("evidence", ""),
                                                target_os))]
            if ev_errs:
                print(f"[porter] P2a: ⚠ wiring evidence 问题：{ev_errs}")
        else:
            failed.append("(redesign/wiring)")
            print("[porter] P2a: 类型 B 调用失败——登记，继续")

    _save(mapping, p2)

    # 末尾增量报告（人工审阅关口 = 仿 P1：报告 + 人工决定沉淀，不中断）
    ents = mapping["entries"]
    vc = {v: sum(1 for e in ents if e["verdict"] == v) for v in VERDICTS}
    risky = [e["linux_api"] for e in ents if e["risk"] in ("med", "high")]
    lowc = [e["linux_api"] for e in ents if e["confidence"] == "low"]
    gaps = [e["linux_api"] for e in ents if e["verdict"] == "gap"]
    rpt = [
        "# P2a 映射增量报告", "",
        f"- 生成时间: {datetime.now():%Y-%m-%d %H:%M}",
        f"- 条目总数: {len(ents)}（direct {vc['direct']} / adapt {vc['adapt']}"
        f" / gap {vc['gap']} / not-migrated {vc['not-migrated']}）",
        f"- redesigns: {len(mapping['redesigns'])} 条；"
        f"wiring: {len(mapping['wiring'])} 条",
        f"- 失败批: {failed or '无'}",
        f"- 高风险条目（探针候选，P3 消费）: {len(risky)}: "
        f"{', '.join(risky[:40]) or '—'}",
        f"- 低置信条目（探针候选）: {len(lowc)}: {', '.join(lowc[:40]) or '—'}",
        f"- gap 条目（决策队列输入）: {', '.join(gaps) or '—'}",
        f"- unresolved 符号（未映射，P3(M) 兜底）: "
        f"{spine['stats']['unresolved']} 个（见 spine_api.json）",
        "",
        "人工关口（§10 定案 6，增量沉淀）：审阅 `mapping.md`；有价值即",
        f"可 `p2-promote --driver <名> --target <目标>` 晋升（P2 末为首个",
        "沉淀点，此后每轮循环末可再次晋升；草稿已自动入 temp/maps/）。",
    ]
    (p2 / "reports" / "mapping_report.md").write_text(
        "\n".join(rpt) + "\n", encoding="utf-8")
    print(f"[porter] P2a: 映射完成——{len(ents)} 条 → mapping.json/md；"
          f"报告 → {p2 / 'reports' / 'mapping_report.md'}")

    # 知识沉淀草稿（增量：P2 末首个点，此后每轮 P3(M) 末随 run_map 刷新）
    try:
        from . import knowledge as kn
        kn.draft_knowledge(ws)
    except Exception as e:      # 沉淀失败不影响映射主流程（仿 P1S 模式）
        print(f"[porter] P2a: ⚠️ 知识草稿失败（不影响映射）：{e}")
    return 1 if failed else 0
