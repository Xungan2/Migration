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
        print(f"[porter] P6: 缺少草案 {path}——先按 P6-3 内容设计起草再定稿")
        return 2
    doc = json.loads(path.read_text(encoding="utf-8"))
    ok_items, errs = validate_l4(doc.get("criteria") or [])
    if errs:
        print(f"[porter] P6: L4 判据草案 schema 错误 {len(errs)} 处：")
        for e in errs:
            print(f"  - {e}")
        return 1
    mode = review_gate_mode(cfg)
    if mode == "human" and not _released(ws):
        doc["status"] = "draft"
        path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        _write_l4_review(ws, ok_items)
        q = ws / "human_questions.md"
        block = ("\n\n---\n\n## P6 L4 判据定稿审核门（exit 3）\n"
                 f"- 时间：{_now()}\n"
                 f"- 草案：{len(ok_items)} 条已落盘 {path}\n"
                 "- 评审摘要：P6/reports/l4_criteria_REVIEW.md\n"
                 "- 放行：审阅后在 answers.md 写一行"
                 " `l4_criteria_finalization: approve` 再重跑"
                 " `p6 --finalize-l4`。\n")
        with q.open("a", encoding="utf-8") as f:
            f.write(block)
        print(f"[porter] P6: 审核门 human——草案落盘停车（exit 3），"
              "等 answers.md 放行")
        return 3
    doc["status"] = "finalized"
    doc["finalized_time"] = _now()
    doc["criteria"] = ok_items
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    print(f"[porter] P6: L4 判据定稿完成（{len(ok_items)} 条，"
          f"park {sum(1 for c in ok_items if c['disposition'] == 'park')}）")
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
        print("[porter] P6: 无 loop_state.json / 模块信息——工作区前置缺失")
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
    print(f"[porter] P6: 聚合完成——{len(mods)} 模块"
          f"（acceptance PASS {n_pass}，skipped "
          f"{sum(1 for m in mods if m['skipped'])}），"
          f"deferred open {deferred['open']} / cleared "
          f"{deferred['cleared']}，L4 e2e 待定稿 {e2e_pending}，"
          f"defects {defects['total']}")
    print(f"[porter] P6: → {ws / 'P6' / 'reports' / 'health.json'}")
    return 0


# ---------- 执行模式 ----------

def _slirp_args(runner: dict) -> str:
    inj = runner.get("inject_device") or {}
    args = ((inj.get("example_args") or {}).get("net-user")) \
        or DEFAULT_EXEC_DEVICE_ARGS
    return args


