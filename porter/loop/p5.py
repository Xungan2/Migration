"""p5.py — P5(M) 模块级验收（方案 A 相位重构，2026-08-31）。

自 p4.py 剥离的验收段独立成相位：P4 专注生产（fill+迁移+冒烟），
P5 专注判据级验收 + deferred 登记/清偿。

步骤：
  0. unit_test 节回填   runner.json 缺通用 `unit_test` 节时做一次性
                        agent 补探（P0-unit-test-discover；reviewed:false）
                        + 第二道烟测（真跑驱动级命令机器复核）
  1. L1 build           runner build 双信号
  2. L2 boot            runner boot 双信号 + 收集启动日志
  3. L0 unit_test       ktest 同场跑一次（本模块单测 + 组件级测试共用
                        输出）；机制 none → 判据自动转 deferred（非硬失败）
  4. L3 log_pattern     本模块 log_pattern/counter 判据 grep +
                        累积回归（此前全部已 done 模块的 L0+L3 判据重跑）
  5. L4 / deferred      e2e 判据登记 deferred（归 P6 系统验收）；
                        deferred_by ⊆ done 的当场清偿；无法清偿 exit 3

产物：P5/<M>/reports/{acceptance.json, report.md}
      （兼容读取旧 P4/<M>/reports/acceptance.json 位置以支持存量工作区）
返回：0 成功 / 1 失败 / 2 前置缺失 / 3 需人工（deferred 无法清偿）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..common import agent
from ..env import probe as probe_mod
from . import criteria as crit_mod
from . import ut_verify

# "归全局系统验收"哨兵：新写 __P6__（旧编号期的 P5/__P5__ 兼容读取，
# 均不可在 P5(M) 内清偿——属 P6 系统验收）
GLOBAL_SENTINEL = "__P6__"
_LEGACY_GLOBAL_SENTINELS = {"P5", "__P5__", GLOBAL_SENTINEL}


def _is_global(db: list | None) -> bool:
    return bool(db) and set(db) <= _LEGACY_GLOBAL_SENTINELS


# ---------- 前置 ----------

def _ctx(ws: Path, module: str) -> tuple[Path, Path, Path, dict, dict] | None:
    for need in (ws / "project.json", ws / "runner.json",
                 ws / "P3" / module / "reports" / "criteria.json",
                 ws / "P4" / module / "reports" / "migration.json"):
        if not need.exists():
            print(f"[porter] P5: 缺少 {need}（先跑 p4 {module}）")
            return None
    proj = json.loads((ws / "project.json").read_text(encoding="utf-8"))
    runner = json.loads((ws / "runner.json").read_text(encoding="utf-8"))
    target_os = Path(proj["target_os"])
    p5m = ws / "P5" / module
    (p5m / "logs").mkdir(parents=True, exist_ok=True)
    (p5m / "reports").mkdir(parents=True, exist_ok=True)
    return target_os, p5m, ws / "P3" / module / "reports", proj, runner


def acceptance_path(ws: Path, module: str) -> Path:
    return ws / "P5" / module / "reports" / "acceptance.json"


def _legacy_acceptance(ws: Path, module: str) -> Path | None:
    """旧编号期的验收产物位置（P4/<M>/reports/），只读兼容。"""
    p = ws / "P4" / module / "reports" / "acceptance.json"
    return p if p.exists() else None


def _done_set(ws: Path) -> set[str]:
    try:
        st = json.loads((ws / "loop_state.json").read_text(encoding="utf-8"))
        return {m for m, v in (st.get("modules") or {}).items()
                if v.get("phase") == "done"}
    except (OSError, json.JSONDecodeError):
        return set()


# ---------- 步骤 0：unit_test 节回填（自 p4.py 迁入） ----------

def _smoke_verify_ut(ws: Path, target_os: Path, runner: dict,
                     ut: dict, label: str) -> tuple[bool, str, str]:
    """第二道烟测：真跑驱动级 cmd 并断言。返回 (ok, 说明, 观测输出)。"""
    from ..env.probe import _base_env
    if ut.get("mechanism") == "none" or not ut.get("cmd"):
        return True, "mechanism=none 或无命令——跳过", ""
    ok, detail, out = ut_verify.run_and_verify(
        ut["cmd"], cwd=target_os, env=_base_env(target_os, runner),
        timeout_sec=int(ut.get("timeout_sec", 1800)),
        log_path=ws / "P5" / "logs" / f"{label}.log",
        success_pattern=ut.get("success_pattern", "test result: ok"),
        fail_pattern=ut.get("fail_pattern"))
    return ok, detail, out


def _save_runner_ut(ws: Path, runner: dict, ut: dict) -> None:
    runner["unit_test"] = ut
    (ws / "runner.json").write_text(json.dumps(runner, ensure_ascii=False,
                                               indent=2), encoding="utf-8")


def _ensure_unit_test(ws: Path, target_os: Path, proj: dict,
                      runner: dict) -> dict:
    """unit_test 节获取 + 第二道烟测（真跑驱动级命令机器复核）。

    - 已有且 verified=true：直接复用。
    - 已有且未验证：真跑一次记 verified（幂等，一次性成本）。
    - 缺失：agent 补探（产出后真跑烟测；失败带观测输出反馈重试 ≤2；
      仍败标 verified=false + 醒目警告——不硬阻塞，验收/人工兜底）。
    """
    ut = runner.get("unit_test")
    if ut:
        if ut.get("verified") or ut.get("mechanism") == "none" \
                or not ut.get("cmd"):
            return ut
        ok, detail, _out = _smoke_verify_ut(ws, target_os, runner, ut,
                                            "unit_test_smoke_backfill")
        ut["verified"] = ok
        _save_runner_ut(ws, runner, ut)
        if ok:
            print("[porter] P5: unit_test 烟测 PASS（verified:true）")
        else:
            print(f"[porter] P5: ⚠ unit_test 烟测 FAIL：{detail}"
                  "（verified:false；验收将按此判定，建议人工修 runner.json）")
        return ut

    print("[porter] P5: runner.json 缺 unit_test 节——一次性补探回填（含"
          "第二道烟测）")
    skill = agent.load_skill("P0-unit-test-discover")
    driver = Path(proj["linux_driver"]).name
    prompt = (f"{skill}\n\n---\n\n## 背景数据\n"
              f"- 目标 OS 源码树：`{target_os}` = 你的工作目录\n"
              f"- 驱动 crate：aster-{driver}（kernel/core/comps/{driver}）\n"
              f"- 既有 build 命令形态（容器包裹）参考：\n"
              f"  {runner['build']['cmd']}\n"
              f"\n## 任务\n探明目标 OS 的内核态单元测试机制并输出紧凑 "
              f"JSON 块。")
    ut = None
    for attempt in range(1, 4):
        rc, out = agent.run_agent(prompt, workdir=target_os,
                                  log_stem=str(ws / "P5" / "logs" /
                                               f"unit_test_discover_R{attempt}"),
                                  timeout_sec=900)
        parsed = agent.extract_json(out) if rc == 0 else None
        if parsed and "cmd" in parsed:
            ut = {k: parsed[k] for k in ("mechanism", "cmd", "timeout_sec",
                                         "success_pattern", "fail_pattern",
                                         "scope_hint", "smoke_cmd")
                  if k in parsed}
            # 第二道烟测：真跑驱动级命令
            ok, detail, observed = _smoke_verify_ut(
                ws, target_os, runner, ut, f"unit_test_discover_smoke_R{attempt}")
            ut["verified"] = ok
            if ok:
                break
            print(f"[porter] P5: 烟测失败（第 {attempt} 次）：{detail}")
            prompt = prompt + ut_verify.feedback_block(detail, observed)
        else:
            prompt = prompt + (
                "\n\n---\n\n## 上一次输出的问题\n未见合法 JSON。只输出一个"
                "紧凑 JSON 对象（一行）。")
    if not ut:
        ut = {"mechanism": "none",
              "note": "补探失败——按无机制处理（L0 判据自动 deferred）",
              "verified": True}
    ut["reviewed"] = False
    ut["discovered_by"] = "porter/loop backfill"
    _save_runner_ut(ws, runner, ut)
    print(f"[porter] P5: unit_test 节回填 mechanism={ut.get('mechanism')}"
          f" verified={ut.get('verified')}（reviewed:false）")
    if not ut.get("verified"):
        print("[porter] P5: ⚠ 烟测未过——命令/特征不可信，验收将按此判定，"
              "建议人工核查 runner.json 的 unit_test 节")
    return ut


def _run_unit_test(ws: Path, target_os: Path, runner: dict,
                   label: str) -> tuple[bool, str]:
    """ktest 同场跑一次（单测 + 组件级测试共用输出）。"""
    ut = runner.get("unit_test") or {}
    if ut.get("mechanism") == "none" or not ut.get("cmd"):
        return False, "mechanism=none"
    from ..env.probe import _base_env, _run, _strip_ansi
    rc, out = _run(ut["cmd"], cwd=target_os,
                   env=_base_env(target_os, runner),
                   timeout_sec=int(ut.get("timeout_sec", 1800)),
                   log_path=ws / "P5" / "logs" / f"{label}.log")
    out = _strip_ansi(out)
    ok = rc == 0 and ut.get("success_pattern", "test result: ok") in out
    fp = ut.get("fail_pattern")
    if ok and fp and fp in out:
        ok = False
    return ok, out


# ---------- deferred 登记/清偿（自 p4.py 迁入，哨兵改 __P6__） ----------

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
        if not deps or _is_global(e.get("deferred_by")) or not deps <= done:
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


def _register_mech_none(ws: Path, module: str, c: dict) -> None:
    d = _load_deferred(ws)
    if any(e["id"] == c["id"] for e in d["entries"]):
        return
    d["entries"].append({"id": c["id"], "module": module, "criterion": c,
                         "deferred_by": [GLOBAL_SENTINEL],
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


# ---------- 步骤 1-5：验收编排 ----------

def run_p5(ws: Path, module: str, order: list[str]) -> int:
    ctx = _ctx(ws, module)
    if ctx is None:
        return 2
    target_os, p5m, p3_reports, proj, runner = ctx
    from . import probes as probe_lib     # 延迟导入避免环

    _ensure_unit_test(ws, target_os, proj, runner)
    runner = json.loads((ws / "runner.json").read_text(encoding="utf-8"))

    crit = json.loads((p3_reports / "criteria.json").read_text(
        encoding="utf-8"))
    results: list[dict] = []

    def rec(cid, layer, ok, detail):
        results.append({"id": cid, "layer": layer, "ok": ok,
                        "detail": detail})

    # L1 build 双信号
    b = probe_mod.probe_build(ws / "P5", target_os, runner,
                              label=f"P5_{module}_acc_build")
    rec(f"{module}.compile", "L1", b["ok"], b["detail"])
    # L2 boot 双信号 + 收集日志
    boot_ok, log = probe_lib.boot_and_log(ws, "P5", target_os, proj,
                                          f"P5_{module}_acc_boot")
    rec(f"{module}.boot", "L2", boot_ok, "boot 双信号" +
        ("PASS" if boot_ok else "FAIL"))
    # L0 unit_test：ktest 同场跑一次，本模块 + 累积回归共用输出
    ut_out = ""
    ut_mech_none = (runner.get("unit_test") or {}).get("mechanism") == "none"
    if not ut_mech_none and (runner.get("unit_test") or {}).get("cmd"):
        _ok, ut_out = _run_unit_test(ws, target_os, runner,
                                     f"P5_{module}_acc_ut")
    success_pattern = (runner.get("unit_test") or {}).get(
        "success_pattern", "test result: ok")
    for c in crit["criteria"]:
        if c["kind"] != "unit_test":
            continue
        if c.get("deferred_by"):
            rec(c["id"], "L0", None, "deferred（消费者依赖）")
            continue
        if ut_mech_none:
            _register_mech_none(ws, module, c)
            rec(c["id"], "L0", None, "deferred（目标 OS 无单测机制）")
            continue
        names = [x.strip() for x in c["expr"].split(",") if x.strip()]
        ok, detail = crit_mod.check_unit_test(ut_out, names, success_pattern)
        rec(c["id"], "L0", ok, detail)
    # L3 本模块 + 累积回归（此前全部已 done 模块的 L0+L3 判据重跑；
    # 未 done 模块的判据不查——它们尚无对应代码）
    done_state = _done_set(ws)
    for m in [module, *(m for m in order if m in done_state)]:
        cpath = ws / "P3" / m / "reports" / "criteria.json"
        if not cpath.exists():
            continue
        cs = json.loads(cpath.read_text(encoding="utf-8"))["criteria"]
        for c in cs:
            if any(r["id"] == c["id"] for r in results):
                continue
            if c["kind"] in ("log_pattern", "counter"):
                if c.get("deferred_by"):
                    continue
                ok, n = crit_mod.check_log_pattern(log, c["expr"])
                rec(c["id"], "L3", ok, f"hits={n}" +
                    ("" if m == module else f"（累积回归 {m}）"))
            elif c["kind"] == "unit_test":
                if c.get("deferred_by") or ut_mech_none:
                    continue
                names = [x.strip() for x in c["expr"].split(",")
                         if x.strip()]
                ok, detail = crit_mod.check_unit_test(ut_out, names,
                                                      success_pattern)
                rec(c["id"], "L0", ok, ("" if m == module else
                                        f"（累积回归 {m}）") + detail)
    # L4 e2e / deferred 登记
    _register_deferred(ws, module, crit)
    for c in crit["criteria"]:
        if c["kind"] == "e2e" and not c.get("deferred_by"):
            rec(c["id"], "L4", None, f"deferred（e2e 归 P6 系统验收）")
    # deferred 清偿（done 集 = 状态机已 done ∪ 本模块即将 done）
    done = done_state | {module}
    rc_def, uncleared = _try_clear_deferred(ws, done, log, ut_out,
                                            success_pattern)

    hard_fail = [r for r in results if r["ok"] is False]
    report = {"module": module,
              "time": datetime.now().isoformat(),
              "results": results,
              "pass": not hard_fail,
              "deferred_uncleared": uncleared}
    acceptance_path(ws, module).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in results:
        mark = "PASS" if r["ok"] else ("DEFER" if r["ok"] is None
                                       else "FAIL")
        print(f"[porter] P5: {r['id']:<40} {mark}  {r['detail']}")
    _write_report(ws, module, p5m, report)
    print(f"[porter] P5: {module} 验收 {'PASS' if report['pass'] else 'FAIL'}")
    if not report["pass"]:
        return 1
    if rc_def == 3:
        _write_deferred_questions(ws, module, uncleared)
        return 3
    return 0


def _write_report(ws: Path, module: str, p5m: Path, report: dict) -> None:
    legacy = _legacy_acceptance(ws, module)
    lines = [f"# P5({module}) 模块级验收报告", "",
             f"- 时间：{datetime.now():%Y-%m-%d %H:%M}",
             f"- 结论：{'PASS' if report['pass'] else 'FAIL'}",
             f"- 判据总数：{len(report['results'])}"
             f"（FAIL {sum(1 for r in report['results'] if r['ok'] is False)}"
             f" / DEFER {sum(1 for r in report['results'] if r['ok'] is None)}）",
             f"- deferred 未清偿：{report['deferred_uncleared'] or '无'}",
             (f"- 旧位置记录（只读兼容）：{legacy}" if legacy else ""),
             "", "| 判据 | 层 | 结果 | 说明 |", "|---|---|---|---|"]
    for r in report["results"]:
        mark = "PASS" if r["ok"] else ("DEFER" if r["ok"] is None
                                       else "FAIL")
        lines.append(f"| {r['id']} | {r['layer']} | {mark} "
                     f"| {r['detail']} |")
    (p5m / "reports" / "report.md").write_text(
        "\n".join(ln for ln in lines if ln is not None) + "\n",
        encoding="utf-8")
