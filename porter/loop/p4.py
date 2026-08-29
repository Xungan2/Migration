"""p4.py — P4(M) 迁移 + 分层验收（plan §3.3）。

步骤：
  0. unit_test 节回填   runner.json 缺通用 `unit_test` 节时做一次性
                        agent 补探（P0-unit-test-discover；reviewed:false）
  1. fill 统一阶段      按 P3 gap_decisions 中 strategy=fill 的条目批量
                        平台补齐（P4-gap-fill skill：加法式扩展铁律）→
                        build+boot+专属探针三重验证 → 失败回退 bypass →
                        登记 platform_patches.json
  2. 迁移阶段           模块文件 × ≤900 行切片 → agent（P4-migrate：
                        映射作数据注入，只翻译不研究）→ 每片后 build
                        （FAIL 带编译错误反馈重试 ≤3）→ 中途新撞 gap
                        现场分类（bypass/及时 fill/皆败 attempts++）
  3. 分层验收           L1 build / L2 boot / L0 unit_test / L3 log_pattern
                        （qemu.log regex）+ 累积回归（已 done 模块全部
                        log_pattern 判据重查；unit_test 全 crate 覆盖）
  4. deferred 登记/清偿 deferred_by ⊆ done 的当场清偿；无法清偿 exit 3

产物：P4/<M>/reports/{fill.json, migration.json, acceptance.json, report.md}
返回：0 成功 / 1 失败 / 2 前置缺失 / 3 需人工。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from ..common import agent
from ..env import probe as probe_mod
from . import criteria as crit_mod
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


# ---------- 步骤 0：unit_test 节回填 ----------

def _ensure_unit_test(ws: Path, target_os: Path, proj: dict,
                      runner: dict) -> dict:
    ut = runner.get("unit_test")
    if ut:
        return ut
    print("[porter] P4: runner.json 缺 unit_test 节——一次性补探回填")
    skill = agent.load_skill("P0-unit-test-discover")
    driver = Path(proj["linux_driver"]).name
    prompt = (f"{skill}\n\n---\n\n## 背景数据\n"
              f"- 目标 OS 源码树：`{target_os}` = 你的工作目录\n"
              f"- 驱动 crate：aster-{driver}（kernel/core/comps/{driver}）\n"
              f"- 既有 build 命令形态（容器包裹）参考：\n"
              f"  {runner['build']['cmd']}\n"
              f"\n## 任务\n探明目标 OS 的内核态单元测试机制并输出紧凑 "
              f"JSON 块。")
    rc, out = agent.run_agent(prompt, workdir=target_os,
                              log_stem=str(ws / "P4" / "logs" /
                                            "unit_test_discover_R1"),
                              timeout_sec=900)
    parsed = agent.extract_json(out) if rc == 0 else None
    ut = None
    if parsed and "cmd" in parsed:
        ut = {k: parsed[k] for k in ("mechanism", "cmd", "timeout_sec",
                                     "success_pattern", "fail_pattern",
                                     "scope_hint") if k in parsed}
    if not ut:
        ut = {"mechanism": "none",
              "note": "补探失败——按无机制处理（L0 判据自动 deferred）"}
    ut["reviewed"] = False
    ut["discovered_by"] = "porter/loop backfill"
    runner["unit_test"] = ut
    (ws / "runner.json").write_text(json.dumps(runner, ensure_ascii=False,
                                               indent=2), encoding="utf-8")
    print(f"[porter] P4: unit_test 节回填 mechanism={ut.get('mechanism')}"
          "（reviewed:false）")
    return ut


def _run_unit_test(ws: Path, target_os: Path, runner: dict, proj: dict,
                   label: str) -> tuple[bool, str]:
    ut = runner.get("unit_test") or {}
    if ut.get("mechanism") == "none" or not ut.get("cmd"):
        return False, "mechanism=none"
    from ..env.probe import _base_env, _run, _strip_ansi
    rc, out = _run(ut["cmd"], cwd=target_os,
                   env=_base_env(target_os, runner),
                   timeout_sec=int(ut.get("timeout_sec", 1800)),
                   log_path=ws / "P4" / "logs" / f"{label}.log")
    out = _strip_ansi(out)
    ok = rc == 0 and ut.get("success_pattern", "test result: ok") in out
    fp = ut.get("fail_pattern")
    if ok and fp and fp in out:
        ok = False
    return ok, out


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
                boot_ok, log = _boot_ok(ws, target_os, proj,
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


def _boot_ok(ws: Path, target_os: Path, proj: dict,
             label: str) -> tuple[bool, str]:
    runner = json.loads((ws / "runner.json").read_text(encoding="utf-8"))
    r = probe_mod.probe_boot_with_device(ws / "P4", target_os, runner,
                                         proj.get("category") or [],
                                         label=label)
    lf = (runner.get("boot") or {}).get("log_file")
    log = ""
    if lf:
        p = Path(lf) if Path(lf).is_absolute() else target_os / lf
        if p.exists():
            log = p.read_text(encoding="utf-8", errors="replace")
    return bool(r.get("ok")), log


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
    map_block, redesign_block = _mapping_data_block(ws, surface, module)
    mod_json = json.loads((mdir / "module.json").read_text(encoding="utf-8"))
    skill = agent.load_skill("P4-migrate")

    mig_path = p4m / "reports" / "migration.json"
    mig = json.loads(mig_path.read_text(encoding="utf-8")) \
        if mig_path.exists() else {"slices": []}
    done_keys = {(s["file"], s["start"], s["end"]) for s in mig["slices"]}
    failures: list[dict] = []

    existing = sorted(p.name for p in (crate / "src").glob("*.rs")) \
        if (crate / "src").exists() else []
    for f, start, end in slices:
        key = (f.name, start, end)
        if key in done_keys:
            continue
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
                  f"\n## 任务\n把本切片重写为安全 Rust 进驱动 crate"
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


# ---------- 步骤 3/4：验收 + deferred ----------

def _load_deferred(ws: Path) -> dict:
    p = ws / "deferred.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() \
        else {"entries": []}


def _save_deferred(ws: Path, d: dict) -> None:
    (ws / "deferred.json").write_text(json.dumps(d, ensure_ascii=False,
                                                 indent=2),
                                      encoding="utf-8")


def _register_deferred(ws: Path, module: str, crit: dict) -> None:
    d = _load_deferred(ws)
    known = {e["id"] for e in d["entries"]}
    for c in crit["criteria"]:
        db = c.get("deferred_by")
        if not db:
            continue
        if c["id"] in known:
            continue
        d["entries"].append({"id": c["id"], "module": module,
                             "criterion": c, "deferred_by": db,
                             "status": "open",
                             "registered": datetime.now().isoformat(
                                 timespec="seconds"), "history": []})
    _save_deferred(ws, d)


def _try_clear_deferred(ws: Path, done: set[str], boot_log: str,
                        unit_out: str,
                        success_pattern: str = "test result: ok"
                        ) -> tuple[int, list[str]]:
    """清偿尝试：deferred_by ⊆ done 的 open 条目当场复核。返回 (rc, 未清偿)。"""
    d = _load_deferred(ws)
    uncleared: list[str] = []
    changed = False
    for e in d["entries"]:
        if e["status"] != "open":
            continue
        deps = set(e.get("deferred_by") or [])
        if not deps or not deps <= done:
            continue
        c = e["criterion"]
        ok, detail = False, ""
        if c["kind"] in ("log_pattern", "counter"):
            ok, n = crit_mod.check_log_pattern(boot_log, c["expr"])
            detail = f"hits={n}"
        elif c["kind"] == "unit_test":
            names = [x.strip() for x in c["expr"].split(",") if x.strip()]
            ok, detail = crit_mod.check_unit_test(unit_out, names,
                                                  success_pattern)
        else:
            ok, detail = False, f"kind {c['kind']} 无机器复核路径"
        e["history"].append({"time": datetime.now().isoformat(
            timespec="seconds"), "ok": ok, "detail": detail})
        if ok:
            e["status"] = "cleared"
        else:
            uncleared.append(e["id"])
        changed = True
    if changed:
        _save_deferred(ws, d)
    return (0 if not uncleared else 3), uncleared


def _acceptance(ws: Path, target_os: Path, module: str, p4m: Path,
                proj: dict, runner: dict, surface: dict,
                order: list[str]) -> tuple[int, dict]:
    crit = json.loads((ws / "P3" / module / "reports" / "criteria.json")
                      .read_text(encoding="utf-8"))
    results: list[dict] = []

    def rec(cid, layer, ok, detail):
        results.append({"id": cid, "layer": layer, "ok": ok,
                        "detail": detail})

    # L1 build
    b = probe_mod.probe_build(ws / "P4", target_os, runner,
                              label=f"P4_{module}_acc_build")
    rec(f"{module}.compile", "L1", b["ok"], b["detail"])
    # L2 boot + 收集日志
    boot_ok, log = _boot_ok(ws, target_os, proj, f"P4_{module}_acc_boot")
    rec(f"{module}.boot", "L2", boot_ok, "boot 双信号" +
        ("PASS" if boot_ok else "FAIL"))
    # L0 unit_test（机制为 none → 判据转 deferred）
    ut_out = ""
    for c in crit["criteria"]:
        if c["kind"] != "unit_test":
            continue
        if c.get("deferred_by"):
            rec(c["id"], "L0", None, "deferred（消费者依赖）")
            continue
        if (runner.get("unit_test") or {}).get("mechanism") == "none":
            _register_mech_none(ws, module, c)
            rec(c["id"], "L0", None, "deferred（目标 OS 无单测机制）")
            continue
        if not ut_out:
            _ok, ut_out = _run_unit_test(ws, target_os, runner, proj,
                                         f"P4_{module}_acc_ut")
        names = [x.strip() for x in c["expr"].split(",") if x.strip()]
        ok, detail = crit_mod.check_unit_test(
            ut_out, names,
            (runner.get("unit_test") or {}).get("success_pattern",
                                                "test result: ok"))
        rec(c["id"], "L0", ok, detail)
    # L3 本模块 + 累积回归（**已 done** 模块的 log_pattern 全查；未 done
    # 模块的判据不查——它们尚无对应代码）
    done_state = set()
    try:
        st = json.loads((ws / "loop_state.json").read_text(encoding="utf-8"))
        done_state = {m for m, v in (st.get("modules") or {}).items()
                      if v.get("phase") == "done"}
    except (OSError, json.JSONDecodeError):
        pass
    for m in [module, *(m for m in order if m in done_state)]:
        cpath = ws / "P3" / m / "reports" / "criteria.json"
        if not cpath.exists():
            continue
        cs = json.loads(cpath.read_text(encoding="utf-8"))["criteria"]
        for c in cs:
            if c["kind"] not in ("log_pattern", "counter"):
                continue
            if c.get("deferred_by"):
                continue
            if any(r["id"] == c["id"] for r in results):
                continue
            ok, n = crit_mod.check_log_pattern(log, c["expr"])
            rec(c["id"], "L3", ok, f"hits={n}" +
                ("" if m == module else f"（累积回归 {m}）"))
    # e2e / deferred 登记
    _register_deferred(ws, module, crit)
    for c in crit["criteria"]:
        if c["kind"] == "e2e" and not c.get("deferred_by"):
            rec(c["id"], "L4", None, "deferred（e2e 归 P5）")
    # deferred 清偿（done 集 = 状态机已 done ∪ 本模块即将 done）
    done = done_state | {module}
    rc_def, uncleared = _try_clear_deferred(
        ws, done, log, ut_out,
        (runner.get("unit_test") or {}).get("success_pattern",
                                            "test result: ok"))

    hard_fail = [r for r in results if r["ok"] is False]
    report = {"module": module,
              "time": datetime.now().isoformat(),
              "results": results,
              "pass": not hard_fail,
              "deferred_uncleared": uncleared}
    (p4m / "reports" / "acceptance.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in results:
        mark = "PASS" if r["ok"] else ("DEFER" if r["ok"] is None else "FAIL")
        print(f"[porter] P4: {r['id']:<40} {mark}  {r['detail']}")
    print(f"[porter] P4: {module} 验收 {'PASS' if report['pass'] else 'FAIL'}")
    if not report["pass"]:
        return 1, report
    if rc_def == 3:
        _write_deferred_questions(ws, module, uncleared)
        return 3, report
    return 0, report


def _register_mech_none(ws: Path, module: str, c: dict) -> None:
    d = _load_deferred(ws)
    if any(e["id"] == c["id"] for e in d["entries"]):
        return
    d["entries"].append({"id": c["id"], "module": module, "criterion": c,
                         "deferred_by": ["P5"],
                         "status": "open",
                         "registered": datetime.now().isoformat(
                             timespec="seconds"),
                         "history": [{"time": datetime.now().isoformat(
                             timespec="seconds"), "ok": False,
                             "detail": "目标 OS 无内核单测机制"}]})
    _save_deferred(ws, d)


def _write_deferred_questions(ws: Path, module: str, uncleared: list[str]):
    path = ws / "human_questions.md"
    lines = ["# loop 人工关口（exit 3）", "",
             f"- 模块：{module}；时间："
             f"{datetime.now():%Y-%m-%d %H:%M}",
             f"- deferred 无法清偿（消费者均已 done 仍 FAIL）："
             f"{', '.join(uncleared)}", "",
             "处理：核查 deferred.json 中对应条目 history，修正判据或"
             "代码后在 answers.md 写 `## retry {module}` 重跑。", ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- 主入口 ----------

def run_p4(ws: Path, module: str, order: list[str]) -> int:
    ctx = _ctx(ws, module)
    if ctx is None:
        return 2
    driver_root, target_os, p4m, proj, runner = ctx
    surface = json.loads((ws / "P3" / module / "reports" / "surface.json")
                         .read_text(encoding="utf-8"))

    _ensure_unit_test(ws, target_os, proj, runner)
    runner = json.loads((ws / "runner.json").read_text(encoding="utf-8"))

    rc = _step_fill(ws, driver_root, target_os, module, p4m, proj, runner,
                    order)
    if rc != 0:
        return rc

    rc, _slices_done = _step_migrate(ws, driver_root, target_os, module,
                                     p4m, proj, runner, surface)
    if rc != 0:
        return rc        # 切片失败已在 migration.json 留痕；attempts 由
        # run.py 统一 bump 并判界

    rc, _report = _acceptance(ws, target_os, module, p4m, proj, runner,
                              surface, order)
    return rc