def _boot_and_log(ws: Path, target_os: Path, runner: dict
                  ) -> tuple[bool, str, str]:
    """SLIRP 环境 boot + 取回日志（去 ANSI 判定用文本 + 原文）。"""
    extra = {}
    env_tpl = ((runner.get("inject_device") or {}).get("env") or {})
    for k in env_tpl:
        extra[k] = _slirp_args(runner)
    if not extra:
        extra = {"EXTRA_QEMU_ARGS": _slirp_args(runner)}
    r = probe_mod.probe_boot(ws / "P6", target_os, runner,
                             extra_env=extra, label="P6_exec_boot")
    lf = (runner.get("boot") or {}).get("log_file")
    raw = ""
    if lf:
        p = Path(lf) if Path(lf).is_absolute() else target_os / lf
        if p.exists():
            raw = p.read_text(encoding="utf-8", errors="replace")
    return bool(r.get("ok")), raw, probe_mod._strip_ansi(raw)


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
        print("[porter] P6: 缺 project.json / runner.json（先跑 p0）")
        return 2
    proj = _load_json(proj_path)
    runner = _load_json(runner_path)
    target_os = Path(proj["target_os"])
    (ws / "P6" / "logs").mkdir(parents=True, exist_ok=True)
    (ws / "P6" / "reports").mkdir(parents=True, exist_ok=True)

    l4_doc = load_finalized_l4(ws)
    if l4 and l4_doc is None:
        print("[porter] P6: --l4 需要 finalized 的 l4_criteria.json"
              "（先 `p6 --finalize-l4`）")
        return 2
    l4_map = {c["id"]: c for c in (l4_doc or {}).get("criteria", [])} \
        if l4_doc else {}

    results: list[dict] = []

    def rec(cid, layer, ok, detail):
        results.append({"id": cid, "layer": layer, "ok": ok,
                        "detail": detail})

    # L1 build
    b = probe_mod.probe_build(ws / "P6", target_os, runner,
                              label="P6_exec_build")
    rec("P6.build", "L1", b["ok"], b["detail"])
    # L2 boot（SLIRP 环境）
    boot_ok, _raw, log = _boot_and_log(ws, target_os, runner)
    rec("P6.boot", "L2", boot_ok, "SLIRP boot 双信号" +
        ("PASS" if boot_ok else "FAIL"))
    # ktest（L0）
    ut_ok, ut_out = _run_ktest(ws, target_os, runner)
    ut = runner.get("unit_test") or {}
    success_pattern = ut.get("success_pattern", "test result: ok")
    rec("P6.ktest", "L0", (ut_ok if ut.get("cmd") else None),
        ("整体 ktest " + ("PASS" if ut_ok else "FAIL"))
        if ut.get("cmd") else "mechanism=none")
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
                ok, detail = crit_mod.check_unit_test(ut_out, names,
                                                      success_pattern)
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

    # deferred 清偿（P6 是哨兵 owner）
    cleared, uncleared, parked_def, pending_def = _clear_deferred_p6(
        ws, log, ut_out, l4_map if l4 else None, success_pattern)

    hard_fail = [r["id"] for r in results if r["ok"] is False]
    all_parked = set(parked_def)
    uncleared_real = [i for i in uncleared if i not in all_parked]

    # ---- 红项分诊（§15 挂载②）：失败即快照 → 逐红项 triage（不重跑，
    #      重跑 = 再次执行 p6 --execute 本身）----
    triage_section = _triage_red_items(ws, target_os, runner, results,
                                       uncleared_real, log, ut_out, mods,
                                       cfg=None)
    verdict = {"all_green_except_parked":
               bool(b["ok"]) and not hard_fail and not uncleared_real,
               "failing": hard_fail,
               "deferred_cleared": cleared,
               "deferred_uncleared": uncleared_real,
               "deferred_pending_l4": pending_def,
               "parked": sorted(all_parked)}
    report = {"time": datetime.now().isoformat(), "mode": "execute",
              "device_args": _slirp_args(runner), "l4_enabled": l4,
              "modules": mods, "results": results, "verdict": verdict,
              "triage": triage_section,
              "deferred": _deferred_facts(ws), "defects": _defects_facts(ws),
              "l4": _l4_facts(ws)}
    _write_health(ws, report)

    for r in results:
        mark = "PASS" if r["ok"] else ("DEFER" if r["ok"] is None
                                       else "FAIL")
        print(f"[porter] P6: {r['id']:<44} {mark}  {r['detail']}")
    print(f"[porter] P6: deferred 清偿 {len(cleared)} 笔"
          f"（未清偿 {len(uncleared_real)}，泊车 {len(all_parked)}，"
          f"待 L4 {len(pending_def)}）")
    print(f"[porter] P6: 判定 {'ALL GREEN（泊车除外）' if verdict['all_green_except_parked'] else 'FAIL'}")
    return 0 if verdict["all_green_except_parked"] else 1


# ---------- 红项分诊（§15 挂载②）+ 缺陷诊断入口（挂载③） ----------

def _criterion_by_id(ws: Path, mods: list[dict], cid: str) -> dict | None:
    for m in mods:
        p = ws / "P3" / m["module"] / "reports" / "criteria.json"
        c = _load_json(p)
        for x in (c or {}).get("criteria") or []:
            if x["id"] == cid:
                return x
    return None


