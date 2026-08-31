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

import json
from datetime import datetime
from pathlib import Path

from ..common import agent
from ..env import probe as probe_mod
from . import probes as probe_lib

AGENT_TIMEOUT_SEC = 1200          # 迁移调用较大，给足预算
MAX_TRIES = 3                     # 每片迁移：首发 + 带反馈重试 2 次
MAX_LINES_PER_SLICE = 900         # 单迁移调用输入上限（32K 教训）


def _ctx(ws: Path, module: str) -> tuple[Path, Path, Path, dict, dict] | None:
    for need in (ws / "project.json", ws / "runner.json",
                 ws / "P3" / module / "reports" / "surface.json",
                 ws / "P3" / module / "reports" / "criteria.json",
                 ws / "P3" / module / "reports" / "gap_decisions.json"):
        if not need.exists():
            print(f"[porter] P4: 缺少 {need}（先跑 p3 {module}）")
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
        print(f"[porter] P4: fill {api} …")
        lines = (f"- {api}：缺什么/绕过候选={d.get('instruction', '')[:200]}；"
                 f"P3 建议 evidence={d.get('evidence', '')}")
        prompt = (f"{skill}\n\n---\n\n## 背景数据\n"
                  f"- 目标 OS 源码树：`{target_os}` = 你的工作目录\n"
                  f"- 驱动 crate：`{target_os / 'kernel' / 'core' / 'comps' / driver}`"
                  f"（平台补齐不得写进驱动 crate）\n"
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
                probe_lib.sync_probes_rs(target_os, driver, sections)
            b = probe_mod.probe_build(ws / "P4", target_os, runner,
                                      label=f"P4_{module}_fill_build_{api}")
            boot_ok = False
            log = ""
            if b["ok"]:
                boot_ok, log = probe_lib.boot_and_log(
                    ws, "P4", target_os, proj,
                    f"P4_{module}_fill_boot_{api}")
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
                print(f"[porter] P4: fill {api} 三重验证 FAIL——回退 bypass")
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
    failures: list[dict] = []

    for f, start, end in slices:
        key = (f.name, start, end)
        if key in done_keys:
            continue
        # 每片重算 existing 与行数快照（2026-08-30 事故：一次性快照误导
        # moved_2 片以为 hw_defs.rs 不存在而"新建"覆盖前片 1472 行成果）
        existing = sorted(p.name for p in (crate / "src").glob("*.rs")) \
            if (crate / "src").exists() else []
        before_lines = _src_line_counts(crate)
        print(f"[porter] P4: 迁移切片 {f.name}:{start}-{end} …")
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
                  f"完成后输出紧凑 JSON 块。")
        ok = False
        err_info = ""
        blocked = False
        for attempt in range(1, MAX_TRIES + 1):
            rc, out = agent.run_agent(
                prompt + err_info, workdir=target_os,
                log_stem=str(p4m / "logs" /
                             f"MIG_{f.name}_{start}_R{attempt}"),
                timeout_sec=AGENT_TIMEOUT_SEC)
            parsed = agent.extract_json(out) if rc == 0 else None
            if parsed and parsed.get("status") == "blocked":
                blocked = True
                print(f"[porter] P4: 切片 {f.name}:{start}-{end} 被agent报"
                      f" blocked：{str(parsed.get('notes', ''))[:200]}"
                      "——停车（映射问题走人工）")
                break
            if parsed and parsed.get("status") == "done":
                b = probe_mod.probe_build(ws / "P4", target_os, runner,
                                          label=f"P4_{module}_"
                                                f"{f.name}_{start}")
                if b["ok"]:
                    shrunk = _shrunk_files(before_lines,
                                           _src_line_counts(crate))
                    if shrunk:
                        print(f"[porter] P4: 切片 {f.name}:{start}-{end} "
                              f"覆盖了既有内容（{'; '.join(shrunk)}）——判 FAIL")
                        err_info = ("\n\n---\n\n## 上一次的严重问题（本片 "
                                    "重做）\n你覆盖/删除了既有文件内容："
                                    f"{'; '.join(shrunk)}。契约是**只追加、"
                                    "不动别片已写内容**。请把被删内容完整"
                                    "恢复后重做本片（重新输出完整 JSON）。")
                        continue
                    ok = True
                    break
                log_path = ws / "P4" / "logs" / f"P4_{module}_{f.name}_" \
                                                f"{start}.log"
                err_tail = ""
                if log_path.exists():
                    err_tail = "\n".join(
                        log_path.read_text(encoding="utf-8",
                                           errors="replace").splitlines()
                        [-40:])
                err_info = ("\n\n---\n\n## 上一次构建失败（修复后重做本片）\n"
                            f"```\n{err_tail}\n```")
            else:
                err_info = ("\n\n---\n\n## 上一次输出的问题\n未报告完成。"
                            "修复后重做本片，输出紧凑 JSON。")
        mig["slices"].append({"file": f.name, "start": start, "end": end,
                              "ok": ok, "blocked": blocked})
        mig_path.write_text(json.dumps(mig, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        if blocked:
            failures.append({"file": f.name, "start": start, "end": end,
                             "blocked": True})
            break
        if not ok:
            failures.append({"file": f.name, "start": start, "end": end})
            print(f"[porter] P4: 切片 {f.name}:{start}-{end} {MAX_TRIES} 次"
                  "仍失败——停止本模块后续切片")
            break
    return (0 if not failures else 1), mig["slices"]


# ---------- 步骤 3：轮末快速冒烟（防毒化闸门） ----------

def _quick_smoke(ws: Path, target_os: Path, module: str, proj: dict,
                 runner: dict) -> bool:
    """compile + boot 双信号快速冒烟：保证每轮结束留下可启动树。

    仅判 build/boot 可用性（~10s 启动闸门）；判据级验收归 P5(M)。
    """
    b = probe_mod.probe_build(ws / "P4", target_os, runner,
                              label=f"P4_{module}_smoke_build")
    if not b["ok"]:
        print(f"[porter] P4: 轮末快速冒烟 FAIL（build）：{b['detail']}")
        return False
    boot_ok, _log = probe_lib.boot_and_log(ws, "P4", target_os, proj,
                                           f"P4_{module}_smoke_boot")
    if not boot_ok:
        print("[porter] P4: 轮末快速冒烟 FAIL（boot 双信号）")
        return False
    print("[porter] P4: 轮末快速冒烟 PASS（build+boot）——留下可启动树")
    return True


# ---------- 主入口 ----------

def run_p4(ws: Path, module: str, order: list[str]) -> int:
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

    if not _quick_smoke(ws, target_os, module, proj, runner):
        return 1        # 冒烟失败：attempts 由 run.py 统一 bump 并判界
    return 0
