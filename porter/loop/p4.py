"""p4.py — P4(M) 迁移（fill 统一 + 切片迁移 + 轮末快速冒烟）（plan §3.3）。

方案 A 相位重构（2026-08-31）：验收段（原步骤 3/4）整体剥离到 p5.py
（P5(M) 模块级验收）；P4 专注生产，只留防毒化闸门。

步骤：
  1. fill 统一阶段      按 P3 gap_decisions 中 strategy=fill 的条目批量
                        平台补齐（P4-gap-fill skill：加法式扩展铁律）→
                        build+boot+专属探针三重验证 → 失败回退 bypass →
                        登记 platform_patches.json
  2. 迁移阶段           模块文件 × ≤900 行切片 → agent（P4-migrate：
                        映射作数据注入，只翻译不研究）→ 每片后 build
                        （FAIL 带编译错误反馈重试 ≤3）→ 中途新撞 gap
                        现场分类（bypass/及时 fill/皆败 attempts++）
  3. 轮末快速冒烟       compile+boot 双信号（~10s 启动闸门）——保证每轮
                        结束留下可启动树，防半成品毒化后续模块

产物：P4/<M>/reports/{fill.json, migration.json, report.md}
返回：0 成功 / 1 失败 / 2 前置缺失。
（模块级验收 → P5(M)；相位推进 p4→p5 由调用方负责。）
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from ..common import agent
from ..env import probe as probe_mod
from . import gates, probes as probe_lib
from .. import log as _log

AGENT_TIMEOUT_SEC = 1200          # 迁移调用较大，给足预算
MAX_TRIES = 3                     # 每片迁移：首发 + 带反馈重试 2 次
MAX_LINES_PER_SLICE = 900         # 单迁移调用输入上限（32K 教训）
SAME_SIG_REPEAT = 2               # 同签名失败连发阈值（零进展 → panic）
# split_long_op（run_agent_seq）总预算 = 老单段上限 ×2；编译（静态段）
# 时长在预算之外——这正是拆分的目的（R1 整轮 TIMEOUT 的教训）
SEQ_BUDGET_SEC = 2400


def _ctx(ws: Path, module: str) -> tuple[Path, Path, Path, dict, dict] | None:
    for need in (ws / "project.json", ws / "runner.json",
                 ws / "P3" / module / "reports" / "surface.json",
                 ws / "P3" / module / "reports" / "criteria.json",
                 ws / "P3" / module / "reports" / "gap_decisions.json"):
        if not need.exists():
            _log.console_line(f"[porter] P4: 缺少 {need}（先跑 p3 {module}）")
            return None
    proj = json.loads((ws / "project.json").read_text(encoding="utf-8"))
    runner = json.loads((ws / "runner.json").read_text(encoding="utf-8"))
    target_os = Path(proj["target_os"])
    p4m = ws / "P4" / module
    (p4m / "logs").mkdir(parents=True, exist_ok=True)
    (p4m / "reports").mkdir(parents=True, exist_ok=True)
    return Path(proj["linux_driver"]), target_os, p4m, proj, runner


# ---------- 步骤 1：fill 统一阶段 ----------

def _step_fill(ws: Path, driver_root: Path, target_os: Path, module: str,
               p4m: Path, proj: dict, runner: dict,
               order: list[str]) -> int:
    dec = json.loads((ws / "P3" / module / "reports" /
                      "gap_decisions.json").read_text(encoding="utf-8"))
    fills = [d for d in dec.get("decisions", []) if d["strategy"] == "fill"]
    fill_path = p4m / "reports" / "fill.json"
    done = {}
    if fill_path.exists():
        done = json.loads(fill_path.read_text(encoding="utf-8")).get(
            "results", {})
    if not fills:
        fill_path.write_text(json.dumps({"results": done},
                                        ensure_ascii=False, indent=2),
                             encoding="utf-8")
        return 0
    driver = Path(proj["linux_driver"]).name
    skill = agent.load_skill("P4-gap-fill")
    from ..bootstrap import kb as _kb
    kb_dir = _kb.kb_dir_for(ws)
    pp_path = ws / "platform_patches.json"
    pp = json.loads(pp_path.read_text(encoding="utf-8")) \
        if pp_path.exists() else {"patches": []}
    from ..bootstrap.mapping import _load_mapping, _save
    mapping = _load_mapping(ws / "P2")
    index = {e["linux_api"]: e for e in mapping["entries"]}
    reg_path = p4m / "reports" / "fill_probes.json"

    for d in fills:
        api = d["linux_api"]
        if api in done and done[api].get("status") in ("filled", "fell-back"):
            continue
        _log.console_line(f"[porter] P4: fill {api} …")
        lines = (f"- {api}：缺什么/绕过候选={d.get('instruction', '')[:200]}；"
                 f"P3 建议 evidence={d.get('evidence', '')}")
        # gaps 域历史记录（文件名存在性；fill 曾失败者 agent 必读）
        try:
            from ..bootstrap import gaps as gaps_kb
            prior = gaps_kb.prior_entry(kb_dir, api)
            if prior is not None:
                lines += (f"\n- ⚠ 历史记录：该 API 在先前迁移有 gap 处置/"
                          f"fill 记录（{prior}）——动手前先读它，"
                          f"历史 fill 失败原因必须正面回应而非重蹈")
        except Exception:
            pass
        crate = target_os / "kernel" / "core" / "comps" / driver
        prompt = (f"{skill}\n\n---\n\n## 背景数据\n"
                  f"- 目标 OS 源码树：`{target_os}` = 你的工作目录\n"
                  f"- 驱动 crate：`{crate}`\n"
                  f"- 平台补齐落点：`{crate}/src/external_interfaces.rs`"
                  "（骨架已预置 mod；补写的平台代码一律写进该 mod——"
                  "按主题拆子模块函数，禁止散写目标树其他位置；"
                  "仅当确需加依赖/接线时才允许改接线文件：根 Cargo.toml、"
                  "Components.toml、kernel/core/Cargo.toml）\n"
                  f"- 使用方模块：{module}\n\n## gap 条目\n{lines}\n"
                  f"\n## 任务\n加法式补齐该能力，输出紧凑 JSON 块"
                  f"（patch_summary/files/evidence/reason/probe）。")
        patch = None
        for attempt in range(1, MAX_TRIES + 1):
            rc, out = agent.run_agent(
                prompt, workdir=target_os,
                log_stem=str(p4m / "logs" / f"FILL_{api}_R{attempt}"),
                timeout_sec=AGENT_TIMEOUT_SEC)
            parsed = agent.extract_json(out) if rc == 0 else None
            if parsed and parsed.get("patch_summary"):
                patch = parsed
                break
            prompt += ("\n\n---\n\n## 上一次输出的问题\n未见合法 JSON。"
                       "只输出一个紧凑 JSON 对象（一行）。")
        status = "fell-back"
        if patch:
            probes_new, _e = probe_lib.validate_probes(
                patch.get("probe") and [patch["probe"]] or [])
            reg = probe_lib.load_registry(reg_path)
            if probes_new:
                reg["probes"] = [p for p in reg["probes"]
                                 if p["claim"] != api]
                reg["probes"].append(probes_new[0])
                probe_lib.save_registry(reg_path, reg)
                sections = probe_lib.collect_sections(ws, order, module,
                                                      reg_path, kind="P4")
                probe_lib.sync_probes(ws, target_os, driver, sections)
            b = probe_mod.probe_build(ws / "P4", target_os, runner,
                                      label=f"P4_{module}_fill_build_{api}")
            boot_ok = False
            log = ""
            if b["ok"]:
                boot_ok, log, log_state = probe_lib.boot_and_log(
                    ws, "P4", target_os, proj,
                    f"P4_{module}_fill_boot_{api}")
                if log_state == "missing":
                    # 抢占（H9 重构）：判定输入不存在，infra 关口已登记
                    _log.console_line(f"[porter] P4: fill {api} 验证中止（boot 日志"
                          "不可得）——exit 3")
                    return 3
            names = [p["name"] for p in probe_lib.load_registry(reg_path)
                     .get("probes", []) if p["status"] == "active"]
            verdicts = probe_lib.judge(log, [n for n in names]) if log else {}
            fill_probe_ok = (b["ok"] and boot_ok and
                             (not probes_new or
                              verdicts.get(probes_new[0]["name"]) == "ok"))
            if fill_probe_ok:
                status = "filled"
                e = index.get(api)
                if e:
                    e["verdict"] = "adapt"
                    ev = patch.get("evidence", "")
                    if ev and ev not in e.get("evidence", ""):
                        e["evidence"] = (e.get("evidence", "") +
                                         (";" if e.get("evidence") else "")
                                         + ev)
                    e["notes"] = (e["notes"].rstrip() +
                                  f"｜fill(P4{module}): "
                                  f"{patch['patch_summary'][:160]}"
                                  ).lstrip("｜")
                    e["confidence"] = "high"
            else:
                _log.console_line(f"[porter] P4: fill {api} 三重验证 FAIL——回退 bypass")
        done[api] = {"status": status,
                     "patch": patch,
                     "time": datetime.now().isoformat(timespec="seconds")}
        pp["patches"] = [p for p in pp["patches"] if p.get("gap") != api]
        pp["patches"].append({"gap": api, "module": module,
                              "status": status,
                              "files": (patch or {}).get("files", []),
                              "evidence": (patch or {}).get("evidence", ""),
                              "reason": (patch or {}).get("reason", ""),
                              "time": done[api]["time"]})
        pp_path.write_text(json.dumps(pp, ensure_ascii=False, indent=2),
                           encoding="utf-8")
        fill_path.write_text(json.dumps({"results": done},
                                        ensure_ascii=False, indent=2),
                             encoding="utf-8")
    _save(mapping, ws / "P2")
    return 0


# ---------- 步骤 2：迁移 ----------

def _slices(files: list[Path], max_lines: int = MAX_LINES_PER_SLICE
            ) -> list[tuple[Path, int, int]]:
    """(文件, 起 1-based 行, 止行) 切片序列。"""
    out: list[tuple[Path, int, int]] = []
    for f in files:
        n = sum(1 for _ in f.open(encoding="utf-8", errors="replace"))
        for start in range(1, n + 1, max_lines):
            out.append((f, start, min(start + max_lines - 1, n)))
    return out


def _src_line_counts(crate: Path) -> dict[str, int]:
    """crate src/ 各 .rs 行数快照（跨片内容守卫用）。"""
    src = crate / "src"
    if not src.is_dir():
        return {}
    return {p.name: sum(1 for _ in p.open(encoding="utf-8",
                                          errors="replace"))
            for p in sorted(src.glob("*.rs"))}


def _shrunk_files(before: dict[str, int], after: dict[str, int],
                  tolerance: int = 5) -> list[str]:
    """既有文件行数显著缩水清单（P4-migrate 契约 = 只追加不动别片）。"""
    return [f"{name} {before[name]}→{after.get(name, 0)}"
            for name in before
            if before[name] - after.get(name, 0) > tolerance]


def _mapping_data_block(ws: Path, surface: dict, module: str) \
        -> tuple[str, str]:
    """该模块的映射数据渲染（P4 只翻译不研究：全部所需条目打包注入）。"""
    mapping = json.loads((ws / "P2" / "mapping.json").read_text(
        encoding="utf-8"))
    entries = {e["linux_api"]: e for e in mapping.get("entries", [])}
    dec = json.loads((ws / "P3" / module / "reports" /
                      "gap_decisions.json").read_text(encoding="utf-8"))
    instr = {d["linux_api"]: d for d in dec.get("decisions", [])}
    syms = [s for v in (surface.get("mapped_by_verdict") or {}).values()
            for s in v] + [s for v in (surface.get("missing_by_domain")
                                       or {}).values() for s in v]
    lines = []
    for s in sorted(set(syms)):
        e = entries.get(s)
        if not e:
            continue
        d = instr.get(s)
        extra = f"｜处置({d['strategy']}): {d['instruction'][:120]}" if d else ""
        if e["verdict"] == "not-migrated":
            lines.append(f"- {s}: not-migrated（不迁）")
            continue
        lines.append(f"- {s}: {e['verdict']} → {e['target'][:180]}"
                     f"｜evidence={e['evidence'][:100]}"
                     f"｜notes={(e['notes'] or '')[:150]}{extra}")
    redesigns = mapping.get("redesigns") or []
    rd = "\n".join(f"- {r.get('linux_pattern', r.get('id'))}: "
                   f"{r.get('target_approach', '')[:200]}" for r in redesigns)
    return "\n".join(lines), rd


def _step_migrate(ws: Path, driver_root: Path, target_os: Path, module: str,
                  p4m: Path, proj: dict, runner: dict,
                  surface: dict) -> tuple[int, list[dict]]:
    mdir = ws / "P1" / "modules" / module
    files = sorted(f for f in mdir.glob("*") if f.suffix in (".c", ".h"))
    slices = _slices(files)
    driver = Path(proj["linux_driver"]).name
    crate = target_os / "kernel" / "core" / "comps" / driver
    crit = json.loads((ws / "P3" / module / "reports" / "criteria.json")
                      .read_text(encoding="utf-8"))
    unit_tests = [c for c in crit["criteria"]
                  if c["kind"] == "unit_test" and not c.get("deferred_by")]
    ut_block = "\n".join(
        f"- 测试函数名：{c['expr']}（判据 id {c['id']}）" for c in unit_tests)
    l3_crits = [c for c in crit["criteria"]
                if c["kind"] in ("log_pattern", "counter")
                and not c.get("deferred_by")]
    l3_block = "\n".join(
        f"- [{c['id']}] 正则 `{c['expr']}`" for c in l3_crits)
    map_block, redesign_block = _mapping_data_block(ws, surface, module)
    mod_json = json.loads((mdir / "module.json").read_text(encoding="utf-8"))
    skill = agent.load_skill("P4-migrate")

    mig_path = p4m / "reports" / "migration.json"
    mig = json.loads(mig_path.read_text(encoding="utf-8")) \
        if mig_path.exists() else {"slices": []}
    done_keys = {(s["file"], s["start"], s["end"]) for s in mig["slices"]}
    sig_counts: dict[str, int] = mig.setdefault("sig_counts", {})
    failures: list[dict] = []
    hard_stop = False              # blocked / 同签名连发 → 立即 panic（H13）

    for f, start, end in slices:
        key = (f.name, start, end)
        if key in done_keys:
            continue
        # 每片重算 existing 与行数快照（2026-08-30 事故：一次性快照误导
        # moved_2 片以为 hw_defs.rs 不存在而"新建"覆盖前片 1472 行成果）
        existing = sorted(p.name for p in (crate / "src").glob("*.rs")) \
            if (crate / "src").exists() else []
        before_lines = _src_line_counts(crate)
        _log.console_line(f"[porter] P4: 迁移切片 {f.name}:{start}-{end} …")
        prompt = (f"{skill}\n\n---\n\n## 背景数据\n"
                  f"- 驱动 crate：`{crate}`（src/ 现有 {existing}）\n"
                  f"- 目标 OS 源码树：`{target_os}` = 你的工作目录\n"
                  f"- 模块：{module}——{mod_json.get('function', '')}\n"
                  f"- 本切片源文件：`{f}`（模块物理切分文件）第 {start}-"
                  f"{end} 行\n"
                  f"\n## 映射表（给定数据，只翻译不研究——禁止自行检索/"
                  f"臆造目标 API）\n{map_block}\n"
                  f"\n## 换思路裁定（全局）\n{redesign_block or '无'}\n"
                  f"\n## 需落的单元测试（ktest，写进本模块代码）\n"
                  f"{ut_block or '本切片无需（判据属其他切片/无）'}\n"
                  f"\n## L3 可观测判据（本模块验收将按正则查启动日志）\n"
                  f"{l3_block or '无'}\n"
                  + ("若上表非空：迁移须在组件/probe 初始化路径接线**对真实"
                     "设备的调用**（QEMU 已挂本驱动设备），使启动日志出现"
                     "满足正则的行；日志措辞自行设计但必须命中正则。\n"
                     if l3_crits else "")
                  + f"\n## 任务\n把本切片重写为安全 Rust 进驱动 crate"
                  f"（目标文件命名贴近模块职责，如 {module.replace('-', '_')}"
                  f".rs；首片建 mod 声明于 lib.rs，后续片只追加内容）。"
                  f"完成后按运行协议输出 JSON 块（status/files/notes）。")

        # ---- split_long_op（2026-09-04 接线）：切片迁移的
        # 「agent→build→拼 err_info 反馈重试」老序列整体替换为
        # run_agent_seq 一次调用：agent 段同会话续接（opencode --session，
        # 无信息损失）；编译=静态段（独立时长，不吃 agent 预算）；
        # 行数守卫并入静态段（覆盖事故 → 静态失败 → 注入反馈修复）。
        label = f"P4_{module}_{f.name}_{start}"
        build_log_path = ws / "P4" / "logs" / f"{label}.log"

        def _static(label=label, before_lines=before_lines) \
                -> tuple[bool, str]:
            b = probe_mod.probe_build(ws / "P4", target_os, runner,
                                      label=label)
            out_text = ""
            if build_log_path.exists():
                out_text = build_log_path.read_text(
                    encoding="utf-8", errors="replace")
            if not b["ok"]:
                return False, out_text or f"build FAIL: {b.get('detail', '')}"
            shrunk = _shrunk_files(before_lines, _src_line_counts(crate))
            if shrunk:
                return False, ("你覆盖/删除了既有文件内容："
                               + "; ".join(shrunk)
                               + "。契约是**只追加、不动别片已写内容**。"
                               "请把被删内容完整恢复后重做本片。")
            return True, out_text

        seq = agent.run_agent_seq(
            prompt, workdir=target_os,
            log_stem=str(p4m / "logs" / f"MIG_{f.name}_{start}"),
            static={"describe": "编译验证（docker 内 make kernel）",
                    "fn": _static},
            gen_schema={"status": "str", "files": "list", "notes": "str"},
            final_static=True,          # done 后编排器强制编译（同旧语义）
            agent_budget_sec=SEQ_BUDGET_SEC,
            task={"phase": "p4", "module": module, "step": "migrate"})
        parsed = seq.get("parsed") or {}
        blocked = parsed.get("status") == "blocked"
        ok = seq["status"] == "done" and not blocked
        attempt = len(seq["rounds"]) or 1
        sig = ""
        if blocked:
            hard_stop = True
            _log.console_line(f"[porter] P4: 切片 {f.name}:{start}-{end} 被agent报"
                  f" blocked：{str(parsed.get('notes', ''))[:200]}"
                  "——立即停车（映射问题走人工，不烧 attempts）")
            gates.panic(ws, {
                "id": f"p4.blocked.{module}.{f.name}-{start}",
                "kind": "decision", "gate_type": "decision",
                "phase": "P4", "module": module,
                "question": (
                    f"切片 {f.name}:{start}-{end} 迁移中 agent 报映射"
                    "不可用——P2/P3 的前提可能错了。请重新裁定该 API "
                    "的映射与处置（改映射/换思路/加平台补丁）。"),
                "context_files": [f"P4/{module}/logs/",
                                  "P2/mapping.md"],
                "answer_form": [
                    {"field": "instruction", "type": "text",
                     "required": True,
                     "hint": "新的处置指令（将回写 gap_decisions 与 "
                             "mapping notes）"},
                    {"field": "rationale", "type": "text",
                     "required": True}],
                "applies_to": {"modules": [module]},
            })
        elif seq["status"] == "stalled":
            # 段内防打转早退（连续 2 次同签名静态失败）：映射到既有
            # 同签名 panic 关口（跨切片 sig_counts 亦保留，见下）
            hard_stop = True
            _log.console_line(f"[porter] P4: 切片 {f.name}:{start}-{end} "
                  "静态段同签名失败连发（零进展）——panic")
            gates.panic(ws, {
                "id": f"p4.slice_sig.{module}.{f.name}-{start}",
                "kind": "retry", "gate_type": "failure",
                "phase": "P4", "module": module, "step": "p4",
                "question": (
                    f"切片 {f.name}:{start}-{end} 编译失败呈"
                    "同签名连发（agent 修不动同一错误）——"
                    "错误超出该切片的自动修复能力。日志尾"
                    f"40 行见 {build_log_path}。"),
                "context_files": [str(build_log_path)],
                "answer_form": [
                    {"field": "note", "type": "text",
                     "required": False,
                     "hint": "诊断笔记（人工定位的根因与修复）"}],
                "applies_to": {"modules": [module]},
            })
        if not ok and not hard_stop:
            # 跨切片同签名计数（老机制保留）：本片失败时读构建日志
            # 尾 40 行计数，同签名跨切片连发 → panic（防换片打转）
            err_tail = ""
            if build_log_path.exists():
                err_tail = "\n".join(
                    build_log_path.read_text(encoding="utf-8",
                                             errors="replace").splitlines()
                    [-40:])
            sig = hashlib.sha1(
                err_tail.encode("utf-8")).hexdigest()[:12] \
                if err_tail.strip() else ""
            if sig:
                sig_counts[sig] = sig_counts.get(sig, 0) + 1
                if sig_counts[sig] >= SAME_SIG_REPEAT:
                    hard_stop = True
                    _log.console_line(f"[porter] P4: 切片 {f.name}:{start}-{end} "
                          f"同签名失败跨切片连发 {sig_counts[sig]} 次"
                          "（零进展）——panic")
                    gates.panic(ws, {
                        "id": f"p4.slice_sig.{module}.{f.name}-{start}",
                        "kind": "retry", "gate_type": "failure",
                        "phase": "P4", "module": module, "step": "p4",
                        "question": (
                            f"切片 {f.name}:{start}-{end} 编译失败呈"
                            "同签名跨切片连发（agent 修不动同一错误）——"
                            "错误超出该切片的自动修复能力。日志尾"
                            f"40 行见 {build_log_path}。"),
                        "context_files": [str(build_log_path)],
                        "answer_form": [
                            {"field": "note", "type": "text",
                             "required": False,
                             "hint": "诊断笔记（人工定位的根因与修复）"}],
                        "applies_to": {"modules": [module]},
                    })
        mig["slices"].append({"file": f.name, "start": start, "end": end,
                              "ok": ok, "blocked": blocked})
        mig_path.write_text(json.dumps(mig, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        if ok and attempt >= 2:
            # 类 3 钩子：切片 FAIL→PASS 翻转——错误→修复原始留痕（B3；
            # 蒸馏归人工，CP5 审核面处置）
            try:
                from ..bootstrap import candidates as _cand
                _cand.record_candidate(
                    ws, hook="slice-rework", ref=f"{module}/{f.name}:{start}",
                    draft=f"切片 {f.name}:{start}-{end} 失败后第 "
                          f"{attempt} 段重试成功（错误尾签名 {sig or '—'}）"
                          f"——错误与修复对话见 P4/{module}/logs/"
                          f"MIG_{f.name}_{start}_S1..S{attempt}",
                    evidence=[f"P4/{module}/logs/"],
                    suggested="pitfalls",
                    scope_extra={"module": module})
            except Exception:
                pass
        if blocked:
            failures.append({"file": f.name, "start": start, "end": end,
                             "blocked": True})
            break
        if not ok:
            failures.append({"file": f.name, "start": start, "end": end})
            _log.console_line(f"[porter] P4: 切片 {f.name}:{start}-{end} "
                  f"预算 {SEQ_BUDGET_SEC}s 内未完成（{seq['status']}）"
                  "——停止本模块后续切片")
            break
    # hard_stop（blocked / 同签名连发）→ rc 3：立即停车不烧 attempts（H13）
    rc_out = 3 if hard_stop else (0 if not failures else 1)
    return rc_out, mig["slices"]


# ---------- 步骤 3：轮末快速冒烟（防毒化闸门） ----------

def _quick_smoke(ws: Path, target_os: Path, module: str, proj: dict,
                 runner: dict) -> bool | str:
    """compile + boot 双信号快速冒烟：保证每轮结束留下可启动树。

    仅判 build/boot 可用性（~10s 启动闸门）；判据级验收归 P5(M)。
    返回 True/False；"infra" = 日志不可得（调用方 rc 3，不烧 attempts）。
    """
    b = probe_mod.probe_build(ws / "P4", target_os, runner,
                              label=f"P4_{module}_smoke_build")
    if not b["ok"]:
        _log.console_line(f"[porter] P4: 轮末快速冒烟 FAIL（build）：{b['detail']}")
        return False
    boot_ok, _raw_log, log_state = probe_lib.boot_and_log(
        ws, "P4", target_os, proj, f"P4_{module}_smoke_boot")
    if log_state == "missing":
        return "infra"            # 抢占：infra 关口已登记
    if not boot_ok:
        _log.console_line("[porter] P4: 轮末快速冒烟 FAIL（boot 双信号）")
        return False
    _log.console_line("[porter] P4: 轮末快速冒烟 PASS（build+boot）——留下可启动树")
    return True


# ---------- 主入口 ----------

def run_p4(ws: Path, module: str, order: list[str]) -> int:
    try:                                # 观测扩全（H12）：P4 相位埋桩
        from . import events as _ev
        _ev.bind(ws, "p4")
        from ..log import core as _log
        _log.phase_begin("p4", module=module, store_only=True)
    except Exception:
        pass
    ctx = _ctx(ws, module)
    if ctx is None:
        return 2
    driver_root, target_os, p4m, proj, runner = ctx
    surface = json.loads((ws / "P3" / module / "reports" / "surface.json")
                         .read_text(encoding="utf-8"))

    rc = _step_fill(ws, driver_root, target_os, module, p4m, proj, runner,
                    order)
    if rc != 0:
        return rc

    rc, _slices_done = _step_migrate(ws, driver_root, target_os, module,
                                     p4m, proj, runner, surface)
    if rc != 0:
        return rc        # 切片失败已在 migration.json 留痕；attempts 由
        # run.py 统一 bump 并判界

    smoke = _quick_smoke(ws, target_os, module, proj, runner)
    if smoke == "infra":
        return 3                 # infra 关口停车（不烧 attempts）
    if not smoke:
        return 1        # 冒烟失败：attempts 由 run.py 统一 bump 并判界
    try:
        from ..log import core as _log
        _log.phase_end("p4", module=module, rc=0, store_only=True)
    except Exception:
        pass
    try:                                # vcs：P4(M) 末目标树 commit（fill+migrate）
        from ..common import vcs as _vcs
        driver = Path(proj["linux_driver"]).name
        _vcs.commit_target(
            ws, f"P4[{module}]: fill + migrate",
            paths=[f"kernel/core/comps/{driver}", *_vcs.TARGET_WIRING_FILES],
            phase="P4")
    except Exception:
        pass
    return 0