def _triage_red_items(ws: Path, target_os: Path, runner: dict,
                      results: list[dict], uncleared: list[str],
                      log: str, ut_out: str, mods: list[dict],
                      cfg: dict | None) -> list[dict]:
    """红项（FAIL 判据 + 未清偿 deferred）分诊：先快照再逐项 triage。"""
    red = [r["id"] for r in results if r["ok"] is False] + list(uncleared)
    if not red:
        return []
    from . import diagnose, triage as triage_mod
    snap = events.take_failure_snapshot(
        ws, "p6", "P6-red",
        f"{len(red)} 红项：{', '.join(red)[:200]}",
        runner=runner, target_os=target_os,
        extra_files=[(ws / "P6" / "reports" / "l4_criteria.json",
                      "l4_criteria.json")]
        if (ws / "P6" / "reports" / "l4_criteria.json").exists() else None)
    gate_ok = diagnose.gate_mode("b_class_autofix", cfg) == "agent"
    verdicts = []
    for cid in red:
        c = _criterion_by_id(ws, mods, cid) or {}
        evidence = {"source": "p6", "subject": cid,
                    "module": cid.split(".")[0] if "." in cid else None,
                    "kind": c.get("kind"), "layer": c.get("layer"),
                    "expr": c.get("expr"),
                    "detail": next((r["detail"] for r in results
                                    if r["id"] == cid),
                                   "deferred 未清偿"),
                    "boot_log": probe_mod._strip_ansi(log)[-4000:],
                    "boot_log_raw": log[-4000:], "ut_out": ut_out[-4000:],
                    "events_tail": events.read_events(ws)[-60:],
                    "criterion": c, "runner": runner,
                    "snapshot": snap.name if snap else None,
                    "_workdir": target_os}
        if cid in uncleared:
            evidence["deferred_uncleared"] = [cid]
        v = triage_mod.run_triage(ws, evidence)
        app = triage_mod.apply_verdict(ws, evidence, v, gate_ok=gate_ok)
        v["applied"] = app["applied"]
        verdicts.append(v)
        print(f"[porter] P6: 分诊 {cid} → {v['circuit']}/{v['action']}")
    return verdicts


def diagnose_defect(ws: Path, did: str, cfg: dict | None = None) -> int:
    """挂载③（§15）：defects 账本驱动的诊断定位（D1 步）。

    defect → 证据包 → triage（规则+agent）→ 按回路处置 →
    unknown/migration → 有界诊断（2 轮×≤10 调用）→ 升级报告 →
    全程 defects history 落账。
    """
    events.bind(ws, "d1")
    d = load_defects(ws)
    e = _find_defect(d, did)
    if not e:
        print(f"[porter] P6: 缺陷不存在: {did}（先 --defect-add）")
        return 2
    proj = _load_json(ws / "project.json") or {}
    target_os = Path(proj.get("target_os") or ws)
    from . import diagnose, triage as triage_mod

    evidence = {"source": "d1", "subject": did,
                "module": None, "detail": e.get("discovered", {})
                .get("evidence", ""),
                "events_tail": events.read_events(ws)[-60:],
                "defect": e, "_workdir": target_os}
    v = triage_mod.run_triage(ws, evidence)
    bump_defect(ws, did, "triaged",
                f"{v['circuit']}/{v['action']} rule={v.get('rule_id')} "
                f"{(v.get('notes') or '')[:160]}")
    print(f"[porter] P6: D1 分诊 {did} → {v['circuit']}/{v['action']}")

    gate_ok = diagnose.gate_mode("b_class_autofix", cfg) == "agent"
    app = triage_mod.apply_verdict(ws, evidence, v, gate_ok=gate_ok)
    human_stop = app.get("human_stop", False)

    escalation_path = None
    if v["circuit"] in ("unknown", "migration") or v.get("action") == \
            "escalate":
        _merged, rep = diagnose.run_diagnosis(
            ws, {**evidence, "_triage_verdicts": [v]}, cfg=cfg)
        escalation_path = rep.get("evidence_files") is not None
        bump_defect(ws, did, "escalated",
                    f"升级报告已生成（escalations/，excluded="
                    f"{len(rep['excluded'])} remaining="
                    f"{len(rep['remaining'])}）")
        human_stop = human_stop or rep.get("human_stop", False)

    if v["circuit"] in ("infra",):
        print("[porter] P6: infra 判定——幂等重跑对应相位（p5/p6 "
              "--execute）即验")
    pack = diagnose.build_context_pack(ws, "d1", did)
    applied_s = "; ".join(app["applied"]) or "无状态变更"
    print(f"[porter] P6: D1 完成 {did}（处置：{applied_s}；"
          f"考古包：{pack}）")
    return 3 if human_stop else 0


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
    tri = report.get("triage") or []
    if tri:
        lines += ["", "## 红项分诊（§15 挂载②）", "",
                  "| 红项 | 回路 | 动作 | 规则 | 处置 |",
                  "|---|---|---|---|---|"]
        for v in tri:
            lines.append(f"| {v.get('subject')} | {v.get('circuit')} "
                         f"| {v.get('action')} | {v.get('rule_id')} "
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

def run_p6(ws: Path, execute_flag: bool = False, l4: bool = False,
           finalize_flag: bool = False, cfg: dict | None = None) -> int:
    if finalize_flag:
        return finalize_l4(ws, cfg)
    if execute_flag:
        return execute(ws, l4=l4)
    return aggregate(ws)
