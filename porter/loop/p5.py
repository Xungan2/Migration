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
from . import events
from . import gates
from . import ut_verify
from .. import log as _log

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
            _log.console_line(f"[porter] P5: 缺少 {need}（先跑 p4 {module}）")
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


def _refresh_runbook(ws: Path) -> None:
    """runbook 域收成（P5 回填后——固定知识定点产出）。失败仅警告。"""
    try:
        from ..bootstrap import runbook as _rb
        _rb.draft_runbook(ws)
    except Exception as e:
        _log.console_line(f"[porter] P5: ⚠️ runbook 草稿刷新失败（不影响主流程）：{e}")


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
        _refresh_runbook(ws)
        if ok:
            _log.console_line("[porter] P5: unit_test 烟测 PASS（verified:true）")
        else:
            _log.console_line(f"[porter] P5: ⚠ unit_test 烟测 FAIL：{detail}"
                  "（verified:false；验收将按此判定，建议人工修 runner.json）")
        return ut

    _log.console_line("[porter] P5: runner.json 缺 unit_test 节——一次性补探回填（含"
          "第二道烟测）")
    skill = agent.load_skill("P0-unit-test-discover")
    from ..common import scope as _scope
    driver = _scope.driver_name_of(proj)
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
            _log.console_line(f"[porter] P5: 烟测失败（第 {attempt} 次）：{detail}")
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
    _refresh_runbook(ws)
    _log.console_line(f"[porter] P5: unit_test 节回填 mechanism={ut.get('mechanism')}"
          f" verified={ut.get('verified')}（reviewed:false）")
    if not ut.get("verified"):
        _log.console_line("[porter] P5: ⚠ 烟测未过——命令/特征不可信，验收将按此判定，"
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


# ---------- 步骤 1-5：验收编排 ----------

# （§15 重设计 2026-09-03：p5 重跑圈退役——求解循环 errorloop 自带
#   轮管理（≤3 轮 + 同签名早退）；attempts 在本挂载点随 rc 3 关口退役）


def _criterion_map(ws: Path, module: str, order: list[str]) -> dict:
    """判据 id → 条目（本模块 + 已 done 模块——累积回归判据也可查）。"""
    out: dict[str, dict] = {}
    done = _done_set(ws)
    for m in [module, *(x for x in order if x in done)]:
        p = ws / "P3" / m / "reports" / "criteria.json"
        try:
            for c in json.loads(p.read_text(encoding="utf-8"))["criteria"]:
                out[c["id"]] = c
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    return out


def _judge_core(ws: Path, module: str, order: list[str], target_os: Path,
                proj: dict, runner: dict, crit: dict
                ) -> tuple[list[dict], str, str, str]:
    """L1/L2/L0/L3 判定核心（无 deferred 副作用——可安全重跑）。

    返回 (results, log, ut_out, log_state)；log_state=missing 时调用方
    应中止（infra 关口已由 boot_and_log 登记，判定未对日志类判据进行）。
    """
    from . import probes as probe_lib     # 延迟导入避免环
    results: list[dict] = []

    def rec(cid, layer, ok, detail):
        results.append({"id": cid, "layer": layer, "ok": ok,
                        "detail": detail})

    b = probe_mod.probe_build(ws / "P5", target_os, runner,
                              label=f"P5_{module}_acc_build")
    rec(f"{module}.compile", "L1", b["ok"], b["detail"])
    boot_ok, log, log_state = probe_lib.boot_and_log(ws, "P5", target_os,
                                                     proj,
                                                     f"P5_{module}_acc_boot")
    rec(f"{module}.boot", "L2", boot_ok, "boot 双信号" +
        ("PASS" if boot_ok else "FAIL"))
    if log_state == "missing":
        rec(f"{module}.infra_log", "infra", None,
            "判定中止：boot 日志不可得（infra 关口待答）")
        return results, "", "", "missing"
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
    done_state = _done_set(ws)
    for m in [module, *(m for m in order if m in done_state)]:
        cpath = ws / "P3" / m / "reports" / "criteria.json"
        if not cpath.exists():
            continue
        try:
            cs = json.loads(cpath.read_text(encoding="utf-8"))["criteria"]
        except (json.JSONDecodeError, KeyError):
            continue
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
    return results, log, ut_out, log_state


def _fail_detail(results: list[dict], crit_map: dict) -> str:
    lines = []
    for r in [x for x in results if x["ok"] is False]:
        c = crit_map.get(r["id"]) or {}
        lines.append(f"- {r['id']}（kind={c.get('kind')} "
                     f"expr={c.get('expr')}）：{r['detail']}")
    return "\n".join(lines)[:2000]


def _solve_failures(ws: Path, module: str, order: list[str],
                    target_os: Path, proj: dict, runner: dict, crit: dict,
                    results: list[dict], log: str, ut_out: str,
                    snapshot: Path | None, crit_map: dict,
                    cfg: dict | None):
    """模块级求解循环（错误处理挂载①）。

    全体 FAIL 判据为一个失败集（subject=module）；verify = 重新执行
    _judge_core（build+boot+ut 全量重判——同旧重跑圈成本口径）。
    熔断（self_diagnosis off）返回 (None, 原判定)——挂载点走旧 rc 1
    语义（attempts→panic 的人工路径保留为熔断回退面）。
    """
    from . import errorloop as EL
    from ..env.probe import _strip_ansi
    if not gates.self_diagnosis_enabled():
        return None, (results, log, ut_out, "")
    state = {"results": results, "log": log, "ut_out": ut_out,
             "log_state": ""}

    def verify():
        res2, log2, ut2, ls2 = _judge_core(ws, module, order, target_os,
                                           proj, runner, crit)
        state.update(results=res2, log=log2, ut_out=ut2, log_state=ls2)
        if ls2 == "missing":
            return False, {"detail": "boot 日志不可得（infra 关口）——"
                                    "求解中止，判定输入缺失"}
        hard2 = [r for r in res2 if r["ok"] is False]
        if not hard2:
            return True, None
        return False, {"detail": _fail_detail(res2, crit_map),
                       "boot_log": _strip_ansi(state["log"])[-4000:],
                       "ut_out": state["ut_out"][-4000:]}

    failure = {
        "source": "p5", "subject": module, "module": module,
        "kind": "criteria-set",
        "detail": _fail_detail(results, crit_map),
        "boot_log": _strip_ansi(log)[-4000:], "boot_log_raw": log[-4000:],
        "ut_out": ut_out[-4000:], "runner": runner,
        "snapshot": snapshot.name if snapshot else None,
        "_workdir": target_os}
    outcome = EL.run_solve_loop(ws, failure, verify, cfg=cfg)
    return outcome, (state["results"], state["log"], state["ut_out"],
                     state["log_state"])


def _unsolved_gate(ws: Path, module: str, outcome: dict) -> int:
    """求解未解决 → 关口（retry 语义；升级报告与验收报告作 context）。"""
    from . import gates as gates_mod
    ctx = [f"P5/{module}/reports/acceptance.json"]
    if outcome.get("report_path"):
        ctx.append(outcome["report_path"])
    return gates_mod.panic(ws, {
        "id": f"p5.unsolved.{module}", "kind": "retry",
        "gate_type": "failure", "phase": "P5", "module": module,
        "subject": module,
        "question": (
            f"P5({module}) 求解循环未解决（终态 {outcome.get('status')}，"
            f"{len(outcome.get('rounds') or [])} 轮）。升级报告/快照/轮次"
            "总结在手（context_files）；人工修复后作答（retry 语义，"
            "重进 P5），或按报告处置（如平台泊车已登记则确认接受）。"),
        "context_files": ctx,
        "answer_form": [
            {"field": "note", "type": "text", "required": False,
             "hint": "诊断笔记（根因与修复，带给下一轮）"}],
        "applies_to": {"modules": [module]},
    })


def run_p5(ws: Path, module: str, order: list[str]) -> int:
    events.bind(ws, "p5")       # 观测地基（§15 挂载①）
    try:
        from ..log import core as _log
        _log.phase_begin("p5", module=module, store_only=True)
    except Exception:
        pass
    ctx = _ctx(ws, module)
    if ctx is None:
        return 2
    target_os, p5m, p3_reports, proj, runner = ctx

    _ensure_unit_test(ws, target_os, proj, runner)
    runner = json.loads((ws / "runner.json").read_text(encoding="utf-8"))
    from . import p6 as p6_mod          # 配置读取（延迟导入避免环）
    cfg = p6_mod.load_config()

    crit = json.loads((p3_reports / "criteria.json").read_text(
        encoding="utf-8"))
    crit_map = _criterion_map(ws, module, order)

    results, log, ut_out, log_state = _judge_core(ws, module, order,
                                                  target_os, proj,
                                                  runner, crit)
    if log_state == "missing":
        # 抢占（H9 重构）：判定输入不存在 → 本轮不判任何日志类判据，
        # infra 关口已登记；写报告后 rc 3（run.py 的 open_blocking
        # 复查衔接：人答关口后续跑）
        report = {"module": module,
                  "time": datetime.now().isoformat(),
                  "results": results, "pass": False,
                  "infra": "boot_no_log",
                  "deferred_uncleared": [], "solve": []}
        acceptance_path(ws, module).write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8")
        _log.console_line(f"[porter] P5: {module} 判定中止（boot 日志不可得，"
              "infra 关口待答）——exit 3")
        return 3

    solve_outcome: dict | None = None
    hard_fail = [r for r in results if r["ok"] is False]
    if hard_fail:
        # ---- 失败即快照（任何求解之前；events 观测不受熔断控制） ----
        snap = events.take_failure_snapshot(
            ws, "p5", module,
            f"{len(hard_fail)} 判据 FAIL："
            f"{', '.join(r['id'] for r in hard_fail)[:200]}",
            runner=runner, target_os=target_os,
            extra_files=[(p3_reports / "criteria.json",
                          "criteria.json")])
        solve_outcome, (results, log, ut_out, log_state) = _solve_failures(
            ws, module, order, target_os, proj, runner, crit, results,
            log, ut_out, snap, crit_map, cfg)
        if solve_outcome is not None and log_state == "missing":
            # 求解复跑中日志面丢失 → 同抢占语义
            report = {"module": module,
                      "time": datetime.now().isoformat(),
                      "results": results, "pass": False,
                      "infra": "boot_no_log",
                      "deferred_uncleared": [],
                      "solve": solve_outcome.get("rounds") or []}
            acceptance_path(ws, module).write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8")
            _log.console_line(f"[porter] P5: {module} 求解复跑后日志不可得"
                  "（infra 关口待答）——exit 3")
            return 3

    # L4 e2e / deferred 登记（一次性，重跑圈外）
    _register_deferred(ws, module, crit)
    for c in crit["criteria"]:
        if c["kind"] == "e2e" and not c.get("deferred_by"):
            results.append({"id": c["id"], "layer": "L4", "ok": None,
                            "detail": "deferred（e2e 归 P6 系统验收）"})
    success_pattern = (runner.get("unit_test") or {}).get(
        "success_pattern", "test result: ok")
    done = _done_set(ws) | {module}
    rc_def, uncleared = _try_clear_deferred(ws, done, log, ut_out,
                                            success_pattern)

    hard_fail = [r for r in results if r["ok"] is False]
    report = {"module": module,
              "time": datetime.now().isoformat(),
              "results": results,
              "pass": not hard_fail,
              "deferred_uncleared": uncleared,
              "solve": (solve_outcome or {}).get("rounds") or []}
    acceptance_path(ws, module).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in results:
        mark = "PASS" if r["ok"] else ("DEFER" if r["ok"] is None
                                       else "FAIL")
        _log.console_line(f"[porter] P5: {r['id']:<40} {mark}  {r['detail']}")
    _write_report(ws, module, p5m, report)
    _log.console_line(f"[porter] P5: {module} 验收 {'PASS' if report['pass'] else 'FAIL'}")
    if not report["pass"]:
        if solve_outcome is not None:
            # 求解循环在场且未解决（含 parked/rehung——泊车/改挂已登记，
            # 停人定夺）→ 关口；attempts 在此挂载点退役（rc 3 不烧）
            return _unsolved_gate(ws, module, solve_outcome)
        return 1        # 熔断 bypass：走 attempts→panic 人工路径
    if rc_def == 3:
        from . import gates as gates_mod
        for eid in uncleared:
            gates_mod.panic(ws, {
                "id": f"p5.deferred.{module}.{eid}", "kind": "decision",
                "gate_type": "failure", "phase": "P5", "module": module,
                "subject": eid, "target": "deferred",
                "question": (
                    f"deferred 判据 {eid} 无法清偿（消费者均已 done 仍 "
                    "FAIL）。两种可能：判据本身写错（如日志级别不可达、"
                    "正则错）或跨模块集成真坏。选 fix-criterion 给新正则"
                    "（工具同步改 deferred 副本+criteria 正本）；选 "
                    "fix-code 修代码后 retry。核查 deferred.json 该条目 "
                    "history 可见历次失败详情。"),
                "context_files": ["deferred.json",
                                  f"P3/{module}/reports/criteria.json"],
                "answer_form": [
                    {"field": "verdict", "type": "enum",
                     "options": ["fix-criterion", "fix-code"],
                     "required": True},
                    {"field": "new_expr", "type": "text", "required": False,
                     "hint": "verdict=fix-criterion 时必填：新判据正则"}],
                "applies_to": {"modules": [module]},
            })
        return 3
    try:
        from ..log import core as _log
        _log.phase_end("p5", module=module, rc=0, store_only=True)
    except Exception:
        pass
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
    tri = report.get("solve") or []
    if tri:
        lines += ["", "## 求解循环（错误处理挂载①）", "",
                  "| 轮 | 动作 | 归责 | 复验 | 处置 |", "|---|---|---|---|---|"]
        for v in tri:
            lines.append(f"| R{v.get('round')} | {v.get('action')} "
                         f"| {v.get('circuit')} "
                         f"| {'PASS' if v.get('verified') else '—'} "
                         f"| {'; '.join(v.get('applied') or [])[:160]} |")
    (p5m / "reports" / "report.md").write_text(
        "\n".join(ln for ln in lines if ln is not None) + "\n",
        encoding="utf-8")
