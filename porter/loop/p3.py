"""p3.py — P3(M) 增量映射 + 探针（plan §3.2）。

步骤（各自幂等，产物在 P3/<M>/reports/）：
  1. surface.json     使用面提取（surface.extract_surface）
  2. answers 消费     人工答案写回映射（上轮 exit-3 的清偿路径）
  3. 增量映射         missing 按域分批 → agent（类型 A）→ 校验合并入
                      P2/mapping.json（origin=P3<M>；知识提示注入）
  4. gap 处置分类     模块面 gap → agent（类型 B）→ gap_decisions.json
                      （bypass / fill / register-fill / human；
                       human → exit 3）
  5. criteria.json    基线 + agent 草案 → schema 校验
  6. 探针             risk∈{med,high} ∪ low-confidence → 生成住骨架 →
                      build+boot 双信号判定 → FAIL 有界改判 → 仍败降级 gap
                      （并入步骤 4 的决策路径重分类）
  7. 收尾             刷新 temp/maps 草稿 + P3/<M>/reports/report.md

返回：0 成功 / 1 失败 / 2 前置缺失 / 3 需人工（gap 队列）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..bootstrap import knowledge as kn
from ..bootstrap.mapping import (BATCH_SIZE, MAX_TRIES, _check_evidence,
                                 _load_mapping, _merge, _save,
                                 _validate_entries)
from ..common import agent
from ..env import probe as probe_mod
from . import criteria as crit_mod
from . import knowledge_consume
from . import probes as probe_lib
from . import surface as surface_mod

AGENT_TIMEOUT_SEC = 900


def _ctx(ws: Path, module: str) -> tuple[Path, Path, Path, dict] | None:
    proj_path = ws / "project.json"
    if not proj_path.exists():
        print(f"[porter] P3: 缺少 {proj_path}")
        return None
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    driver_root = Path(proj["linux_driver"])
    target_os = Path(proj["target_os"])
    if not driver_root.is_dir() or not target_os.is_dir():
        print(f"[porter] P3: 路径无效 {driver_root} / {target_os}")
        return None
    p3m = ws / "P3" / module
    (p3m / "logs").mkdir(parents=True, exist_ok=True)
    (p3m / "reports").mkdir(parents=True, exist_ok=True)
    return driver_root, target_os, p3m, proj


# ---------- 人工答案 ----------

def _apply_answers(ws: Path, module: str, p3m: Path) -> int:
    """消费人工-gap 答案（写回映射 + 决策）。

    双协议：先走 gates 账本（`## @p3.gap.<module>.<api>` 表单答案，
    process_answered_gates 已在 loop 入口消费并经 applier 改正本），
    再走 legacy 自由文本（`## <linux_api>` 节）。仍有未答 human → 3。
    """
    dec_path = p3m / "reports" / "gap_decisions.json"
    if not dec_path.exists():
        return 0
    dec = json.loads(dec_path.read_text(encoding="utf-8"))
    human = [d for d in dec.get("decisions", [])
             if d.get("strategy") == "human"]
    if not human:
        return 0
    from .state import consume_answers
    keys = [d["linux_api"] for d in human]
    taken = consume_answers(ws, keys)
    mapping = _load_mapping(ws / "P2")
    index = {e["linux_api"]: e for e in mapping["entries"]}
    n = 0
    for d in dec["decisions"]:
        ans = taken.get(d["linux_api"])
        if not ans:
            continue
        e = index.get(d["linux_api"])
        if e:
            e["notes"] = (e["notes"].rstrip() +
                          f"｜绕过(人工): {ans}").lstrip("｜")
            e["confidence"] = "high"
        d["strategy"] = "bypass"
        d["instruction"] = ans
        d["answered"] = True
        n += 1
    _save(mapping, ws / "P2")
    dec_path.write_text(json.dumps(dec, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    if n:
        print(f"[porter] P3: 人工答案写回 {n} 条（{module}）")
    remaining = [d["linux_api"] for d in dec["decisions"]
                 if d.get("strategy") == "human"]
    if remaining:
        _panic_gap_gates(ws, module, remaining)
        return 3
    return 0


# ---------- 步骤 3：增量映射 ----------

def _prompt_map_type_a(skill: str, driver_root: Path, target_os: Path,
                       module: str, domain: str, syms: list[str],
                       locs: dict, hints: str) -> str:
    loc_lines = "\n".join(
        f"- {s}: {'; '.join(locs.get(s, [])[:3]) or '—'}" for s in syms)
    return (f"{skill}\n\n---\n\n## 背景数据\n"
            f"- 驱动 Linux 源码参考树：`{driver_root}`（仅语义参考）\n"
            f"- 目标 OS 源码树：`{target_os}` = 你的工作目录，"
            f"evidence 必须在此树核实（相对该树根）\n"
            f"- 模块：{module}；域：`{domain}`\n"
            + (f"\n## 参考知识（仅提示，须重核实）\n{hints}\n"
               if hints else "")
            + f"\n## 使用位置（file:line 为模块切分文件内位置）\n{loc_lines}"
            f"\n\n## 任务（类型 A：符号映射）\n映射以下 {len(syms)} 个符号"
            f"（均属 `{domain}` 域）：\n{', '.join(syms)}\n"
            f"逐符号核实后输出一个紧凑 JSON 块（entries 数组，每条 domain "
            f"填 `{domain}`；若是驱动内部符号/字段名而非 OS API，判 "
            f"not-migrated 并在 notes 说明）。")


def _step_missing_mapping(ws: Path, driver_root: Path, target_os: Path,
                          module: str, p3m: Path, proj: dict,
                          surface: dict) -> list[str]:
    """真缺失符号的增量映射。返回失败域清单。

    幂等对账：surface.json 可能是映射增长前的快照——先按当前
    mapping.json 剔除已映射符号（断点重跑不重复付费）。
    """
    mapping = _load_mapping(ws / "P2")
    have = {e["linux_api"] for e in mapping["entries"]}
    missing_by_domain: dict[str, list[str]] = {
        d: [s for s in syms if s not in have]
        for d, syms in (surface.get("missing_by_domain") or {}).items()}
    missing_by_domain = {d: v for d, v in missing_by_domain.items() if v}
    if not missing_by_domain:
        print(f"[porter] P3: {module} 无缺失符号——映射步骤跳过")
        return []
    mapping = _load_mapping(ws / "P2")
    skill = agent.load_skill("P3-module-map")
    failed: list[str] = []
    locs = surface.get("usage_locations") or {}
    for domain, syms in sorted(missing_by_domain.items()):
        for i in range(0, len(syms), BATCH_SIZE):
            batch = syms[i:i + BATCH_SIZE]
            dom_key = domain.replace("/", "_")
            hints, _hit = knowledge_consume.collect_hints(
                proj.get("driver_name") or Path(proj["linux_driver"]).name,
                Path(proj["target_os"]).name, proj.get("category") or [],
                [domain])
            base = _prompt_map_type_a(skill, driver_root, target_os, module,
                                      domain, batch, locs, hints)
            got: list[dict] = []
            feedback = ""
            for attempt in range(1, MAX_TRIES + 1):
                rc, out = agent.run_agent(
                    base + feedback, workdir=target_os,
                    log_stem=str(p3m / "logs" /
                                 f"P3A_{dom_key}_R{attempt}"),
                    timeout_sec=AGENT_TIMEOUT_SEC)
                parsed = agent.extract_json(out) if rc == 0 else None
                if parsed and "entries" in parsed:
                    got, errs = _validate_entries(parsed["entries"],
                                                  target_os, domain)
                    covered = {e["linux_api"] for e in got}
                    missing_now = [s for s in batch if s not in covered]
                    if got and not missing_now and not errs:
                        break
                    fb = []
                    if missing_now:
                        fb.append("以下符号仍缺条目，补齐后重输出全部："
                                  + ", ".join(missing_now))
                    if errs:
                        fb.append("不合格条目（修正后重输出全部）：" +
                                  "; ".join(errs[:8]))
                    feedback = ("\n\n---\n\n## 上一次输出的问题（修正后重输出"
                                "完整 JSON）\n" + "\n".join(fb))
                    if attempt < MAX_TRIES and got:
                        for e in got:
                            e["origin"] = f"P3{module}"
                        _merge(mapping, got)
                else:
                    feedback = ("\n\n---\n\n## 上一次输出的问题\n未见合法 "
                                "JSON 块。只输出一个紧凑 JSON 对象（一行）。")
            if got:
                for e in got:
                    e["origin"] = f"P3{module}"
                _merge(mapping, got)
            _save(mapping, ws / "P2")
            have = {e["linux_api"] for e in mapping["entries"]}
            if [s for s in batch if s not in have]:
                failed.append(f"{domain}[{i}:{i + len(batch)}]")
                print(f"[porter] P3: 批 {domain} {MAX_TRIES} 次后仍有缺口"
                      "——登记失败，继续")
            else:
                print(f"[porter] P3: 批 {domain} 完成（累计 "
                      f"{len(mapping['entries'])} 条）")
    return failed


# ---------- 步骤 4：gap 处置分类 ----------

def _prompt_gap_classify(skill: str, driver_root: Path, target_os: Path,
                         module: str, gaps: list[dict],
                         locs: dict) -> str:
    lines = "\n".join(
        f"- {g['linux_api']}：target={g['target'][:120]}；notes="
        f"{g['notes'][:120]}；risk={g['risk']}；confidence={g['confidence']}；"
        f"使用位置 {'; '.join(locs.get(g['linux_api'], [])[:2]) or '—'}"
        for g in gaps)
    return (f"{skill}\n\n---\n\n## 背景数据\n"
            f"- 目标 OS 源码树：`{target_os}` = 你的工作目录\n"
            f"- 模块：{module}\n\n## 本模块面 gap 条目\n{lines}\n"
            f"\n## 任务（类型 B：gap 处置分类）\n逐条给出 strategy 与可执行"
            f"指令，输出紧凑 JSON 块。")


def _step_gap_decisions(ws: Path, target_os: Path, module: str, p3m: Path,
                        surface: dict) -> int:
    dec_path = p3m / "reports" / "gap_decisions.json"
    gaps: list[dict] = surface.get("gaps") or []
    # 重读映射（步骤 3 可能升级/新增条目 → gap 面变化）
    mapping = _load_mapping(ws / "P2")
    entries = {e["linux_api"]: e for e in mapping["entries"]}
    # 模块 OS-API 全集 = surface 时已映射 ∋ 步骤 3 新映射（原缺失）；
    # 步骤 3 合并后重查 verdict
    mapped = [s for v in (surface.get("mapped_by_verdict") or {}).values()
              for s in v]
    for syms in (surface.get("missing_by_domain") or {}).values():
        mapped.extend(syms)
    gaps = []
    for s in mapped:
        e = entries.get(s)
        if e and e["verdict"] == "gap":
            gaps.append({"linux_api": s, "target": e.get("target", ""),
                         "notes": e.get("notes", ""),
                         "risk": e.get("risk", ""),
                         "confidence": e.get("confidence", "")})
    if not gaps:
        dec_path.write_text(json.dumps({"decisions": []},
                                       ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"[porter] P3: {module} 模块面无 gap——分类步骤跳过")
        return 0
    if dec_path.exists():
        dec = json.loads(dec_path.read_text(encoding="utf-8"))
        if {d["linux_api"] for d in dec.get("decisions", [])} == \
                {g["linux_api"] for g in gaps}:
            rc = _apply_answers(ws, module, p3m)
            if rc == 3:
                return 3
            print(f"[porter] P3: {module} gap 分类已存在——复用")
            return 0

    skill = agent.load_skill("P3-module-map")
    locs = surface.get("usage_locations") or {}
    base = _prompt_gap_classify(skill, Path(""), target_os, module, gaps,
                                locs)
    decisions: list[dict] = []
    feedback = ""
    for attempt in range(1, MAX_TRIES + 1):
        rc, out = agent.run_agent(
            base + feedback, workdir=target_os,
            log_stem=str(p3m / "logs" / f"P3G_R{attempt}"),
            timeout_sec=AGENT_TIMEOUT_SEC)
        parsed = agent.extract_json(out) if rc == 0 else None
        if parsed and isinstance(parsed.get("decisions"), list):
            decisions = []
            errs = []
            covered = set()
            for d in parsed["decisions"]:
                if not isinstance(d, dict):
                    continue
                api, strat = d.get("linux_api"), d.get("strategy")
                if api not in {g["linux_api"] for g in gaps}:
                    errs.append(f"未知符号 {api}")
                    continue
                if strat not in ("bypass", "fill", "register-fill", "human"):
                    errs.append(f"{api}: strategy 非法 {strat}")
                    continue
                if not str(d.get("instruction", "")).strip() and \
                        strat != "human":
                    errs.append(f"{api}: 非 human 须有 instruction")
                    continue
                if strat in ("fill", "register-fill") and \
                        not _check_evidence(d.get("evidence", ""), target_os):
                    # fill 证据在树内定位补齐点（register-fill 可空）
                    if strat == "fill":
                        errs.append(f"{api}: fill 须有树内 evidence")
                        continue
                covered.add(api)
                decisions.append({"linux_api": api, "strategy": strat,
                                  "instruction": str(d.get("instruction",
                                                           "")),
                                  "evidence": str(d.get("evidence", ""))})
            missing = [g["linux_api"] for g in gaps
                       if g["linux_api"] not in covered]
            if not missing and not errs:
                break
            fb = []
            if missing:
                fb.append("仍缺决策：", ", ".join(missing))
            if errs:
                fb.append("问题：" + "; ".join(errs[:8]))
            feedback = ("\n\n---\n\n## 上一次输出的问题（修正后重输出完整 "
                        "JSON）\n" + "\n".join(fb))
        else:
            feedback = ("\n\n---\n\n## 上一次输出的问题\n未见合法 JSON。只输出"
                        "一个紧凑 JSON 对象（一行）。")
    if not decisions:
        print(f"[porter] P3: {module} gap 分类失败——exit 1")
        return 1
    dec_path.write_text(json.dumps(
        {"module": module, "generated": datetime.now().isoformat(
            timespec="seconds"), "decisions": decisions},
        ensure_ascii=False, indent=2), encoding="utf-8")
    # register-fill 登记平台补丁候选（planned，P7 决策）
    rf = [d for d in decisions if d["strategy"] == "register-fill"]
    if rf:
        pp_path = ws / "platform_patches.json"
        pp = json.loads(pp_path.read_text(encoding="utf-8")) \
            if pp_path.exists() else {"patches": []}
        known = {p.get("gap") for p in pp["patches"]}
        for d in rf:
            if d["linux_api"] in known:
                continue
            pp["patches"].append({
                "gap": d["linux_api"], "module": module,
                "status": "planned", "strategy": "register-fill",
                "instruction": d["instruction"], "evidence": d["evidence"],
                "registered": datetime.now().isoformat(timespec="seconds")})
        pp_path.write_text(json.dumps(pp, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    # #7 例外硬路由：register-fill 动平台代码（风险外溢）→ 转 human 关口
    # （平台补丁候选照登 P7 素材；人经 gap 关口表单确认最终处置）
    for d in rf:
        d["strategy"] = "human"
        if not str(d.get("instruction", "")).strip():
            d["instruction"] = "agent 初判 register-fill（动平台）——待人确认"
    human = [d["linux_api"] for d in decisions if d["strategy"] == "human"]
    if human:
        _panic_gap_gates(ws, module, human)
        print(f"[porter] P3: {module} 有 {len(human)} 条 gap 需人工决策"
              "——exit 3")
        return 3
    by = {}
    for d in decisions:
        by.setdefault(d["strategy"], []).append(d["linux_api"])
    print(f"[porter] P3: {module} gap 分类完成——" +
          "，".join(f"{k}: {len(v)}" for k, v in sorted(by.items())))
    return 0


def _panic_gap_gates(ws: Path, module: str, apis: list[str]) -> None:
    """human-gap 逐条登记 panic 关口（表单：strategy/instruction/rationale）。"""
    from . import gates
    for api in apis:
        gates.panic(ws, {
            "id": f"p3.gap.{module}.{api}", "kind": "decision",
            "gate_type": "decision", "phase": "P3", "module": module,
            "subject": api, "target": "gap",
            "question": (f"Linux API `{api}`（模块 {module}）在目标 OS 无"
                         "对应物且 agent 分类低置信。请裁定处置方式。"),
            "context_files": [f"P3/{module}/reports/gap_decisions.json",
                              "P2/mapping.md"],
            "answer_form": [
                {"field": "strategy", "type": "enum",
                 "options": ["bypass", "fill", "register-fill"],
                 "required": True},
                {"field": "instruction", "type": "text", "required": True,
                 "hint": "给 P4 的处置指令（如：丢弃 per-CPU 统计）"},
                {"field": "rationale", "type": "text", "required": True,
                 "hint": "理由（留档供批审与知识沉淀）"}],
            "applies_to": {"modules": [module]},
        })


# ---------- 步骤 5：判据草案 ----------

def _strategy_row(ws: Path, module: str) -> str:
    st = ws / "P1" / "strategy.md"
    if not st.exists():
        return ""
    for ln in st.read_text(encoding="utf-8").splitlines():
        if ln.startswith("|") and f"**{module}**" in ln:
            return ln
    return ""


def _step_criteria(ws: Path, module: str, p3m: Path, surface: dict) -> int:
    crit_path = p3m / "reports" / "criteria.json"
    if crit_path.exists():
        print(f"[porter] P3: {module} 判据已存在——复用")
        return 0
    row = _strategy_row(ws, module)
    skill = agent.load_skill("P3-criteria")
    st = surface["stats"]
    prompt = (f"{skill}\n\n---\n\n## 背景数据\n"
              f"- 模块：{module}；文件：{', '.join(surface['files'])}\n"
              f"- strategy.md §5 该模块行（验证方式原始描述）：\n"
              f"  {row or '（未找到——按模块内容推导）'}\n"
              f"- 使用面：OS-API {st['os_api']}（已映射 {st['mapped']} / "
              f"缺失 {st['missing']}）\n"
              f"- 模块物理文件根：{ws / 'P1' / 'modules' / module}\n"
              f"\n## 任务\n产出该模块的验收判据草案（紧凑 JSON 块，"
              f"criteria 数组）。基线 compile/boot 由脚本自动附加，"
              f"无需输出。消费者依赖的可观测项给 deferred_by。")
    final: list[dict] = []
    for attempt in range(1, MAX_TRIES + 1):
        rc, out = agent.run_agent(prompt, workdir=ws,
                                  log_stem=str(p3m / "logs" /
                                               f"P3C_R{attempt}"),
                                  timeout_sec=AGENT_TIMEOUT_SEC)
        parsed = agent.extract_json(out) if rc == 0 else None
        if parsed and isinstance(parsed.get("criteria"), list):
            final, errs = crit_mod.validate_criteria(parsed["criteria"],
                                                     module)
            if final and not errs:
                break
            if errs:
                prompt = prompt + (
                    "\n\n---\n\n## 上一次输出的问题（修正后重输出完整 JSON）\n"
                    + "; ".join(errs[:8]))
        else:
            prompt = prompt + (
                "\n\n---\n\n## 上一次输出的问题\n未见合法 JSON。只输出一个"
                "紧凑 JSON 对象（一行）。")
    if not final:
        print(f"[porter] P3: {module} 判据草案失败——exit 1")
        return 1
    crit = {"module": module,
            "generated": datetime.now().isoformat(timespec="seconds"),
            "criteria": crit_mod.baseline_criteria(module) + final}
    crit_path.write_text(json.dumps(crit, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    print(f"[porter] P3: {module} 判据草案 {len(crit['criteria'])} 条"
          f"（含基线 2）→ {crit_path}")
    return 0


# ---------- 步骤 6：探针（共享生命周期，见 probes.run_probe_lifecycle） ----------

def _step_probes(ws: Path, driver_root: Path, target_os: Path, module: str,
                 p3m: Path, proj: dict, surface: dict,
                 order: list[str]) -> int:
    """P3(M) 探针补新：只处理本模块面新增的风险主张（P2 预生成与其他
    模块已探测的 claim 自动去重跳过——probes.known_claims）。"""
    reg_path = p3m / "reports" / "probes.json"
    risky = probe_lib.filter_risky(ws, surface)
    claimed = ({p["claim"] for p in probe_lib.load_registry(reg_path)
                .get("probes", [])} |
               probe_lib.known_claims(ws, order, module))
    todo = [e for e in risky if e["linux_api"] not in claimed]
    if not todo and not probe_lib.load_registry(reg_path).get("probes"):
        print(f"[porter] P3: {module} 无新增风险条目（预生成/他模块已覆盖）"
              "——探针步骤跳过")
        return 0

    def _after_downgrade(ws_: Path, claims: list[str]) -> None:
        """降级项重过 gap 分类（带本模块使用位置上下文）。"""
        rc = _step_gap_decisions(ws_, target_os, module, p3m,
                                 _reload_surface(p3m))
        if rc != 0:
            print(f"[porter] P3: {module} 降级项 gap 分类 rc={rc}（loop "
                  "人工关口接管）")
            _step_probes.last_rc = rc      # 冒泡给调用方（见 run_p3）

    _step_probes.last_rc = 0
    rc = probe_lib.run_probe_lifecycle(
        ws, target_os, proj, order, reg_path,
        label=f"P3_{module}", todo_entries=todo,
        logs_dir=p3m / "logs", boot_ws=ws / "P3",
        usage_locs=surface.get("usage_locations") or {},
        after_downgrade=_after_downgrade)
    if rc != 0:
        return rc
    return int(_step_probes.last_rc or 0)


def _reload_surface(p3m: Path) -> dict:
    return json.loads((p3m / "reports" / "surface.json").read_text(
        encoding="utf-8"))


# ---------- 主入口 ----------

def run_p3(ws: Path, module: str, order: list[str]) -> int:
    try:                                # 观测扩全（H12）：P3 相位埋桩
        from . import events as _ev
        _ev.bind(ws, "p3")
    except Exception:
        pass
    ctx = _ctx(ws, module)
    if ctx is None:
        return 2
    driver_root, target_os, p3m, proj = ctx

    surface, rc = surface_mod.extract_surface(ws, driver_root, module)
    if rc != 0:
        return rc

    failed_batches = _step_missing_mapping(ws, driver_root, target_os,
                                           module, p3m, proj, surface)

    rc = _step_gap_decisions(ws, target_os, module, p3m, surface)
    if rc != 0:
        return rc

    rc = _step_criteria(ws, module, p3m, surface)
    if rc != 0:
        return rc

    rc = _step_probes(ws, driver_root, target_os, module, p3m, proj,
                      surface, order)
    if rc != 0:
        return rc

    # 刷新知识草稿（增量沉淀；不自动晋升）
    try:
        kn.draft_knowledge(ws)
    except Exception as e:
        print(f"[porter] P3: ⚠️ 知识草稿刷新失败（不影响主流程）：{e}")

    _write_report(ws, module, p3m, failed_batches)
    return 0 if not failed_batches else 1


def _write_report(ws: Path, module: str, p3m: Path,
                  failed_batches: list[str]) -> None:
    mapping = _load_mapping(ws / "P2")
    surface = _reload_surface(p3m)
    st = surface["stats"]
    dec = json.loads((p3m / "reports" / "gap_decisions.json").read_text(
        encoding="utf-8")) if (p3m / "reports" / "gap_decisions.json") \
        .exists() else {"decisions": []}
    crit = json.loads((p3m / "reports" / "criteria.json").read_text(
        encoding="utf-8")) if (p3m / "reports" / "criteria.json") \
        .exists() else {"criteria": []}
    by = {}
    for d in dec.get("decisions", []):
        by.setdefault(d["strategy"], []).append(d["linux_api"])
    reg = probe_lib.load_registry(p3m / "reports" / "probes.json")
    lines = [
        f"# P3({module}) 报告", "",
        f"- 时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 使用面：外部 {st['external']}（跨模块 {st['cross_module']} / "
        f"OS-API {st['os_api']} = 已映射 {st['mapped']} + 缺失 "
        f"{st['missing']}；噪音 {st['noise']}）",
        f"- 全局映射表累计：{len(mapping['entries'])} 条",
        f"- 失败批：{failed_batches or '无'}",
        f"- gap 处置：" +
        ("；".join(f"{k} {len(v)}（{', '.join(v[:8])}）"
                   for k, v in sorted(by.items())) if by else "无 gap"),
        f"- 判据：{len(crit.get('criteria', []))} 条（含基线 compile/boot）",
        f"- 探针：{len(reg.get('probes', []))} 个"
        f"（active {sum(1 for p in reg.get('probes', []) if p.get('status') == 'active')}）",
        "",
        "## 判据明细", "",
    ]
    for c in crit.get("criteria", []):
        db = f"｜deferred_by {','.join(c['deferred_by'])}" \
            if c.get("deferred_by") else ""
        lines.append(f"- [{c['id']}] {c['layer']}/{c['kind']} "
                     f"expr=`{c['expr']}`{db}")
    (p3m / "reports" / "report.md").write_text("\n".join(lines) + "\n",
                                               encoding="utf-8")
    print(f"[porter] P3: {module} 完成——报告 → "
          f"{p3m / 'reports' / 'report.md'}")
