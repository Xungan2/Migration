"""p6.py — P6 系统验收（全局收口：聚合健康 / 执行重测 / L4 判据定稿与判定）。

三种模式（互斥，见 run_p6）：
  聚合（默认）       汇总各模块 acceptance（P5/ 优先，兼容读旧 P4/ 位置）
                    + deferred + 判据状态 + defects → P6/reports/health.json/.md，
                    零重测。
  --finalize-l4      L4 判据定稿门：校验 P6/reports/l4_criteria.json 草案 →
                    按审核门（porter/config.json 的
                    review_gates.l4_criteria_finalization = agent|human）：
                    agent 自判续跑；human 草案落盘 + 评审摘要 → exit 3 停车，
                    ws/answers.md 出现放行标记（l4_criteria_finalization:
                    approve/release/放行/通过）后重跑放行。
  --execute [--l4]   一轮 build + boot(SLIRP) + ktest → 重判全部判据
                    （L1/L2/L0/L3）+ deferred 清偿（含 __P6__ 哨兵条目——
                    P6 是哨兵 owner；P5 循环不可清、P6 可清）；
                    --l4 扩展位：按定稿后的 l4_criteria.json 判全部 L4 判据
                    （驱动内核自测打 `L4 <id> PASS|FAIL <detail>` 日志行，
                    本工具从 boot 日志正则判定）。

defects.json（缺陷账本，P6-5 消费）：
  {defects: [{id, title, status(open|in_progress|fixed|parked),
              discovered{time,evidence}, root_cause, fix, regression_evidence,
              attempts, history[{time,event,detail}]}]}
  close（→fixed）强制四字段完整：发现(discovered)/根因/修复/回归证据。

哨兵迁移：读取兼容 {P5, __P5__, __P6__}（p5.py 同款语义）；执行模式首次
写回 deferred.json 时统一规范化为 __P6__。

返回：0 达成 / 1 硬失败 / 2 前置缺失 / 3 需人工（审核门停车）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from ..env import probe as probe_mod
from . import criteria as crit_mod
from . import events
from . import p5 as p5_mod
from .. import log as _log

# 归全局系统验收哨兵（与 p5.py 语义一致；P6 是 owner，可清偿）
GLOBAL_SENTINEL = "__P6__"
_LEGACY_GLOBAL_SENTINELS = {"P5", "__P5__", GLOBAL_SENTINEL}

# 执行模式设备环境：SLIRP 显式后端（P5-A 已验证形态）。优先取
# runner.inject_device.example_args["net-user"]（工作区数据），缺省用此常量。
DEFAULT_EXEC_DEVICE_ARGS = "-netdev user,id=e1 -device e1000,netdev=e1"

# L4 判据草案 schema
L4_FORMS = {"内核自测", "boot观测", "流量驱动"}
L4_DISPOSITIONS = {"clear", "park"}     # 清偿 / 泊车
_RELEASE_RE = re.compile(
    r"l4_criteria_finalization\s*[:：]\s*(approve|release|放行|通过)",
    re.IGNORECASE)


def _is_global(db: list | None) -> bool:
    return bool(db) and set(db) <= _LEGACY_GLOBAL_SENTINELS


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- 配置（porter/config.json，仓级；缺省 agent 自判） ----------

def load_config(path: Path | None = None) -> dict:
    p = path or Path(__file__).resolve().parent.parent / "config.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def review_gate_mode(cfg: dict | None = None,
                     name: str = "l4_criteria_finalization") -> str:
    cfg = cfg if cfg is not None else load_config()
    mode = ((cfg.get("review_gates") or {}).get(name)) or "agent"
    return "human" if mode == "human" else "agent"


# ---------- defects.json 缺陷账本 ----------

def load_defects(ws: Path) -> dict:
    p = ws / "defects.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() \
        else {"defects": []}


def save_defects(ws: Path, d: dict) -> None:
    (ws / "defects.json").write_text(json.dumps(d, ensure_ascii=False,
                                                indent=2),
                                     encoding="utf-8")


def _find_defect(d: dict, did: str):
    return next((x for x in d["defects"] if x["id"] == did), None)


def add_defect(ws: Path, did: str, title: str, evidence: str) -> dict:
    d = load_defects(ws)
    if _find_defect(d, did):
        raise ValueError(f"缺陷已存在: {did}")
    entry = {"id": did, "title": title, "status": "open",
             "discovered": {"time": _now(), "evidence": evidence},
             "root_cause": "", "fix": "", "regression_evidence": "",
             "attempts": 0,
             "history": [{"time": _now(), "event": "discovered",
                          "detail": evidence}]}
    d["defects"].append(entry)
    save_defects(ws, d)
    return entry


def close_defect(ws: Path, did: str, root_cause: str, fix: str,
                 regression_evidence: str) -> dict:
    """close 强制四字段完整（发现已在 add 时记录）。"""
    d = load_defects(ws)
    e = _find_defect(d, did)
    if not e:
        raise ValueError(f"缺陷不存在: {did}")
    missing = [k for k, v in (("root_cause", root_cause), ("fix", fix),
                              ("regression_evidence", regression_evidence))
               if not (v or "").strip()]
    if missing:
        raise ValueError(f"close 缺字段: {missing}（发现/根因/修复/回归证据"
                         "四字段必须完整）")
    e.update({"root_cause": root_cause, "fix": fix, "status": "fixed",
              "regression_evidence": regression_evidence})
    e["history"].append({"time": _now(), "event": "fixed",
                         "detail": f"根因={root_cause[:120]}"})
    save_defects(ws, d)
    try:                        # 类 2 钩子：缺陷根因链 → 候选（B11）
        from ..bootstrap import candidates as _cand
        _cand.record_candidate(
            ws, hook="defect-close", ref=did,
            draft=f"缺陷 {did}（{e.get('title', '')}）根因：{root_cause}"
                  f"；修复：{fix}；回归证据：{regression_evidence[:200]}",
            evidence=["defects.json"], suggested="pitfalls")
    except Exception:
        pass
    return e


def park_defect(ws: Path, did: str, reason: str) -> dict:
    d = load_defects(ws)
    e = _find_defect(d, did)
    if not e:
        raise ValueError(f"缺陷不存在: {did}")
    e["status"] = "parked"
    e["history"].append({"time": _now(), "event": "parked",
                         "detail": reason})
    save_defects(ws, d)
    try:                        # 类 2 钩子：泊车理由 → 候选（平台缺口类）
        from ..bootstrap import candidates as _cand
        _cand.record_candidate(
            ws, hook="defect-park", ref=did,
            draft=f"缺陷 {did}（{e.get('title', '')}）泊车：{reason}",
            evidence=["defects.json"], suggested="pitfalls")
    except Exception:
        pass
    return e


def bump_defect(ws: Path, did: str, event: str, detail: str) -> int:
    d = load_defects(ws)
    e = _find_defect(d, did)
    if not e:
        raise ValueError(f"缺陷不存在: {did}")
    e["attempts"] += 1
    e["history"].append({"time": _now(), "event": event, "detail": detail})
    save_defects(ws, d)
    return e["attempts"]


# ---------- L4 判据草案：schema 校验 / 定稿门 ----------

def l4_criteria_path(ws: Path) -> Path:
    return ws / "P6" / "reports" / "l4_criteria.json"


def validate_l4(raw: list) -> tuple[list[dict], list[str]]:
    """返回 (合格条目, 错误清单)。字段：
    {id, title, form(内核自测|boot观测|流量驱动), expr(正则；park 可空),
     rationale, disposition(clear|park)}
    """
    ok: list[dict] = []
    errs: list[str] = []
    if not isinstance(raw, list):
        return [], ["criteria 不是数组"]
    seen: set[str] = set()
    for i, c in enumerate(raw):
        if not isinstance(c, dict):
            errs.append(f"[{i}] 非对象")
            continue
        miss = [k for k in ("id", "title", "form", "expr", "rationale",
                            "disposition") if k not in c]
        if miss:
            errs.append(f"[{i}] 缺字段 {miss}")
            continue
        problems = []
        cid = str(c["id"])
        if not cid:
            problems.append("id 为空")
        if cid in seen:
            problems.append(f"id 重复: {cid}")
        if not str(c["title"] or "").strip():
            problems.append("title 为空")
        if c["form"] not in L4_FORMS:
            problems.append(f"form 非法: {c['form']}（须 {sorted(L4_FORMS)}）")
        if c["disposition"] not in L4_DISPOSITIONS:
            problems.append(f"disposition 非法: {c['disposition']}"
                            f"（须 clear|park）")
        if not str(c["rationale"] or "").strip():
            problems.append("rationale 为空")
        expr = str(c["expr"] or "")
        if c["disposition"] == "clear" and not expr:
            problems.append("clear 判据必须有 expr（boot 日志正则）")
        if expr:
            try:
                re.compile(expr)
            except re.error as e:
                problems.append(f"expr 非法正则: {e}")
        if problems:
            errs.append(f"[{c.get('id', i)}] {'; '.join(problems)}")
        else:
            seen.add(cid)
            ok.append({"id": cid, "title": str(c["title"]),
                       "form": c["form"], "expr": expr,
                       "rationale": str(c["rationale"]),
                       "disposition": c["disposition"]})
    return ok, errs


def _released(ws: Path) -> bool:
    p = ws / "answers.md"
    if not p.exists():
        return False
    for ln in p.read_text(encoding="utf-8").splitlines():
        if _RELEASE_RE.search(ln):
            return True
    return False


def finalize_l4(ws: Path, cfg: dict | None = None) -> int:
    """定稿门：校验草案 → 审核门（agent 续跑 / human 停车等 answers.md）。"""
    path = l4_criteria_path(ws)
    if not path.exists():
        _log.console_line(f"[porter] P6: 缺少草案 {path}——先按 P6-3 内容设计起草再定稿")
        return 2
    doc = json.loads(path.read_text(encoding="utf-8"))
    ok_items, errs = validate_l4(doc.get("criteria") or [])
    if errs:
        _log.console_line(f"[porter] P6: L4 判据草案 schema 错误 {len(errs)} 处：")
        for e in errs:
            print(f"  - {e}")
        return 1
    mode = review_gate_mode(cfg)
    # 双协议放行：legacy 单行放行令（_RELEASE_RE）或 gates 审批关口
    from . import gates as gates_mod
    gates_mod.process_answered_gates(ws)
    gate = gates_mod.GateLedger(ws).load().find("p6.l4.finalize")
    gate_ok = bool(gate and gate.get("status") in ("applied", "resolved")
                   and (gate.get("answer") or {}).get("verdict",
                                                      "").lower()
                   in ("approve", "release", "放行", "通过"))
    if mode == "human" and not _released(ws) and not gate_ok:
        doc["status"] = "draft"
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        _write_l4_review(ws, ok_items)
        import hashlib as _hl
        sha = _hl.sha256(path.read_bytes()).hexdigest()[:16]
        led = gates_mod.GateLedger(ws).load()
        gate = led.find("p6.l4.finalize")
        if gate and gate.get("status") in ("open", "invalid"):
            gate["artifact_sha"] = sha      # 草案可能已人工修改——刷新指纹
            led.save()
        elif not gate:
            led.add(
                id="p6.l4.finalize", lane="checkpoint", kind="approval",
                gate_type="decision", phase="P6", checkpoint="CP3",
                question=(f"L4 判据定稿审批（{len(ok_items)} 条，草案已落盘 "
                          f"{path}）。这是对'完成'定义的签字：评审摘要见 "
                          "P6/reports/l4_criteria_REVIEW.md。批准绑定草案"
                          "指纹——草案变更后批准自动失效。"),
                context_files=["P6/reports/l4_criteria.json",
                               "P6/reports/l4_criteria_REVIEW.md"],
                answer_form=[
                    {"field": "verdict", "type": "enum",
                     "options": ["approve", "reject"], "required": True}],
                artifact_path=str(path.relative_to(ws)),
                artifact_sha=sha,
            )
        gates_mod.render_human_questions(ws)
        _log.console_line(f"[porter] P6: 审核门 human——草案落盘停车（exit 3），"
              "等 answers.md 表单放行（verdict: approve）")
        return 3
    if gate and gate.get("status") in ("applied", "resolved"):
        gates_mod.resolve_applied(gates_mod.GateLedger(ws).load(),
                                  "p6.l4.finalize", "L4 定稿完成结清")
    doc["status"] = "finalized"
    doc["finalized_time"] = _now()
    doc["criteria"] = ok_items
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    _log.console_line(f"[porter] P6: L4 判据定稿完成（{len(ok_items)} 条，"
          f"park {sum(1 for c in ok_items if c['disposition'] == 'park')}）")
    try:                        # 类 2 钩子：park 理由 → 候选（B12）
        from ..bootstrap import candidates as _cand
        for c in ok_items:
            if c.get("disposition") == "park" and c.get("rationale"):
                _cand.record_candidate(
                    ws, hook="l4-park", ref=c["id"],
                    draft=f"L4 判据 {c['id']}（{c.get('title', '')}）泊车："
                          f"{c['rationale']}",
                    evidence=["P6/reports/l4_criteria.json"],
                    suggested="pitfalls")
    except Exception:
        pass
    return 0


def _write_l4_review(ws: Path, items: list[dict]) -> None:
    lines = ["# L4 判据定稿评审摘要（human 门）", "",
             f"- 时间：{_now()}", f"- 条目：{len(items)}", "",
             "| id | 形态 | 处置 | 断言正则 | 理由 |", "|---|---|---|---|---|"]
    for c in items:
        lines.append(f"| {c['id']} | {c['form']} | {c['disposition']} "
                     f"| `{c['expr'] or '—'}` | {c['rationale']} |")
    lines += ["", "放行：answers.md 写 `l4_criteria_finalization: approve`；"
              "修改：直接编辑 l4_criteria.json 后重跑 `p6 --finalize-l4`。"]
    (ws / "P6" / "reports" / "l4_criteria_REVIEW.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8")


def load_finalized_l4(ws: Path) -> dict | None:
    path = l4_criteria_path(ws)
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if doc.get("status") == "finalized" else None


# ---------- 聚合 ----------

def _load_json(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _module_facts(ws: Path) -> list[dict]:
    st = _load_json(ws / "loop_state.json") or {}
    mods = st.get("modules") or {}
    order = st.get("order") or list(mods)
    facts = []
    for m in order:
        v = mods.get(m) or {}
        acc_path = p5_mod.acceptance_path(ws, m)
        acc, legacy = None, False
        if acc_path.exists():
            acc = _load_json(acc_path)
        else:
            lp = p5_mod._legacy_acceptance(ws, m)
            if lp:
                acc = _load_json(lp)
                legacy = True
        crit = _load_json(ws / "P3" / m / "reports" / "criteria.json")
        cs = (crit or {}).get("criteria") or []
        layers: dict[str, int] = {}
        for c in cs:
            layers[c["layer"]] = layers.get(c["layer"], 0) + 1
        facts.append({"module": m,
                      "phase": v.get("phase"),
                      "skipped": bool(v.get("skipped")),
                      "acceptance_pass": (acc or {}).get("pass"),
                      "acceptance_path": str(acc_path if acc_path.exists()
                                             else "") or None,
                      "acceptance_legacy": legacy,
                      "criteria_total": len(cs),
                      "criteria_layers": layers})
    return facts


def _deferred_facts(ws: Path) -> dict:
    d = _load_json(ws / "deferred.json") or {"entries": []}
    open_e = [e for e in d["entries"] if e["status"] == "open"]
    return {"total": len(d["entries"]), "open": len(open_e),
            "cleared": len(d["entries"]) - len(open_e),
            "open_entries": [{"id": e["id"], "module": e.get("module"),
                              "kind": (e.get("criterion") or {}).get("kind"),
                              "expr": (e.get("criterion") or {})
                                      .get("expr", ""),
                              "sentinel": _is_global(e.get("deferred_by")),
                              "deferred_by": e.get("deferred_by")}
                             for e in open_e]}


def _defects_facts(ws: Path) -> dict:
    d = _load_json(ws / "defects.json") or {"defects": []}
    by: dict[str, int] = {}
    for e in d["defects"]:
        by[e["status"]] = by.get(e["status"], 0) + 1
    return {"total": len(d["defects"]), "by_status": by}


def _l4_facts(ws: Path) -> dict:
    doc = _load_json(l4_criteria_path(ws))
    if not doc:
        return {"exists": False}
    return {"exists": True, "status": doc.get("status"),
            "total": len(doc.get("criteria") or []),
            "park": sum(1 for c in doc.get("criteria") or []
                        if c.get("disposition") == "park")}


def aggregate(ws: Path) -> int:
    """聚合健康全景（零重测）→ P6/reports/health.json/.md。"""
    (ws / "P6" / "reports").mkdir(parents=True, exist_ok=True)
    mods = _module_facts(ws)
    if not mods:
        _log.console_line("[porter] P6: 无 loop_state.json / 模块信息——工作区前置缺失")
        return 2
    deferred = _deferred_facts(ws)
    l4 = _l4_facts(ws)
    defects = _defects_facts(ws)
    e2e_pending = sum(1 for m in mods for _ in range(
        m["criteria_layers"].get("L4", 0)))
    report = {"time": datetime.now().isoformat(), "mode": "aggregate",
              "modules": mods, "deferred": deferred, "l4": l4,
              "defects": defects, "e2e_pending_total": e2e_pending}
    _write_health(ws, report)
    n_pass = sum(1 for m in mods if m["acceptance_pass"] and
                 not m["skipped"])
    _log.console_line(f"[porter] P6: 聚合完成——{len(mods)} 模块"
          f"（acceptance PASS {n_pass}，skipped "
          f"{sum(1 for m in mods if m['skipped'])}），"
          f"deferred open {deferred['open']} / cleared "
          f"{deferred['cleared']}，L4 e2e 待定稿 {e2e_pending}，"
          f"defects {defects['total']}")
    _log.console_line(f"[porter] P6: → {ws / 'P6' / 'reports' / 'health.json'}")
    return 0


# ---------- 执行模式 ----------

def _slirp_args(runner: dict) -> str:
    inj = runner.get("inject_device") or {}
    args = ((inj.get("example_args") or {}).get("net-user")) \
        or DEFAULT_EXEC_DEVICE_ARGS
    return args


def _boot_and_log(ws: Path, target_os: Path, runner: dict
                  ) -> tuple[bool, str, str, str]:
    """SLIRP 环境 boot + 取回日志（去 ANSI 判定用文本 + 原文 + state）。

    日志面语义与共享助手一致（missing→复探→infra 关口抢占；empty→
    判定照常）——接共享 _recover_boot_log/_log_face，保持 SLIRP 注入。
    """
    from . import probes as probe_lib
    extra = {}
    env_tpl = ((runner.get("inject_device") or {}).get("env") or {})
    for k in env_tpl:
        extra[k] = _slirp_args(runner)
    if not extra:
        extra = {"EXTRA_QEMU_ARGS": _slirp_args(runner)}

    def _run() -> tuple[bool, str, str]:
        r = probe_mod.probe_boot(ws / "P6", target_os, runner,
                                 extra_env=extra, label="P6_exec_boot")
        raw, state = probe_lib._recover_boot_log(ws / "P6", runner,
                                                 target_os, "P6_exec_boot")
        return bool(r.get("ok")), raw, state

    ok, raw, state = probe_lib._log_face(ws, ws / "P6", runner, target_os,
                                         "P6_exec_boot", _run(), _run)
    return ok, raw, probe_mod._strip_ansi(raw), state


def _run_ktest(ws: Path, target_os: Path, runner: dict
               ) -> tuple[bool, str]:
    """ktest 一轮（复用 runner.unit_test.cmd——已含 console=ttyS0/earlycon
    显式 --kcmd-args 的 §14 修复）。"""
    ut = runner.get("unit_test") or {}
    if ut.get("mechanism") == "none" or not ut.get("cmd"):
        return False, ""
    rc, out = probe_mod._run(ut["cmd"], cwd=target_os,
                             env=probe_mod._base_env(target_os, runner),
                             timeout_sec=int(ut.get("timeout_sec", 1800)),
                             log_path=ws / "P6" / "logs" / "P6_exec_ut.log")
    out = probe_mod._strip_ansi(out)
    ok = rc == 0 and ut.get("success_pattern", "test result: ok") in out
    fp = ut.get("fail_pattern")
    if ok and fp and fp in out:
        ok = False
    return ok, out


def _normalize_sentinels(entries: list[dict]) -> bool:
    changed = False
    for e in entries:
        db = e.get("deferred_by")
        if db and set(db) <= _LEGACY_GLOBAL_SENTINELS \
                and set(db) != {GLOBAL_SENTINEL}:
            e["deferred_by"] = [GLOBAL_SENTINEL]
            changed = True
    return changed


def _clear_deferred_p6(ws: Path, log: str, ut_out: str, l4_map: dict | None,
                        success_pattern: str
                        ) -> tuple[list[str], list[str], list[str], list[str]]:
    """P6 deferred 清偿（含哨兵条目）。返回 (cleared, uncleared, parked, pending)。

    哨兵条目是 P6 的清偿对象（P5 循环不可清）；e2e kind 经定稿后的
    L4 判据（l4_map）判定，park 处置的算泊车不清偿也不算失败；
    未启用 --l4 / 未定稿的 e2e 记 pending（不阻塞，报告可见）。
    """
    d = _load_json(ws / "deferred.json") or {"entries": []}
    cleared: list[str] = []
    uncleared: list[str] = []
    parked: list[str] = []
    pending: list[str] = []
    changed = _normalize_sentinels(d["entries"])
    for e in d["entries"]:
        if e["status"] != "open":
            continue
        c = e.get("criterion") or {}
        ok, detail = None, ""
        if c.get("kind") in ("log_pattern", "counter"):
            ok, n = crit_mod.check_log_pattern(log, c.get("expr", ""))
            detail = f"hits={n}"
        elif c.get("kind") == "unit_test":
            names = [x.strip() for x in c.get("expr", "").split(",")
                     if x.strip()]
            ok, detail = crit_mod.check_unit_test(ut_out, names,
                                                  success_pattern)
        elif c.get("kind") == "e2e":
            lc = (l4_map or {}).get(e["id"])
            if lc and lc["disposition"] == "park":
                parked.append(e["id"])
                e["history"].append({"time": _now(), "ok": None,
                                     "detail": "泊车（定稿处置 park）"})
                changed = True
                continue
            if lc and lc["expr"]:
                ok, n = crit_mod.check_log_pattern(log, lc["expr"])
                detail = f"L4 hits={n}"
            else:
                e["history"].append({"time": _now(), "ok": None,
                                     "detail": "e2e 待 L4 定稿/执行"
                                     "（--finalize-l4 + --execute --l4）"})
                changed = True
                pending.append(e["id"])
                continue
        else:
            ok, detail = False, f"kind {c.get('kind')} 无机器复核路径"
        e["history"].append({"time": _now(), "ok": ok, "detail": detail})
        changed = True
        if ok:
            e["status"] = "cleared"
            cleared.append(e["id"])
        else:
            uncleared.append(e["id"])
    if changed:
        (ws / "deferred.json").write_text(json.dumps(d, ensure_ascii=False,
                                                     indent=2),
                                          encoding="utf-8")
    return cleared, uncleared, parked, pending


def execute(ws: Path, l4: bool = False) -> int:
    """执行模式：一轮 build + SLIRP boot + ktest → 判全部判据 + 清偿。"""
    events.bind(ws, "p6")       # 观测地基（§15 挂载②）
    proj_path = ws / "project.json"
    runner_path = ws / "runner.json"
    if not proj_path.exists() or not runner_path.exists():
        _log.console_line("[porter] P6: 缺 project.json / runner.json（先跑 p0）")
        return 2
    proj = _load_json(proj_path)
    runner = _load_json(runner_path)
    target_os = Path(proj["target_os"])
    (ws / "P6" / "logs").mkdir(parents=True, exist_ok=True)
    (ws / "P6" / "reports").mkdir(parents=True, exist_ok=True)

    l4_doc = load_finalized_l4(ws)
    if l4 and l4_doc is None:
        _log.console_line("[porter] P6: --l4 需要 finalized 的 l4_criteria.json"
              "（先 `p6 --finalize-l4`）")
        return 2
    l4_map = {c["id"]: c for c in (l4_doc or {}).get("criteria", [])} \
        if l4_doc else {}

def _execute_judge(ws: Path, target_os: Path, runner: dict,
                    l4: bool, l4_map: dict
                    ) -> tuple[list[dict], str, str, str]:
    """execute 判定核心（纯判定，无 deferred 清偿副作用——供求解循环
    复验复用）。返回 (results, boot_log, ut_out, log_state)。"""
    results: list[dict] = []

    def rec(cid, layer, ok, detail):
        results.append({"id": cid, "layer": layer, "ok": ok,
                        "detail": detail})

    # L1 build
    b = probe_mod.probe_build(ws / "P6", target_os, runner,
                              label="P6_exec_build")
    rec("P6.build", "L1", b["ok"], b["detail"])
    # L2 boot（SLIRP 环境）
    boot_ok, _raw, log, log_state = _boot_and_log(ws, target_os, runner)
    rec("P6.boot", "L2", boot_ok, "SLIRP boot 双信号" +
        ("PASS" if boot_ok else "FAIL"))
    if log_state == "missing":
        return results, log, "", log_state
    # ktest（L0）
    ut_ok, ut_out = _run_ktest(ws, target_os, runner)
    ut = runner.get("unit_test") or {}
    ut_mech_none = ut.get("mechanism") == "none"

    # 全部模块判据重判（L1/L2 用本轮全局结果；deferred_by 非空项归
    # deferred 清偿通道，不直接判）
    mods = _module_facts(ws)
    for m in mods:
        cpath = ws / "P3" / m["module"] / "reports" / "criteria.json"
        cs = ((_load_json(cpath) or {}).get("criteria")) or []
        for c in cs:
            if c.get("deferred_by"):
                continue
            kind = c["kind"]
            if kind == "compile":
                rec(c["id"], "L1", b["ok"], "复用本轮全局 build")
            elif kind == "boot":
                rec(c["id"], "L2", boot_ok, "复用本轮全局 boot（SLIRP）")
            elif kind == "unit_test":
                if ut_mech_none or not ut.get("cmd"):
                    continue
                names = [x.strip() for x in c["expr"].split(",")
                         if x.strip()]
                ok, detail = crit_mod.check_unit_test(
                    ut_out, names,
                    ut.get("success_pattern", "test result: ok"))
                rec(c["id"], "L0", ok, detail)
            elif kind in ("log_pattern", "counter"):
                ok, n = crit_mod.check_log_pattern(log, c["expr"])
                rec(c["id"], "L3", ok, f"hits={n}")
            elif kind == "e2e":
                if not l4:
                    rec(c["id"], "L4", None, "未启用 --l4")
                    continue
                lc = l4_map.get(c["id"])
                if lc is None:
                    rec(c["id"], "L4", None, "未入定稿清单（定稿时 drop？）")
                    continue
                if lc["disposition"] == "park":
                    rec(c["id"], "L4", None, "泊车：" + lc["rationale"])
                    continue
                ok, n = crit_mod.check_log_pattern(log, lc["expr"])
                rec(c["id"], "L4", ok, f"L4 hits={n}（{lc['form']}）")

    # 定稿清单中的新增判据（不在模块 criteria 里的，如 eth0 驱动级代位）
    judged_ids = {r["id"] for r in results}
    for lc in l4_map.values():
        if lc["id"] in judged_ids:
            continue
        if lc["disposition"] == "park":
            rec(lc["id"], "L4", None, "泊车：" + lc["rationale"])
            continue
        ok, n = crit_mod.check_log_pattern(log, lc["expr"])
        rec(lc["id"], "L4", ok, f"L4 hits={n}（{lc['form']}，新增）")
    return results, log, ut_out, log_state


def _red_detail(results: list[dict], uncleared: list[str]) -> str:
    lines = [f"- {r['id']}：{r['detail']}"
             for r in results if r["ok"] is False]
    lines += [f"- {i}（deferred 未清偿）" for i in uncleared]
    return "\n".join(lines)[:2000]


def execute(ws: Path, l4: bool = False) -> int:
    """执行模式：一轮 build + SLIRP boot + ktest → 判全部判据 + 清偿。

    红项（FAIL 判据 + 未清偿 deferred）→ 失败即快照 → 求解循环
    （错误处理挂载②；复验 = 重跑判定核心）；未解决 → p6.unsolved
    关口（rc 3）。熔断关 → 红项直接进 verdict（旧 bypass 语义）。
    """
    events.bind(ws, "p6")       # 观测地基（挂载②）
    proj_path = ws / "project.json"
    runner_path = ws / "runner.json"
    if not proj_path.exists() or not runner_path.exists():
        _log.console_line("[porter] P6: 缺 project.json / runner.json（先跑 p0）")
        return 2
    proj = _load_json(proj_path)
    runner = _load_json(runner_path)
    target_os = Path(proj["target_os"])
    (ws / "P6" / "logs").mkdir(parents=True, exist_ok=True)
    (ws / "P6" / "reports").mkdir(parents=True, exist_ok=True)

    l4_doc = load_finalized_l4(ws)
    if l4 and l4_doc is None:
        _log.console_line("[porter] P6: --l4 需要 finalized 的 l4_criteria.json"
              "（先 `p6 --finalize-l4`）")
        return 2
    l4_map = {c["id"]: c for c in (l4_doc or {}).get("criteria", [])} \
        if l4_doc else {}

    results, log, ut_out, log_state = _execute_judge(
        ws, target_os, runner, l4, l4_map)
    if log_state == "missing":
        # 抢占（H9 重构）：判定输入不存在——本轮不判任何日志类判据，
        # infra 关口已登记；health 落盘后 rc 3
        results.append({"id": "P6.infra_log", "layer": "infra", "ok": None,
                        "detail": "判定中止：boot 日志不可得"
                                  "（infra 关口待答）"})
        report = {"time": datetime.now().isoformat(), "mode": "execute",
                  "infra": "boot_no_log", "results": results,
                  "verdict": {"all_green_except_parked": False,
                              "failing": [], "deferred_cleared": [],
                              "deferred_uncleared": [],
                              "deferred_pending_l4": [], "parked": []},
                  "solve": []}
        _write_health(ws, report)
        _log.console_line("[porter] P6: execute 判定中止（boot 日志不可得，"
              "infra 关口待答）——exit 3")
        return 3
    ut = runner.get("unit_test") or {}
    success_pattern = ut.get("success_pattern", "test result: ok")

    # deferred 清偿（P6 是哨兵 owner）
    cleared, uncleared, parked_def, pending_def = _clear_deferred_p6(
        ws, log, ut_out, l4_map if l4 else None, success_pattern)

    hard_fail = [r["id"] for r in results if r["ok"] is False]
    all_parked = set(parked_def)
    uncleared_real = [i for i in uncleared if i not in all_parked]

    # ---- 红项求解（错误处理挂载②）：失败即快照 → 求解循环（复验 =
    #      重跑判定核心——纯判定无副作用）----
    solve_outcome: dict | None = None
    red = hard_fail + list(uncleared_real)
    if red:
        snap = events.take_failure_snapshot(
            ws, "p6", "P6-red", f"{len(red)} 红项：{', '.join(red)[:200]}",
            runner=runner, target_os=target_os,
            extra_files=[(ws / "P6" / "reports" / "l4_criteria.json",
                          "l4_criteria.json")]
            if (ws / "P6" / "reports" / "l4_criteria.json").exists() else None)
        from . import gates as _gates_sd
        if _gates_sd.self_diagnosis_enabled():
            from . import errorloop as EL
            st = {"results": results, "log": log, "ut_out": ut_out,
                  "log_state": ""}

            def verify():
                res2, log2, ut2, ls2 = _execute_judge(
                    ws, target_os, runner, l4, l4_map)
                st.update(results=res2, log=log2, ut_out=ut2,
                          log_state=ls2)
                if ls2 == "missing":
                    return False, {"detail": "boot 日志不可得"
                                            "（infra 关口）——求解中止"}
                hard2 = [r["id"] for r in res2 if r["ok"] is False]
                if not hard2:
                    return True, None
                return False, {"detail": _red_detail(res2, []),
                               "boot_log": st["log"][-4000:],
                               "ut_out": st["ut_out"][-4000:]}

            failure = {
                "source": "p6", "subject": "P6-red", "module": None,
                "kind": "red-set", "detail": _red_detail(results,
                                                         uncleared_real),
                "boot_log": log[-4000:], "ut_out": ut_out[-4000:],
                "runner": runner, "snapshot": snap.name if snap else None,
                "deferred_uncleared": uncleared_real or None,
                "_workdir": target_os}
            solve_outcome = EL.run_solve_loop(ws, failure, verify)
            if solve_outcome["status"] in ("solved",):
                # 复验现场为准 + 重清偿（现场已变化）
                results, log, ut_out = (st["results"], st["log"],
                                        st["ut_out"])
                cleared, uncleared, parked_def, pending_def = \
                    _clear_deferred_p6(ws, log, ut_out,
                                       l4_map if l4 else None,
                                       success_pattern)
                hard_fail = [r["id"] for r in results if r["ok"] is False]
                all_parked = set(parked_def)
                uncleared_real = [i for i in uncleared
                                  if i not in all_parked]
            elif st["log_state"] == "missing":
                report = {"time": datetime.now().isoformat(),
                          "mode": "execute", "infra": "boot_no_log",
                          "results": results,
                          "verdict": {"all_green_except_parked": False,
                                      "failing": [],
                                      "deferred_cleared": cleared,
                                      "deferred_uncleared": [],
                                      "deferred_pending_l4": pending_def,
                                      "parked": sorted(all_parked)},
                          "solve": solve_outcome.get("rounds") or []}
                _write_health(ws, report)
                _log.console_line("[porter] P6: 求解复跑后日志不可得"
                      "（infra 关口待答）——exit 3")
                return 3
        else:
            _log.console_line("[porter] P6: §15 熔断关——红项不走求解"
                  "（人工/重开熔断接管）")
    verdict = {"all_green_except_parked":
               not hard_fail and not uncleared_real,
               "failing": hard_fail,
               "deferred_cleared": cleared,
               "deferred_uncleared": uncleared_real,
               "deferred_pending_l4": pending_def,
               "parked": sorted(all_parked)}
    report = {"time": datetime.now().isoformat(), "mode": "execute",
              "device_args": _slirp_args(runner), "l4_enabled": l4,
              "modules": _module_facts(ws), "results": results,
              "verdict": verdict,
              "solve": (solve_outcome or {}).get("rounds") or [],
              "deferred": _deferred_facts(ws), "defects": _defects_facts(ws),
              "l4": _l4_facts(ws)}
    _write_health(ws, report)

    for r in results:
        mark = "PASS" if r["ok"] else ("DEFER" if r["ok"] is None
                                       else "FAIL")
        _log.console_line(f"[porter] P6: {r['id']:<44} {mark}  {r['detail']}")
    _log.console_line(f"[porter] P6: deferred 清偿 {len(cleared)} 笔"
          f"（未清偿 {len(uncleared_real)}，泊车 {len(all_parked)}，"
          f"待 L4 {len(pending_def)}）")
    _log.console_line(f"[porter] P6: 判定 {'ALL GREEN（泊车除外）' if verdict['all_green_except_parked'] else 'FAIL'}")
    if not verdict["all_green_except_parked"]:
        if solve_outcome is not None and solve_outcome["status"] != "solved":
            # 求解在场且未解决（含 parked/rehung——已登记，停人定夺）
            return _p6_unsolved_gate(ws, solve_outcome)
    return 0 if verdict["all_green_except_parked"] else 1


def _p6_unsolved_gate(ws: Path, outcome: dict) -> int:
    """p6 红项求解未解决 → 关口（retry 语义；报告作 context）。"""
    from . import gates as gates_mod
    ctx = ["P6/reports/health.json"]
    if outcome.get("report_path"):
        ctx.append(outcome["report_path"])
    return gates_mod.panic(ws, {
        "id": "p6.unsolved", "kind": "retry", "gate_type": "failure",
        "phase": "P6", "subject": "P6-red",
        "question": (
            f"P6 红项求解循环未解决（终态 {outcome.get('status')}，"
            f"{len(outcome.get('rounds') or [])} 轮）。升级报告/快照/轮次"
            "总结在手；人工修复后作答，或重跑 `p6 --execute` 复核。"),
        "context_files": ctx,
        "answer_form": [
            {"field": "note", "type": "text", "required": False,
             "hint": "诊断笔记（根因与修复）"}],
    })


# ---------- 红项分诊（§15 挂载②）+ 缺陷诊断入口（挂载③） ----------

def diagnose_defect(ws: Path, did: str, cfg: dict | None = None) -> int:
    """挂载③（错误处理模块按需入口 d1）：对单个缺陷跑求解循环。

    defect → 证据包 → solve（≤3 轮，检索 failures 知识）→ 动作执行
    → 复验（build+boot 双信号）→ 解决：四字段闭账 + CP4 决策债
    （--defect-fix 已并入此处）；未解决：升级报告 + d1.unsolved 关口。
    熔断关 / PORTER_NO_AGENT → rc 2（人工路径指引）。
    """
    from . import errorloop as EL
    from . import gates as _gates_sd
    if not _gates_sd.self_diagnosis_enabled():
        _log.console_line(f"[porter] P6: §15 熔断关——--defect-diagnose {did}"
              " 不可用。人工诊断可翻 events.jsonl / 各相位 logs；修好后"
              " --defect-close 闭账；或重开熔断（self_diagnosis.enabled）")
        return 2
    events.bind(ws, "d1")
    d = load_defects(ws)
    e = _find_defect(d, did)
    if not e:
        _log.console_line(f"[porter] P6: 缺陷不存在: {did}（先 --defect-add）")
        return 2
    if e.get("status") == "fixed":
        _log.console_line(f"[porter] P6: 缺陷 {did} 已 fixed——无需处理")
        return 0
    proj = _load_json(ws / "project.json") or {}
    target_os = Path(proj.get("target_os") or ws)
    runner = _load_json(ws / "runner.json") or {}
    import os as _os
    if _os.environ.get("PORTER_NO_AGENT"):
        _log.console_line("[porter] P6: PORTER_NO_AGENT=1——求解循环不可用"
              "（人工修后 --defect-close）")
        return 2

    safe = re.sub(r"[^A-Za-z0-9._-]", "_", did)[:60]
    st = {"log_state": ""}

    def verify():
        b = probe_mod.probe_build(ws / "P6", target_os, runner,
                                  label=f"D1_{safe}_build")
        if not b["ok"]:
            return False, {"detail": f"build FAIL {b['detail']}",
                           "build_out": (b.get("out") or "")[-4000:]}
        boot_ok, _raw, log2, ls2 = _boot_and_log(ws, target_os, runner)
        if ls2 == "missing":
            st["log_state"] = "missing"
            return False, {"detail": "boot 日志不可得（infra 关口）——"
                                    "求解中止"}
        if not boot_ok:
            return False, {"detail": "boot 双信号 FAIL",
                           "boot_log": log2[-4000:]}
        return True, None

    failure = {"source": "d1", "subject": did, "module": None,
               "detail": e.get("discovered", {}).get("evidence", ""),
               "defect": e, "_workdir": target_os}
    outcome = EL.run_solve_loop(ws, failure, verify, cfg=cfg)
    status = outcome.get("status")

    for r in outcome.get("rounds") or []:
        if r.get("action"):
            bump_defect(ws, did, "solve-round",
                        f"R{r.get('round')} {r.get('action')}"
                        f"（{'; '.join(r.get('applied') or [])[:160]}）")

    if status == "bypass":
        return 2
    if status == "no-agent":
        return 2
    if st.get("log_state") == "missing":
        bump_defect(ws, did, "solve-infra-stop", "验证中止：boot 日志不可得")
        _log.console_line("[porter] P6: 求解验证中止（infra 关口待答）——rc 3")
        return 3
    if status == "solved":
        _close_fixed_defect(ws, did, outcome)
        return 0
    if status == "parked":
        bump_defect(ws, did, "parked", "求解循环泊车（P7 上游素材）")
        _log.console_line(f"[porter] P6: ✔ 缺陷 {did} 泊车（platform_patches"
              " 已登记）")
        return 0
    if status == "rehung":
        bump_defect(ws, did, "rehung", "求解循环改挂真实消费者")
        return 0

    # unsolved / early-exit / escalated：报告已生成 → 关口
    bump_defect(ws, did, "escalated",
                f"求解未解决（{status}）——升级报告见 escalations/")
    from . import gates as gates_mod
    ctx = []
    if outcome.get("report_path"):
        ctx.append(outcome["report_path"])
    return gates_mod.panic(ws, {
        "id": f"d1.unsolved.{did}", "kind": "retry", "gate_type": "failure",
        "phase": "d1", "subject": did,
        "question": (
            f"缺陷 {did} 求解循环未解决（终态 {status}，"
            f"{len(outcome.get('rounds') or [])} 轮）。升级报告/轮次总结"
            "在手；人工修复后 --defect-close 闭账，或作答后重跑"
            " --defect-diagnose。"),
        "context_files": ctx,
        "answer_form": [
            {"field": "note", "type": "text", "required": False,
             "hint": "诊断笔记（根因与修复）"}],
    })


def _close_fixed_defect(ws: Path, did: str, outcome: dict) -> None:
    """d1 求解解决 → 四字段闭账 + CP4 决策债（批审闭账证据链）。"""
    rounds = outcome.get("rounds") or []
    last = next((r for r in reversed(rounds) if r.get("action")), None)
    reg_ev = (f"build+boot PASS @{_now()}；求解日志 solve/logs/"
              f"SOLVE_{re.sub(r'[^A-Za-z0-9._-]', '_', did)[:60]}_R*.log")
    close_defect(
        ws, did,
        root_cause=str((last or {}).get("summary") or "solve 循环修复"
                      )[:400],
        fix="; ".join(a for r in rounds for a in r.get("applied") or [])
        or "fix-code（见 solve/logs）",
        regression_evidence=reg_ev)
    bump_defect(ws, did, "fixed-auto",
                "--defect-diagnose 求解闭账（CP4 批审）")
    from . import gates as gates_mod
    led = gates_mod.GateLedger(ws).load()
    gid = f"p6.defect.fix.{did}"
    if led.find(gid) is None:
        led.add(id=gid, kind="decision", lane="checkpoint",
                gate_type="failure", phase="P6", checkpoint="CP4",
                subject=did, blocking=False,
                question=(f"缺陷 {did} 已由求解循环自动闭账"
                          "（四字段+build/boot 证据）——CP4 批审闭账。"),
                context_files=["defects.json"],
                answer_form=[
                    {"field": "verdict", "type": "enum",
                     "options": ["approve", "reject"], "required": True}])
    led.mark(gid, "applied", answer={"verdict": "approve"},
             answered_by="agent", answered_at=_now(),
             resolution=reg_ev[:300])
    _log.console_line(f"[porter] P6: ✔ 缺陷 {did} 求解闭账（决策债 {gid}，"
          "CP4 批审）")


# ---------- health 报告 ----------

def _write_health(ws: Path, report: dict) -> None:
    rp = ws / "P6" / "reports"
    (rp / "health.json").write_text(json.dumps(report, ensure_ascii=False,
                                               indent=2), encoding="utf-8")
    mods = report.get("modules") or []
    lines = ["# P6 系统验收健康报告", "",
             f"- 时间：{report.get('time')}",
             f"- 模式：{report.get('mode')}"
             + (f"（设备参数 `{report.get('device_args')}`，"
                f"l4={'on' if report.get('l4_enabled') else 'off'}）"
                if report.get("mode") == "execute" else ""), "",
             "## 模块", "",
             "| 模块 | 相位 | skipped | 验收 | 判据(L0/L1/L2/L3/L4) |",
             "|---|---|---|---|---|"]
    for m in mods:
        ly = m.get("criteria_layers") or {}
        acc = m.get("acceptance_pass")
        acc_s = "—" if acc is None else ("PASS" if acc else "FAIL") \
            + ("（旧位置）" if m.get("acceptance_legacy") else "")
        lines.append(f"| {m['module']} | {m.get('phase')} "
                     f"| {'是' if m.get('skipped') else ''} | {acc_s} "
                     f"| {ly.get('L0', 0)}/{ly.get('L1', 0)}/"
                     f"{ly.get('L2', 0)}/{ly.get('L3', 0)}/"
                     f"{ly.get('L4', 0)} |")
    df = report.get("deferred") or {}
    lines += ["", "## deferred", "",
              f"- open {df.get('open', 0)} / cleared "
              f"{df.get('cleared', 0)}（哨兵=归系统验收）", ""]
    for e in df.get("open_entries") or []:
        lines.append(f"- {e['id']}（{e.get('kind')}，"
                     f"{'哨兵' if e.get('sentinel') else e.get('deferred_by')}）"
                     f" expr=`{e.get('expr')}`")
    if report.get("mode") == "execute":
        v = report.get("verdict") or {}
        lines += ["", "## 执行判定", "",
                  f"- 结论：{'ALL GREEN（泊车除外）' if v.get('all_green_except_parked') else 'FAIL'}",
                  f"- deferred 清偿：{', '.join(v.get('deferred_cleared') or []) or '无'}",
                  f"- 未清偿：{', '.join(v.get('deferred_uncleared') or []) or '无'}",
                  f"- 待 L4：{', '.join(v.get('deferred_pending_l4') or []) or '无'}",
                  f"- 泊车：{', '.join(v.get('parked') or []) or '无'}",
                  f"- 失败判据：{', '.join(v.get('failing') or []) or '无'}",
                  "", "## 判据明细", "",
                  "| 判据 | 层 | 结果 | 说明 |", "|---|---|---|---|"]
        for r in report.get("results") or []:
            mark = "PASS" if r["ok"] else ("DEFER" if r["ok"] is None
                                           else "FAIL")
            lines.append(f"| {r['id']} | {r['layer']} | {mark} "
                         f"| {r['detail']} |")
    tri = report.get("solve") or []
    if tri:
        lines += ["", "## 红项求解循环（错误处理挂载②）", "",
                  "| 轮 | 动作 | 归责 | 复验 | 处置 |", "|---|---|---|---|---|"]
        for v in tri:
            lines.append(f"| R{v.get('round')} | {v.get('action')} "
                         f"| {v.get('circuit')} "
                         f"| {'PASS' if v.get('verified') else '—'} "
                         f"| {'; '.join(v.get('applied') or [])[:160]} |")
    dt = report.get("defects") or {}
    if dt.get("total"):
        lines += ["", "## defects", "",
                  f"- 总数 {dt['total']}：{dt.get('by_status')}"]
    l4f = report.get("l4") or {}
    if l4f.get("exists"):
        lines += ["", "## L4 定稿", "",
                  f"- 状态 {l4f.get('status')}，条目 {l4f.get('total')}"
                  f"（park {l4f.get('park')}）"]
    (rp / "health.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------- 入口 ----------

# ---------- L4 草案生成（S5：修 H1/H2——"出题"自动化，人审归 CP3/定稿门） ----------

_FORM_BY_KIND = {"log_pattern": "boot观测", "counter": "boot观测",
                 "unit_test": "内核自测", "e2e": "流量驱动"}
_DRAFT_TRIES = 2                     # agent 草案回炉上限


def _collect_l4_material(ws: Path) -> list[dict]:
    """机器段素材：deferred 全局哨兵条目 + 各模块 e2e 判据（预分类）。"""
    mat: list[dict] = []
    seen: set[str] = set()
    dp = ws / "deferred.json"
    if dp.exists():
        try:
            d = json.loads(dp.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            d = {}
        for e in d.get("entries", []):
            if not _is_global(e.get("deferred_by")):
                continue
            c = e.get("criterion") or {}
            cid = c.get("id") or e.get("id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            mat.append({"id": cid, "source": f"deferred[{e.get('status')}]",
                        "kind": c.get("kind"), "expr": c.get("expr") or "",
                        "module": e.get("module")})
    for crit_path in sorted((ws / "P3").glob("*/reports/criteria.json")):
        try:
            crit = json.loads(crit_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for c in crit.get("criteria", []):
            if c.get("kind") != "e2e" or c.get("id") in seen:
                continue
            seen.add(c["id"])
            mat.append({"id": c["id"], "source": "P3.e2e", "kind": "e2e",
                        "expr": c.get("expr") or "",
                        "module": crit_path.parts[-3]})
    return mat


def _machine_draft(ws: Path, mat: list[dict]) -> list[dict]:
    """无 agent 兜底草案：kind→form 预分类；无 expr 的条目 park（schema
    要求 clear 必有 expr——宁可泊车给人审，不造假正则）。"""
    out = []
    for m in mat:
        form = _FORM_BY_KIND.get(m.get("kind") or "", "流量驱动")
        expr = m.get("expr") or ""
        if expr:
            out.append({"id": m["id"], "title": f"{m['id']}（机器预分类）",
                        "form": form, "expr": expr,
                        "rationale": (f"机器预分类 {m.get('kind')} → {form}"
                                      "（agent 不可用/失败，请人工补全理由）"),
                        "disposition": "clear"})
        else:
            out.append({"id": m["id"], "title": f"{m['id']}（机器预分类）",
                        "form": form, "expr": "",
                        "rationale": (f"机器预分类 {m.get('kind')} → {form}；"
                                      "素材无正则且 agent 不可用——请人审时"
                                      "补 expr 改 clear，或维持 park"),
                        "disposition": "park"})
    return out


def draft_l4(ws: Path, cfg: dict | None = None) -> int:
    """`p6 --draft-l4`：deferred/__P6__ + P3 e2e 判据 → l4_criteria.json 草案。

    机器段预分类 → agent（skill L4-draft）补全六字段 → validate_l4 复核
    （失败带反馈回炉 ≤2）→ agent 不可用/失败时落机器草案（占位 rationale，
    定稿门前人审补全）。修 H1（草案无生成器）/H2（出题工具化）。
    """
    import os
    from ..common import agent as agent_mod
    events.bind(ws, "p6")
    path = l4_criteria_path(ws)
    if path.exists() and (json.loads(path.read_text(encoding="utf-8"))
                          .get("status")) in ("draft", "finalized"):
        _log.console_line(f"[porter] P6: L4 草案已存在（{path}）——跳过（删文件可重生成）")
        return 0
    mat = _collect_l4_material(ws)
    if not mat:
        _log.console_line("[porter] P6: 无 L4 素材（deferred 无全局哨兵条目、P3 无 "
              "e2e 判据）——先跑 loop/p6")
        return 2
    pp_open = []
    try:
        pp = json.loads((ws / "platform_patches.json").read_text(
            encoding="utf-8"))
        pp_open = [p.get("gap") for p in pp.get("patches", [])
                   if p.get("status") in ("proposed", "planned")]
    except (OSError, json.JSONDecodeError):
        pass

    criteria = None
    agent_ok = False
    if not os.environ.get("PORTER_NO_AGENT"):
        skill = agent_mod.load_skill("L4-draft")
        mat_block = json.dumps(mat, ensure_ascii=False, indent=1)
        pp_block = "、".join(pp_open) if pp_open else "无"
        feedback = ""
        for attempt in range(1, _DRAFT_TRIES + 1):
            prompt = (f"{skill}\n\n---\n\n## 素材（机器预分类）\n{mat_block}"
                      f"\n\n## 已知平台缺口（相关判据建议 park）\n{pp_block}"
                      f"\n\n## 任务\n产出全部条目的六字段草案（紧凑 JSON "
                      "数组）。{feedback}")
            rc, out = agent_mod.run_agent(
                prompt, workdir=ws, timeout_sec=900,
                log_stem=str(ws / "P6" / "logs" / f"L4_draft_R{attempt}"))
            parsed = agent_mod.extract_json(out) if rc == 0 else None
            cand = parsed.get("criteria") if isinstance(parsed, dict) \
                else parsed
            ok_items, errs = validate_l4(cand if isinstance(cand, list)
                                         else [])
            if cand and not errs:
                criteria = ok_items
                agent_ok = True
                break
            feedback = (f"\n\n---\n\n## 上次输出的问题（修正后重输出）\n"
                        + "; ".join(errs[:8]))
    if criteria is None:
        criteria = _machine_draft(ws, mat)
        _log.console_line("[porter] P6: agent 草案不可用——落机器预分类草案"
              "（rationale 占位，人审时补全）")
    doc = {"status": "draft", "generated": _now(), "criteria": criteria}
    path.parent.mkdir(parents=True, exist_ok=True)
    (ws / "P6" / "logs").mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    n_form = {f: sum(1 for c in criteria if c["form"] == f)
              for f in L4_FORMS}
    print(f"[porter] P6: L4 草案就绪（{len(criteria)} 条："
          + "，".join(f"{k} {v}" for k, v in n_form.items() if v)
          + f"）→ {path}")
    _log.console_line("[porter] P6: 人审入口 = `p6 --finalize-l4`（CP3：审批关口）")
    events.append_event("l4-draft", subject="draft-l4",
                        summary=f"{len(criteria)} 条（agent="
                                f"{'ok' if agent_ok else 'machine'}）")
    return 0


# ---------- 缺陷修复步（S5：修 H4——诊断→修复→验证→自动闭账→CP4 批审） ----------

_FIX_TRIES = 2


def fix_defect(ws: Path, did: str, cfg: dict | None = None) -> int:
    """`p6 --defect-fix ID`：已并入 --defect-diagnose（求解循环含修复
    +双信号验证+四字段闭账+CP4 决策债——2026-09-03 §15 重设计定案）。
    本入口保留为重定向垫片。
    """
    _log.console_line(f"[porter] P6: --defect-fix {did} 已并入 --defect-diagnose"
          "（求解循环含修复/验证/闭账）—— redirecting")
    return diagnose_defect(ws, did, cfg)


def run_p6(ws: Path, execute_flag: bool = False, l4: bool = False,
           finalize_flag: bool = False, draft_flag: bool = False,
           defect_fix: str | None = None,
           cfg: dict | None = None) -> int:
    if draft_flag:
        return draft_l4(ws, cfg)
    if defect_fix:
        return fix_defect(ws, defect_fix, cfg)
    if finalize_flag:
        return finalize_l4(ws, cfg)
    if execute_flag:
        return execute(ws, l4=l4)
    return aggregate(ws)
