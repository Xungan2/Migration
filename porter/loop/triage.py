"""triage.py — 分诊引擎（plan §15 子系统 C：规则先行 → 不识别上小 agent）。

五回路（§15.3，处置各不相同）：
    infra       基础设施（docker 锁/静默控制台/半成品 ISO）→ 幂等重跑，
                **不计 attempts**（mount 侧执行重跑）
    criteria    判据/测试/文档错 → 自动修正判定数据（强制 Linux C 或
                QEMU 源码 file:line 证据，标 auto-fixed；仅工作区 JSON，
                需改目标树的 → 检出后升级）
    migration   迁移 bug → verdict 带证据回炉（mount 走既有 attempts 机器）
    attribution 归属错 → deferred 改挂真实消费者
    platform    平台缺口 → 泊车 + platform_patches 登记（P7 上游素材）

判不了 → 有界 agent 重试 2 次 → 仍判不了 → circuit=unknown
→ 泊车绕过（mount 继续其余）+ 轮末集中升级（diagnose.generate_…）。

输入 evidence（dict，挂载点组装）：
    source      "p5"|"p6"|"d1"（挂载点）
    subject     判据 id / 红项 / defect id
    module / kind / layer / expr / detail   判据上下文（可 None）
    boot_log / ut_out / build_out           日志摘录（判定输入，已去 ANSI；
                                            boot_log_raw 保留 ANSI 供 R4）
    events_tail   events.jsonl 相关尾部（list[dict]）
    criterion / defect / deferred           原始条目（可 None）
    runner        runner dict（SIG-02 修复检查用，可 None）
    snapshot      快照目录名（可 None）

verdict（返回/落 events）：
    {circuit, rule_id, confidence, evidence[{file,line,quote}], action,
     notes, signature_candidates[], fix_target, fix_value, rehang_to}
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from ..common import agent
from . import events

CIRCUITS = ("infra", "criteria", "migration", "attribution", "platform",
            "unknown")
MAX_AGENT_TRIES = 2

# ---------- 签名规则库（deterministic；六案例淬出，与 skills/triage.md、
# knowledge/failures.md 的 SIG 编号对齐） ----------

_INFRA_PATTERNS = (
    "resource temporarily unavailable", "database is locked",
    "device or resource busy", "The container name",
    "Cannot connect to the Docker daemon", "no space left on device",
)
_UEFI_MARKERS = ("BdsDxe", "UEFI QEMU")
_KERNEL_MARKERS = ("Successfully booted", "Kernel", "kernel")
_PLATFORM_MARKERS = (("irq_count", "0"), ("OSTD", "level"),
                     ("OSTD", "电平"), ("ioapic", "level"))
_PLATFORM_WORDS = ("平台缺口", "禁改", "platform_patches")


def _hay(evidence: dict) -> str:
    parts = [evidence.get(k) or "" for k in
             ("detail", "boot_log", "ut_out", "build_out")]
    for e in evidence.get("events_tail") or []:
        parts.append(" ".join(str(e.get(k) or "") for k in
                              ("intent", "cmd", "summary")))
    d = evidence.get("defect")
    if d:
        parts.append(json.dumps(d, ensure_ascii=False))
    c = evidence.get("criterion")
    if c:
        parts.append(json.dumps(c, ensure_ascii=False))
    return "\n".join(parts)


def _ut_rc0_silent(evidence: dict) -> bool:
    """ktest 静默形态：ut 命令 rc==0 但成功特征缺失（输出型判据假 FAIL）。"""
    ut_out = evidence.get("ut_out") or ""
    sp = ((evidence.get("runner") or {}).get("unit_test") or {}) \
        .get("success_pattern", "test result: ok")
    if sp and sp in ut_out:
        return False                      # 特征在——不是静默
    for e in evidence.get("events_tail") or []:
        if e.get("kind") != "cmd_end" or e.get("rc") != 0:
            continue
        blob = str(e.get("cmd") or "") + str(e.get("summary") or "")
        if "osdk test" in blob or "_acc_ut" in blob or "exec_ut" in blob:
            return True
    return False


def match_rules(evidence: dict) -> dict | None:
    """规则先行：命中返回 verdict dict，未命中返回 None。"""
    hay = _hay(evidence)
    kind = evidence.get("kind")
    layer = evidence.get("layer")
    boot_log = evidence.get("boot_log") or ""

    # R1 SIG-01 docker/资源锁 → infra
    for pat in _INFRA_PATTERNS:
        if pat in hay:
            return {"circuit": "infra", "rule_id": "SIG-01",
                    "confidence": 0.9, "action": "rerun",
                    "evidence": [{"file": "<cmd-output>", "line": 0,
                                  "quote": pat}],
                    "notes": f"基础设施签名命中：{pat}（幂等重跑不计 "
                             "attempts）", "signature_candidates": []}

    # R2 SIG-02 ktest 静默（rc==0 ∧ 特征缺失）→ infra + 定向修复建议
    if (kind == "unit_test" or evidence.get("subject") == "P6.ktest") \
            and _ut_rc0_silent(evidence):
        fix = "ut-console-args" if not _ut_cmd_has_console(evidence) \
            else None
        return {"circuit": "infra", "rule_id": "SIG-02",
                "confidence": 0.85, "action": "rerun",
                "suggested_fix": fix,
                "evidence": [{"file": "knowledge/pitfalls/"
                                      "ktest-console-args.md", "line": 0,
                              "quote": "rc==0 但 success_pattern 缺失——"
                                       "console 缓存参数被清空"}],
                "notes": "ktest 静默形态（§14 定谳）" +
                         ("；runner ut 命令缺显式 --kcmd-args → "
                          "自动补 console=ttyS0 earlycon" if fix else ""),
                "signature_candidates": []}

    # R3 SIG-02b 半成品 ISO（UEFI 起而内核无输出 + 此前有被杀 boot）
    if (kind == "boot" or layer == "L2") \
            and any(m in boot_log for m in _UEFI_MARKERS) \
            and not any(m in boot_log for m in _KERNEL_MARKERS):
        killed = any(e.get("rc") in (-1, None) and
                     "TIMEOUT" in str(e.get("summary") or "")
                     for e in evidence.get("events_tail") or [])
        if killed:
            return {"circuit": "infra", "rule_id": "SIG-02b",
                    "confidence": 0.8, "action": "rerun",
                    "suggested_fix": "full-make-kernel",
                    "evidence": [{"file": "knowledge/pitfalls/"
                                          "killed-make-halfbuilt-iso.md",
                                  "line": 0,
                                  "quote": "UEFI 起而内核无输出"}],
                    "notes": "杀 make 留半成品 ISO（§16 沉淀③）——完整 "
                             "make kernel 一次即愈",
                    "signature_candidates": []}

    # R4 SIG-03 ANSI 边界正则失配 → criteria（确定性修正 = 去 \b）
    expr = evidence.get("expr") or ""
    raw = evidence.get("boot_log_raw") or evidence.get("boot_log") or ""
    if kind in ("log_pattern", "counter") and r"\b" in expr \
            and "\x1b[" in raw:
        return {"circuit": "criteria", "rule_id": "SIG-03",
                "confidence": 0.7, "action": "autofix",
                "fix_target": "criteria", "fix_value": expr.replace(
                    r"\b", ""),
                "evidence": [{"file": "knowledge/pitfalls/"
                                      "ktest-console-args.md", "line": 0,
                              "quote": r"正则跨 ANSI 色码边界失配"
                                       r"（\beth0\b 型）——优先字面量"}],
                "notes": "判据正则含 \\b 且日志含 ANSI 色码——跨边界失配",
                "signature_candidates": []}

    # R5 编译失败 → migration（编译错即迁移 bug，build 输出为证据）
    detail = evidence.get("detail") or ""
    if kind == "compile" and ("pattern=MISS" in detail
                              or re.search(r"\brc=[1-9]", detail)):
        return {"circuit": "migration", "rule_id": "COMPILE-FAIL",
                "confidence": 0.9, "action": "rework",
                "evidence": [{"file": "<build-output>", "line": 0,
                              "quote": (evidence.get("build_out") or
                                        "")[-160:]}],
                "notes": "编译失败——迁移代码问题，attempts 带证据回炉",
                "signature_candidates": []}

    # R6 SIG-05 RX 复合型（RX 计数为 0 而 TX 成功）→ migration
    if re.search(r"rx_(bytes|packets)=0\b", hay) \
            and re.search(r"tx_bytes=[1-9]", hay):
        return {"circuit": "migration", "rule_id": "SIG-05",
                "confidence": 0.75, "action": "rework",
                "evidence": [{"file": "<boot-log>", "line": 0,
                              "quote": "rx 计数恒 0 而 tx_bytes 非零——"
                                       "复合型（defects.json RX-PATH 链）"}],
                "notes": "RX 通路复合型签名——逐条分解独立证据链回炉",
                "signature_candidates": []}

    # R7 deferred 无法清偿（消费者全 done 仍 FAIL）→ attribution
    if evidence.get("deferred_uncleared"):
        return {"circuit": "attribution", "rule_id": "DEFER-UNCLEARED",
                "confidence": 0.6, "action": "rehang",
                "evidence": [{"file": "deferred.json", "line": 0,
                              "quote": ", ".join(
                                  evidence["deferred_uncleared"])}],
                "notes": "deferred 清偿失败——归属可疑，agent 核实真实"
                         "消费者后改挂",
                "signature_candidates": []}

    # R8 SIG-06 平台缺口证据 → platform（泊车 + 登记）
    if any(a in hay and b in hay for a, b in _PLATFORM_MARKERS) \
            or any(w in hay for w in _PLATFORM_WORDS):
        return {"circuit": "platform", "rule_id": "SIG-06",
                "confidence": 0.85, "action": "park",
                "evidence": [{"file": "<evidence>", "line": 0,
                              "quote": "设备侧已证 vs 消费侧恒零——平台"
                                       "缺口（INTX-DELIVERY 形态）"}],
                "notes": "平台缺口签名——泊车 + platform_patches 登记"
                         "（P7 上游素材）",
                "signature_candidates": []}

    # R9 SIG-04 计划/文档过期型假缺陷（d1：defect 由计划/遗留清单登记）
    d = evidence.get("defect")
    if d and any(w in json.dumps(
            {k: d.get(k) for k in ("title", "discovered", "root_cause")},
            ensure_ascii=False) for w in ("计划", "遗留", "§14", "§16")):
        return {"circuit": "criteria", "rule_id": "SIG-04",
                "confidence": 0.6, "action": "autofix",
                "fix_target": "close_stale",
                "needs_agent_evidence": True,    # 闭账须 agent 补代码
                "evidence": [],                  # 实测调用点 file:line
                "notes": "计划/文档过期型假缺陷候选——对照代码实测核计划"
                         "（RESET-HW-STALE 模式），agent 须给调用点 "
                         "file:line 证据",
                "signature_candidates": []}

    return None


def _ut_cmd_has_console(evidence: dict) -> bool:
    ut = ((evidence.get("runner") or {}).get("unit_test") or {})
    cmd = ut.get("cmd") or ""
    return "--kcmd-args" in cmd and "console=" in cmd


# ---------- agent 兜底（规则未命中 / 需证据补强） ----------

def _agent_prompt(evidence: dict) -> str:
    skill = agent.load_skill("triage")
    slim = {k: v for k, v in evidence.items()
            if k in ("source", "subject", "module", "kind", "layer", "expr",
                     "detail", "boot_log", "ut_out", "build_out",
                     "criterion", "defect", "deferred", "snapshot",
                     "deferred_uncleared")}
    slim = {k: (v if not isinstance(v, str) or len(v) <= 2000
                else v[:2000] + "…") for k, v in slim.items()}
    return (f"{skill}\n\n---\n\n## 失败证据包（JSON）\n```json\n"
            f"{json.dumps(slim, ensure_ascii=False, indent=1)}\n```\n\n"
            f"## 任务\n判定回路并输出紧凑 JSON verdict（照 skill 输出格式）。"
            f"若判 criteria 回路，必须附 Linux C 或 QEMU 源码 file:line "
            f"证据并给 fix_target/fix_value；若判 attribution 给 rehang_to。")


def _agent_verdict(evidence: dict, log_dir: Path) -> dict | None:
    prompt = _agent_prompt(evidence)
    for attempt in range(1, MAX_AGENT_TRIES + 1):
        rc, out = agent.run_agent(
            prompt, workdir=evidence.get("_workdir") or Path.cwd(),
            log_stem=str(log_dir / f"triage_agent_R{attempt}"),
            timeout_sec=900)
        parsed = agent.extract_json(out) if rc == 0 else None
        if not parsed and out:
            # extract_json 找 "moves" 锚——triage 输出无该键，宽容兜底
            try:
                blocks = re.findall(r"```json\s*(.*?)```", out, re.DOTALL) \
                    or re.findall(r"```\s*(\{.*?\})\s*```", out, re.DOTALL)
                for b in blocks:
                    obj = json.loads(b.strip())
                    if isinstance(obj, dict) and "circuit" in obj:
                        parsed = obj
                        break
            except (json.JSONDecodeError, re.error):
                parsed = None
        if parsed and parsed.get("circuit") in CIRCUITS:
            return parsed
    return None


# ---------- 入口 ----------

def run_triage(ws: Path, evidence: dict, use_agent: bool = True) -> dict:
    """规则先行 → agent 兜底 → unknown。落 events，返回 verdict dict。

    PORTER_NO_AGENT=1 时禁用 agent 兜底（单测/夜间守护）。
    """
    ws = Path(ws)
    verdict = match_rules(evidence)
    agent_allowed = use_agent and not os.environ.get("PORTER_NO_AGENT")
    if verdict is None and agent_allowed:
        log_dir = ws / "triage" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        verdict = _agent_verdict(evidence, log_dir) or None
    elif verdict is not None and verdict.get("needs_agent_evidence") \
            and agent_allowed:
        # 规则候选判定 + agent 证据补强（SIG-04：闭账须代码实测 file:line）
        log_dir = ws / "triage" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        sup = _agent_verdict(evidence, log_dir)
        if sup and sup.get("circuit") == verdict["circuit"] \
                and sup.get("evidence"):
            verdict["evidence"] = sup["evidence"]
            if sup.get("fix_target"):
                verdict["fix_target"] = sup["fix_target"]
            verdict["confidence"] = max(verdict["confidence"],
                                        sup.get("confidence", 0))
            verdict["notes"] += "；agent 已补代码实测证据"
        else:
            verdict["notes"] += "；agent 未能补证——按候选处置（人工复核）"
    if verdict is None:
        verdict = {"circuit": "unknown", "rule_id": None,
                   "confidence": 0.0, "action": "escalate",
                   "evidence": [], "notes": "规则与 agent 均未能判定——"
                   "泊车绕过 + 轮末集中升级",
                   "signature_candidates": []}
    verdict.setdefault("action", "escalate")
    verdict.setdefault("evidence", [])
    verdict.setdefault("signature_candidates", [])
    verdict["time"] = datetime.now().isoformat(timespec="seconds")
    verdict["source"] = evidence.get("source")
    verdict["subject"] = evidence.get("subject")
    events.append_event("triage", subject=evidence.get("subject"),
                        intent=evidence.get("source"),
                        summary=f"{verdict['circuit']}/{verdict['action']}"
                                f" rule={verdict.get('rule_id')} "
                                f"{verdict.get('notes') or ''}"[:380],
                        ws=ws, mount=evidence.get("source"))
    return verdict


# ---------- 处置执行（apply_verdict：按回路改状态，全部工作区 JSON） ----------

def _load(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _save(p: Path, data) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                 encoding="utf-8")


def _evidence_str(verdict: dict) -> str:
    evs = [f"{e.get('file')}:{e.get('line')} {e.get('quote', '')[:80]}"
           for e in verdict.get("evidence") or []]
    return "; ".join(evs) or (verdict.get("notes") or "")


def apply_verdict(ws: Path, evidence: dict, verdict: dict,
                  gate_ok: bool = True) -> dict:
    """执行处置。返回 {applied: [...], human_stop: bool}。

    gate_ok=False（review_gates.b_class_autofix=human）时 criteria 自动
    修正不执行，改写 human_questions.md 停车等 answers.md。
    """
    ws = Path(ws)
    applied: list[str] = []
    circuit = verdict.get("circuit")

    if circuit == "infra":
        fix = verdict.get("suggested_fix")
        if fix == "ut-console-args":
            rp = ws / "runner.json"
            runner = _load(rp, None)
            if runner is None:
                runner = evidence.get("runner") or {}
            ut = runner.get("unit_test") or {}
            cmd = ut.get("cmd") or ""
            if cmd and "--kcmd-args" not in cmd:
                ut["cmd"] = (cmd.rstrip() +
                             ' --kcmd-args="console=ttyS0"'
                             ' --kcmd-args="earlycon"')
                ut["notes"] = (ut.get("notes") or "") + \
                    f"｜auto-fixed(SIG-02 {verdict['time']}): 显式补 " \
                    "console=ttyS0/earlycon（§14 修复，防缓存清空）"
                runner["unit_test"] = ut
                _save(rp, runner)
                applied.append("runner.unit_test.cmd += --kcmd-args "
                               "(SIG-02 auto-fixed)")
        elif fix == "full-make-kernel":
            applied.append("建议：完整 make kernel 一次（SIG-02b，"
                           "由 mount 重跑执行）")

    elif circuit == "criteria" and verdict.get("action") == "autofix":
        if not gate_ok:
            _write_autofix_question(ws, evidence, verdict)
            return {"applied": [], "human_stop": True}
        ok, what = _apply_criteria_fix(ws, evidence, verdict)
        if ok:
            applied.append(what)
        else:
            applied.append(f"criteria 修正未执行：{what}")

    elif circuit == "attribution" and verdict.get("rehang_to"):
        dp = ws / "deferred.json"
        d = _load(dp, {"entries": []})
        for e in d.get("entries", []):
            if e.get("id") == evidence.get("subject"):
                e["deferred_by"] = list(verdict["rehang_to"])
                e["history"] = e.get("history") or []
                e["history"].append({
                    "time": verdict["time"], "ok": None,
                    "detail": f"triage 改挂 {verdict['rehang_to']}"
                              f"（{_evidence_str(verdict)[:160]}）"})
                _save(dp, d)
                applied.append(f"deferred {evidence.get('subject')} "
                               f"改挂 {verdict['rehang_to']}")
                break

    elif circuit == "platform" and verdict.get("action") == "park":
        applied.append(_register_platform_patch(
            ws, evidence, verdict))

    events.append_event("triage_apply", subject=evidence.get("subject"),
                        intent=evidence.get("source"),
                        summary="; ".join(applied) or "无状态变更"
                        f"（{circuit}/{verdict.get('action')}）",
                        ws=ws, mount=evidence.get("source"))
    return {"applied": applied, "human_stop": False}


def _apply_criteria_fix(ws: Path, evidence: dict,
                        verdict: dict) -> tuple[bool, str]:
    """criteria 自动修正（强制证据入档；仅工作区 JSON）。"""
    ev_str = _evidence_str(verdict)
    target = verdict.get("fix_target")
    if target == "criteria":
        module = evidence.get("module")
        if not module:
            return False, "缺 module——无法定位 criteria.json"
        cp = ws / "P3" / module / "reports" / "criteria.json"
        doc = _load(cp, None)
        if doc is None:
            return False, f"缺 {cp}"
        for c in doc.get("criteria", []):
            if c.get("id") == evidence.get("subject"):
                old = c.get("expr")
                c["expr"] = verdict.get("fix_value") or old
                c["auto_fixed"] = {"time": verdict["time"], "was": old,
                                   "evidence": ev_str,
                                   "rule": verdict.get("rule_id")}
                _save(cp, doc)
                return True, (f"criteria {c['id']} expr 修正（auto-fixed，"
                              f"证据 {ev_str[:120]}）")
        return False, "criteria 条目未找到"
    if target == "l4":
        lp = ws / "P6" / "reports" / "l4_criteria.json"
        doc = _load(lp, None)
        if doc is None:
            return False, f"缺 {lp}"
        for c in doc.get("criteria", []):
            if c.get("id") == evidence.get("subject"):
                old = c.get("expr")
                c["expr"] = verdict.get("fix_value") or old
                c["auto_fixed"] = {"time": verdict["time"], "was": old,
                                   "evidence": ev_str}
                _save(lp, doc)
                return True, f"l4 {c['id']} expr 修正（auto-fixed）"
        return False, "l4 条目未找到"
    if target == "close_stale":
        from . import p6 as p6_mod
        did = evidence.get("subject")
        try:
            p6_mod.close_defect(
                ws, did,
                root_cause=f"计划/文档过期（假缺陷）：{ev_str[:300]}",
                fix="无代码修复（闭账 stale；auto-fixed）",
                regression_evidence=verdict.get("regression_evidence")
                or "代码实测调用点已在（见 root_cause file:line）")
            return True, f"defect {did} 闭账 stale（auto-fixed）"
        except ValueError as ex:
            return False, str(ex)
    if target == "needs-target-tree":
        return False, ("修正需改目标树（超出边界）——升级人工"
                       f"（证据：{ev_str[:160]}）")
    return False, f"未知 fix_target: {target}"


def _register_platform_patch(ws: Path, evidence: dict,
                             verdict: dict) -> str:
    pp = ws / "platform_patches.json"
    doc = _load(pp, {"patches": []})
    gap = evidence.get("subject") or "UNKNOWN-GAP"
    if not any(p.get("gap") == gap for p in doc.get("patches", [])):
        doc["patches"].append({
            "gap": gap, "module": evidence.get("module"),
            "status": "proposed", "strategy": "platform-gap",
            "instruction": f"triage 泊车登记（{verdict['time']}）："
                           f"{verdict.get('notes') or ''}",
            "evidence": _evidence_str(verdict)[:500],
            "registered": verdict["time"]})
        _save(pp, doc)
    # defects 账本同步 add+park（幂等）
    from . import p6 as p6_mod
    try:
        p6_mod.add_defect(ws, gap,
                          f"平台缺口（triage 自动登记）：{gap}",
                          _evidence_str(verdict)[:300])
    except ValueError:
        pass
    try:
        p6_mod.park_defect(ws, gap, "平台缺口泊车（triage SIG-06）——"
                                    "P7 上游补丁素材")
    except ValueError:
        pass
    return f"platform_patches 登记 + defect 泊车：{gap}"


def _write_autofix_question(ws: Path, evidence: dict,
                            verdict: dict) -> None:
    q = ws / "human_questions.md"
    block = (f"\n\n---\n\n## B 类自动修正审核门（b_class_autofix=human）\n"
             f"- 时间：{verdict['time']}；对象："
             f"{evidence.get('subject')}\n"
             f"- 判定：{verdict.get('circuit')} / "
             f"{verdict.get('rule_id')}；证据：{_evidence_str(verdict)}\n"
             f"- 拟修正：fix_target={verdict.get('fix_target')} "
             f"fix_value={verdict.get('fix_value')}\n"
             f"- 放行：answers.md 写 `b_class_autofix: approve` 后重跑；"
             f"否决：写 `b_class_autofix: reject`。\n")
    with q.open("a", encoding="utf-8") as f:
        f.write(block)
