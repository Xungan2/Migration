"""gates.py — 人工关口账本（人工介入框架重构：两车道 + 一账本）。

设计（S1 地基，S2-S4 在此之上接线）：
- 全部人工介入点收敛为 ws/gates.json 里的关口条目；ws/human_questions.md
  只是账本的人读渲染产物（工具是唯一写者）。
- 生命周期：open → answered → applied → resolved
  （旁路：invalid=答案校验失败回 open 语义；vetoed=检查点批审否决）。
- 答案面：answers.md `## @<gate_id>` 节 + `字段: 值` 行（照 answer_form
  填表）。启动时 process_answered_gates() 消费：校验 → 记账 → 应用。
  答案进账本后从 answers.md 移除 @ 节（账本即档案，不再删答案本体）；
  无 @ 前缀的旧键节（## retry X / ## <api>）不碰，归 legacy 路径。
- 应用者原则：人只表态，工具经 applier 注册表改正本（gap_decisions /
  deferred 副本+criteria 正本同步 / loop_state attempts 清零……）。
- panic 车道统一入口 panic()：登记 panic 关口 + §15 失败快照（best-effort）
  + 渲染 + 返回退出码 3。S2 将各相位 return-3 站点改走此入口。

观测纪律：账本写盘原子（tmp+rename）；history append-only。
"""

from __future__ import annotations

import json
import os
import re
import hashlib
from datetime import datetime
from pathlib import Path
from .. import log as _log

LEDGER_NAME = "gates.json"
VERSION = 1

STATUSES = ("open", "answered", "applied", "resolved", "invalid", "vetoed")
KINDS = ("fact", "decision", "approval", "retry", "memo")
LANES = ("panic", "checkpoint")

# 聚类检测：同 id 关口 re-asked 达到阈值 → 提示"该升检查点/写 policy 规则"
CLUSTER_THRESHOLD = 3

# 答案节定界：## @<gate_id>
_SEC_RE = re.compile(r"^##\s+@(\S+)\s*$")
# 字段行：field: value（字段名限 ASCII 标识符）
_FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*[:：]\s*(.*)$")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- 账本 ----------

class GateLedger:
    def __init__(self, ws: Path):
        self.ws = Path(ws)
        self.path = self.ws / LEDGER_NAME
        self.version = VERSION
        self.gates: list[dict] = []

    def load(self) -> "GateLedger":
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.version = data.get("version", 1)
                self.gates = list(data.get("gates") or [])
            except (OSError, json.JSONDecodeError) as e:
                _log.console_line(f"[porter] gates: 账本损坏，重建（{e}）")
                self.gates = []
        return self

    def save(self) -> None:
        self.ws.mkdir(parents=True, exist_ok=True)
        data = {"version": self.version,
                "gates": self.gates,
                "updated": _now()}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self.path)

    # ---------- 查询 ----------

    def find(self, gate_id: str) -> dict | None:
        for g in self.gates:
            if g.get("id") == gate_id:
                return g
        return None

    def open_blocking(self) -> list[dict]:
        """open/invalid 且 blocking 的关口（exit-3 判定集）。"""
        return [g for g in self.gates
                if g.get("status") in ("open", "invalid")
                and g.get("blocking", True)]

    def pending_review(self) -> list[dict]:
        """已应用待批审的决策债（检查点 digest 素材）。"""
        return [g for g in self.gates
                if g.get("status") == "applied"
                and g.get("answered_by") in ("agent", "policy")]

    # ---------- 登记 ----------

    def add(self, **spec) -> dict:
        """登记关口（幂等：同 id 已存在 → 追加 history 不重复建）。"""
        gate_id = spec.get("id")
        if not gate_id:
            raise ValueError("gate id 必填")
        existing = self.find(gate_id)
        if existing:
            self.note(gate_id, "re-asked", spec.get("question", ""))
            return existing
        gate = {
            "id": gate_id,
            "lane": spec.get("lane", "panic"),
            "kind": spec.get("kind", "decision"),
            "gate_type": spec.get("gate_type", "decision"),
            "checkpoint": spec.get("checkpoint"),
            "phase": spec.get("phase"),
            "module": spec.get("module"),
            "step": spec.get("step"),
            "blocking": spec.get("blocking", True),
            "status": "open",
            "question": spec.get("question", ""),
            "context_files": spec.get("context_files", []),
            "answer_form": spec.get("answer_form", []),
            "agent_draft": None,
            "answer": None,
            "answered_by": None,
            "answered_at": None,
            "applies_to": spec.get("applies_to", {}),
            "artifact_sha": spec.get("artifact_sha"),
            "artifact_path": spec.get("artifact_path"),
            "resolution": None,
            "asked_at": _now(),
            "history": [],
        }
        # 应用器路由字段（decision/deferred 细分等）+ 前向兼容的其余键
        for k, v in spec.items():
            if k not in gate and v is not None:
                gate[k] = v
        self.gates.append(gate)
        self.save()
        return gate

    def note(self, gate_id: str, event: str, detail: str = "") -> None:
        g = self.find(gate_id)
        if g is not None:
            g["history"].append({"time": _now(), "event": event,
                                 "detail": detail[:400]})
            self.save()

    # ---------- 状态迁移 ----------

    def mark(self, gate_id: str, status: str, **extra) -> dict | None:
        g = self.find(gate_id)
        if g is None:
            return None
        g["status"] = status
        g.update(extra)
        self.save()
        return g


# ---------- 答案解析与校验 ----------

def parse_gate_answers(ws: Path) -> dict[str, dict[str, str]]:
    """解析 answers.md 的 `## @<gate_id>` 节 → {id: {field: value}}。

    非 @ 前缀节（legacy 键）不在此函数视野内。多行值：无字段前缀的
    非空行追加到上一字段（换行连接）。
    """
    path = Path(ws) / "answers.md"
    if not path.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    cur_id = None
    last_field = None
    for ln in path.read_text(encoding="utf-8").splitlines():
        m = _SEC_RE.match(ln)
        if m:
            cur_id = m.group(1)
            out.setdefault(cur_id, {})
            last_field = None
            continue
        if cur_id is None:
            continue
        fm = _FIELD_RE.match(ln)
        if fm and not ln.startswith((" ", "\t")):
            last_field = fm.group(1)
            out[cur_id][last_field] = fm.group(2).strip()
        elif ln.strip() and last_field:
            out[cur_id][last_field] = \
                out[cur_id][last_field] + "\n" + ln.rstrip()
    return {k: v for k, v in out.items() if v}


def validate_answer(form: list[dict], answer: dict) -> list[str]:
    """照 answer_form 校验答案。返回错误清单（空 = 合格）。"""
    errs: list[str] = []
    for f in form or []:
        name = f.get("field", "")
        val = (answer.get(name) or "").strip() if isinstance(
            answer.get(name), str) else answer.get(name)
        if f.get("required") and not val:
            errs.append(f"必填字段缺失: {name}")
            continue
        if not val:
            continue                      # 可选且空 → 跳过后续检查
        if f.get("type") == "enum":
            opts = f.get("options") or []
            if val not in opts:
                errs.append(
                    f"字段 {name} 取值非法: {val}"
                    f"（须为 {'/'.join(map(str, opts))} 之一）")
    return errs


def _remove_gate_sections(ws: Path, consumed: set[str]) -> None:
    """从 answers.md 移除已消费的 @ 节（保留其他节原样）。"""
    path = Path(ws) / "answers.md"
    if not path.exists() or not consumed:
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    skip = False
    for ln in lines:
        m = _SEC_RE.match(ln)
        if m:
            skip = m.group(1) in consumed
            continue
        if not skip:
            out.append(ln)
    text = "\n".join(out).rstrip("\n")
    path.write_text((text + "\n") if text else "", encoding="utf-8")


# ---------- applier 注册表 ----------

_APPLIERS: dict[str, object] = {}


def applier(kind: str):
    """注册某 kind 的应用器：fn(ws, gate, answer) -> resolution 说明。"""
    def deco(fn):
        _APPLIERS[kind] = fn
        return fn
    return deco


def _apply(ws: Path, gate: dict, answer: dict) -> str:
    fn = _APPLIERS.get(gate.get("kind", ""))
    if fn is None:
        return "记账完成（该类关口无正本改写）"
    return fn(ws, gate, answer)


@applier("retry")
def _apply_retry(ws: Path, gate: dict, answer: dict) -> str:
    """attempts 清零（loop_state.json；文件不存在则仅记账）。"""
    module = gate.get("module")
    if not module:
        return "记账完成（无模块上下文）"
    sp = Path(ws) / "loop_state.json"
    if not sp.exists():
        return "记账完成（loop_state.json 不存在，无 attempts 可清）"
    state = json.loads(sp.read_text(encoding="utf-8"))
    mod = (state.get("modules") or {}).get(module)
    if not mod:
        return f"记账完成（{module} 不在 loop_state）"
    step = gate.get("step")
    att = mod.setdefault("attempts", {})
    if step:
        att[step] = 0
    else:
        for k in att:
            att[k] = 0
    sp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    scope = f"{module}{'-' + step if step else ''}"
    note = (answer.get("note") or "").strip()
    return f"attempts 清零（{scope}）" + (f"｜诊断笔记: {note[:200]}"
                                          if note else "")


@applier("decision")
def _apply_decision(ws: Path, gate: dict, answer: dict) -> str:
    """决策类：按 gate 的 target 细分正本改写。

    target 取值（gate["target"]）：
      gap          → gap_decisions.json 决策回写 + mapping notes（P3 human gap）
      deferred     → deferred.json 条目与 criteria.json 正本同步改判据（#11）
      resolve      → 仅记账（P1D_plan 结构改动由人工/agent 会话执行后 ack）
    """
    target = gate.get("target")
    if target == "gap":
        return _apply_gap(ws, gate, answer)
    if target == "deferred":
        return _apply_deferred(ws, gate, answer)
    return ("决策已记账（target=" + str(target) +
            "；rationale 留档供批审）")


def _apply_gap(ws: Path, gate: dict, answer: dict) -> str:
    module = gate.get("module")
    api = gate.get("subject") or gate.get("id", "").rsplit(".", 1)[-1]
    strategy = (answer.get("strategy") or "bypass").strip()
    instruction = (answer.get("instruction") or "").strip()
    rationale = (answer.get("rationale") or "").strip()
    dec_path = (Path(ws) / "P3" / (module or "") / "reports" /
                "gap_decisions.json")
    if not dec_path.exists():
        return f"决策记账（{dec_path} 不存在，无正本可写）"
    dec = json.loads(dec_path.read_text(encoding="utf-8"))
    hit = False
    for d in dec.get("decisions", []):
        if d.get("linux_api") == api:
            d["strategy"] = strategy
            d["instruction"] = instruction
            d["answered"] = True
            if rationale:
                d["rationale"] = rationale      # 随草稿收成入 gaps 域
            hit = True
    if not hit:
        dec.setdefault("decisions", []).append(
            {"linux_api": api, "strategy": strategy,
             "instruction": instruction, "answered": True,
             **({"rationale": rationale} if rationale else {})})
    dec_path.write_text(json.dumps(dec, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    # mapping notes 同步（尽力而为；文件缺失不致命）
    try:
        mp = Path(ws) / "P2" / "mapping.json"
        mapping = json.loads(mp.read_text(encoding="utf-8"))
        for e in mapping.get("entries", []):
            if e.get("linux_api") == api:
                e["notes"] = (e.get("notes") or "").rstrip() + \
                    f"｜人工({strategy}): {instruction[:160]}".lstrip("｜")
                e["confidence"] = "high"
        mp.write_text(json.dumps(mapping, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    except (OSError, json.JSONDecodeError):
        pass
    return (f"gap 处置回写: {api} → {strategy}"
            f"（gap_decisions.json + mapping notes）")


def _apply_deferred(ws: Path, gate: dict, answer: dict) -> str:
    """修判据：deferred.json 条目 criterion 副本与 P3 criteria.json 正本
    同步改 expr（根治"改哪个文件"的副本陷阱）。fix-code → 仅记账+提示。"""
    entry_id = gate.get("subject")
    verdict = (answer.get("verdict") or "").strip()
    new_expr = (answer.get("new_expr") or "").strip()
    dp = Path(ws) / "deferred.json"
    if verdict != "fix-criterion" or not entry_id:
        return (f"记账完成（verdict={verdict or '?'}；"
                "fix-code 请修复后 retry 对应模块）")
    if not dp.exists():
        return "记账完成（deferred.json 不存在）"
    d = json.loads(dp.read_text(encoding="utf-8"))
    module = None
    for e in d.get("entries", []):
        if e.get("id") == entry_id:
            module = e.get("module")
            e.setdefault("criterion", {})["expr"] = new_expr
            e["history"].append({"time": _now(), "ok": None,
                                 "detail": f"人工修判据: expr → {new_expr[:120]}"})
    dp.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    synced = False
    if module:
        cp = Path(ws) / "P3" / module / "reports" / "criteria.json"
        try:
            crit = json.loads(cp.read_text(encoding="utf-8"))
            for c in crit.get("criteria", []):
                if c.get("id") == entry_id:
                    c["expr"] = new_expr
                    synced = True
            cp.write_text(json.dumps(crit, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass
    return (f"判据已同步修改（deferred.json 条目"
            f"{' + criteria.json 正本' if synced else ''}）: "
            f"{entry_id} → {new_expr[:120]}")


@applier("approval")
def _apply_approval(ws: Path, gate: dict, answer: dict) -> str:
    """批准：校验 artifact_sha（若登记）——工件指纹不符则拒绝。"""
    sha = gate.get("artifact_sha")
    apath = gate.get("artifact_path")
    if sha and apath:
        p = Path(ws) / apath if not Path(apath).is_absolute() \
            else Path(apath)
        try:
            cur = hashlib.sha256(
                p.read_bytes()).hexdigest()[:16]
        except OSError:
            cur = None
        if cur != sha:
            return (f"批准无效：工件指纹不符（登记 {sha}，"
                    f"当前 {cur or '不可读'}）——草案已变更，请重审")
    verdict = (answer.get("verdict") or "").strip().lower()
    return f"批准已记录（verdict={verdict or 'approve'}）"


@applier("fact")
def _apply_fact(ws: Path, gate: dict, answer: dict) -> str:
    return "事实答案已记账（消费方经账本读取）"


@applier("memo")
def _apply_memo(ws: Path, gate: dict, answer: dict) -> str:
    return "备忘确认已记账"


# ---------- 消费入口 ----------

def process_answered_gates(ws: Path, ledger: GateLedger | None = None
                           ) -> tuple[int, int]:
    """扫描 answers.md 的 @ 节 → 校验 → 记账 → 应用 / 批审否决。

    返回 (applied 数, invalid 数)。invalid 的错误写进 gate history 并在
    渲染面标红；对应 @ 节保留在 answers.md 待人工修正。

    批审语义：对 status=applied（agent/policy 自动应答的决策债）再作答
    verdict: veto|reject → 转 vetoed + 按 applies_to 回滚；verdict: approve
    → 结清（resolved）。resolved/vetoed 的重复作答直接移除。
    """
    ws = Path(ws)
    ledger = ledger or GateLedger(ws).load()
    answers = parse_gate_answers(ws)
    applied = invalid = 0
    consumed: set[str] = set()
    for gate_id, ans in answers.items():
        gate = ledger.find(gate_id)
        if gate is None:
            continue                      # 未知 id：留给渲染面提示，不消费
        verdict = (ans.get("verdict") or "").strip().lower()
        if gate.get("status") in ("resolved", "vetoed"):
            consumed.add(gate_id)         # 已处置过的重复作答 → 直接移除
            continue
        if gate.get("status") == "applied":
            consumed.add(gate_id)
            if verdict in ("veto", "reject", "否决"):
                detail = _rollback_veto(ws, ledger, gate)
                _log.console_line(f"[porter] gates: 决策债被否决 {gate_id}（{detail}）")
                try:
                    from . import events as _ev
                    _ev.append_event("gate-veto", subject=gate_id,
                                     summary=detail,
                                     module=gate.get("module"))
                except Exception:
                    pass
                try:                        # 类 1 钩子：veto 理由 → 候选
                    from ..bootstrap import candidates as _cand
                    _cand.record_from_gate(ws, gate, ans)
                except Exception:
                    pass
            elif verdict in ("approve", "release", "放行", "通过"):
                resolve_applied(ledger, gate_id, "检查点批审：approve")
            continue
        errs = validate_answer(gate.get("answer_form"), ans)
        if errs:
            gate["status"] = "invalid"
            gate["history"].append({"time": _now(), "event": "invalid-answer",
                                    "detail": "; ".join(errs)[:400]})
            invalid += 1
            ledger.save()
            continue
        gate["answer"] = ans
        gate["answered_by"] = "human"
        gate["answered_at"] = _now()
        gate["status"] = "answered"
        resolution = _apply(ws, gate, ans)
        gate["resolution"] = resolution
        gate["status"] = "applied"
        gate["history"].append({"time": _now(), "event": "answered",
                                "detail": f"human; {resolution[:300]}"})
        consumed.add(gate_id)
        applied += 1
        _log.console_line(f"[porter] gates: 关口已应用 {gate_id}（{resolution[:120]}）")
        try:                            # 类 1 钩子：note/rationale → 候选
            from ..bootstrap import candidates as _cand
            _cand.record_from_gate(ws, gate, ans)
        except Exception:
            pass
    ledger.save()
    _remove_gate_sections(ws, consumed)
    if applied:
        try:                            # vcs：人工答案消费留痕（best-effort）
            from ..common import vcs as _vcs
            _vcs.commit_workspace(ws, f"answers: {applied} applied",
                                  phase="gates")
        except Exception:
            pass
    return applied, invalid


def resolve_applied(ledger: GateLedger, gate_id: str,
                    how: str = "批审通过") -> None:
    """applied → resolved（检查点批审 / 单关口直接结清）。"""
    g = ledger.find(gate_id)
    if g and g.get("status") == "applied":
        g["status"] = "resolved"
        g["history"].append({"time": _now(), "event": "resolved",
                             "detail": how[:200]})
        ledger.save()


# ---------- 渲染 ----------

def render_human_questions(ws: Path, ledger: GateLedger | None = None) -> Path:
    """human_questions.md = 账本渲染产物（唯一写者）。"""
    ws = Path(ws)
    ledger = ledger or GateLedger(ws).load()
    lines = ["# 人工关口（由 gates.json 账本渲染；勿手改本文件）", "",
             f"- 渲染时间：{_now()}", ""]
    open_gates = [g for g in ledger.gates
                  if g.get("status") in ("open", "invalid")]
    blocking = [g for g in open_gates if g.get("blocking", True)]
    memos = [g for g in open_gates if not g.get("blocking", True)]
    if not open_gates:
        lines += ["当前无待答关口。"]
    for g in blocking:
        lines += _render_gate(g)
    if memos:
        lines += ["", "---", "", "## 非阻塞备忘（有空确认）", ""]
        for g in memos:
            lines += [f"### @{g['id']}", (g.get("question") or "")[:400], ""]
    path = ws / "human_questions.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _render_gate(g: dict) -> list[str]:
    out = ["---", "", f"## @{g['id']}", ""]
    meta = []
    if g.get("phase"):
        meta.append(f"相位 {g['phase']}")
    if g.get("module"):
        meta.append(f"模块 {g['module']}" + (f"-{g['step']}"
                    if g.get("step") else ""))
    meta.append(f"车道 {g.get('lane', '?')}")
    if g.get("status") == "invalid":
        meta.append("**答案校验失败（已提交过不合格答案，见下方错误）**")
    out.append("- " + "｜".join(meta))
    out += ["", (g.get("question") or ""), ""]
    if g.get("context_files"):
        out.append("证据材料：" + "、".join(
            f"`{c}`" for c in g["context_files"]))
        out.append("")
    form = g.get("answer_form") or []
    if form:
        out.append("作答：在 answers.md 写如下节，照表单填字段——")
        out.append("")
        out.append(f"    ## @{g['id']}")
        for f in form:
            req = "必填" if f.get("required") else "可选"
            if f.get("type") == "enum":
                out.append(f"    {f['field']}: <{'/'.join(map(str, f.get('options') or []))}>（{req}）")
            else:
                hint = f.get("hint") or "自由文本"
                out.append(f"    {f['field']}: <{hint}>（{req}）")
        out.append("")
    else:
        out.append("作答：在 answers.md 写如下节（自由文本）——")
        out.append("")
        out.append(f"    ## @{g['id']}")
        out.append("    <你的回答>")
        out.append("")
    if g.get("status") == "invalid":
        errs = [h.get("detail", "") for h in g.get("history", [])
                if h.get("event") == "invalid-answer"]
        if errs:
            out.append("上次答案的错误：")
            for e in errs[-3:]:
                out.append(f"- {e}")
            out.append("")
    return out


# ---------- panic 统一入口（S2 各 return-3 站点改走此处） ----------

def panic(ws: Path, spec: dict, evidence: dict | None = None) -> int:
    """登记 panic 关口 + §15 快照（best-effort）+ 渲染 + 返回 3。

    spec 即 GateLedger.add 的关键字参数；evidence 传给
    events.take_failure_snapshot（subject/module 摘要 + extra_files）。
    """
    ws = Path(ws)
    spec = {"lane": "panic", **spec}
    try:                                # vcs：停车保存现场（best-effort）
        from ..common import vcs as _vcs
        _vcs.commit_workspace(ws, f"stop: {spec.get('id', '?')}",
                              phase=str(spec.get("phase") or "loop").lower())
    except Exception:
        pass
    ledger = GateLedger(ws).load()
    gate = ledger.add(**spec)
    # 组件四：开给人之前按层链尝试自动应答（rules→agent；仅 decision 类，
    # PORTER_NO_AGENT=1 全跳过；命中 → 决策债 applied，链终止）
    try:
        from . import routing as _routing
        if _routing.maybe_auto_answer(ws, ledger, gate):
            render_human_questions(ws, ledger)
            return 0            # 已自动处置——调用方据此可续跑（run.py
            # 对 rc==3 会复查 open_blocking 为空则继续）
    except Exception:
        pass                    # 路由面永不打断主流程
    snap = None
    try:
        from . import events as _ev
        snap = _ev.take_failure_snapshot(
            ws, str(spec.get("phase", "loop")).lower(),
            str(spec.get("module") or spec.get("id", ""))[:80],
            (spec.get("question") or "")[:200],
            extra_files=[(Path(ws) / c, Path(c).name)
                         for c in (spec.get("context_files") or [])
                         if (ws / c).is_file()])
        if snap:
            ledger.note(gate["id"], "snapshot", str(snap))
    except Exception:
        pass                              # 观测面永不打断主流程
    # 聚类检测：同型 panic 反复发生 = 该异常不是小概率，该机制化处理
    re_asked = sum(1 for h in gate.get("history", [])
                   if h.get("event") == "re-asked")
    if re_asked >= CLUSTER_THRESHOLD:
        hint = (f"关口 {gate['id']} 已第 {re_asked + 1} 次触发——"
                "建议升为检查点（checkpoint 车道）或为该类问题写 policy "
                "规则，而非反复人工应答")
        _log.console_line(f"[porter] gates: ⚠️ 聚类 {hint}")
        try:
            from . import events as _ev
            _ev.append_event("gate-cluster", subject=gate["id"],
                             summary=hint,
                             module=gate.get("module"))
        except Exception:
            pass
    render_human_questions(ws, ledger)
    blocking = len(ledger.open_blocking())
    _log.console_line(f"[porter] gates: panic {gate['id']}（待答关口 {blocking} 个）"
          f"→ 详见 {ws / 'human_questions.md'}")
    return 3


def summary_line(ws: Path) -> str:
    """退出时的一行关口摘要（main 分发层打印用）。"""
    ledger = GateLedger(ws).load()
    n_open = len(ledger.open_blocking())
    n_debt = len(ledger.pending_review())
    return (f"open 阻塞关口 {n_open}｜决策债（待批审）{n_debt}"
            if (n_open or n_debt) else "无待答关口/决策债")


# ---------- 检查点车道（S3） ----------

_REPO_CONFIG = Path(__file__).resolve().parent.parent / "config.json"


def load_config() -> dict:
    """读仓级 porter/config.json（坏文件/缺文件 → {}，缺键语义由调用方定）。"""
    try:
        return json.loads(_REPO_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def self_diagnosis_enabled() -> bool:
    """错误处理模块总开关兼熔断（solve 循环的判断机器；events 观测不受控）。

    config self_diagnosis.enabled，缺省 true（§15 重设计 2026-09-03 后
    直接生效；false = 熔断回退旧人工路径：p5 失败 rc 1 → attempts→panic、
    --defect-diagnose rc 2）。PORTER_SELF_DIAGNOSIS=1 强制开（测试惯例，
    同 PORTER_NO_AGENT）。
    """
    if os.environ.get("PORTER_SELF_DIAGNOSIS"):
        return True
    return bool((load_config().get("self_diagnosis") or {})
                .get("enabled", True))


def _cp_config() -> dict:
    return load_config().get("checkpoints") or {}


def checkpoint_enabled(cp: str) -> bool:
    """CP 开关：CP2 默认关（e2e 实证无它也跑通），其余默认开。"""
    if cp == "CP2":
        return bool(_cp_config().get("CP2_enabled", False))
    return True


def first_module_review_enabled() -> bool:
    return bool(_cp_config().get("first_module_review", True))


def checkpoint_digest(ws: Path, cp: str,
                      ledger: GateLedger | None = None) -> Path:
    """检查点 digest：决策债批审材料（按 answered_by/模式分组渲染）。"""
    ws = Path(ws)
    ledger = ledger or GateLedger(ws).load()
    debt = ledger.pending_review()
    by_kind: dict[str, list[dict]] = {}
    for g in debt:
        by_kind.setdefault(g.get("kind", "?"), []).append(g)
    lines = [f"# {cp} 检查点 digest", "",
             f"- 时间：{_now()}",
             f"- 决策债（agent/policy 自动应答，待批审）：{len(debt)} 条", ""]
    if not debt:
        lines += ["无待批审决策。"]
    for kind, items in sorted(by_kind.items()):
        lines += [f"## {kind}（{len(items)}）", "",
                  "| 关口 | 应答者 | 决策摘要 | 否决方式 |", "|---|---|---|---|"]
        for g in items:
            ans = json.dumps(g.get("answer") or {}, ensure_ascii=False)[:120]
            lines.append(f"| `{g['id']}` | {g.get('answered_by')} "
                         f"| {ans} | answers.md `## @{g['id']}` "
                         f"verdict: veto |")
        lines.append("")
    vetoed = [g for g in ledger.gates if g.get("status") == "vetoed"]
    if vetoed:
        lines += ["## 已否决（已回滚）", ""]
        for g in vetoed[-10:]:
            lines.append(f"- `{g['id']}`：{g.get('resolution', '')[:160]}")
        lines.append("")
    out = ws / "checkpoints" / f"{cp}_digest.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def checkpoint_run(ws: Path, cp: str, register: list[dict] | None = None,
                   blocking: bool = True) -> int:
    """检查点执行：登记本站关口 → digest → 有 open 阻塞项则 exit 3。

    register：本站要登记的关口 spec 列表（lane/checkpoint 自动设为
    本检查点）。返回 0=放行 / 3=停车。
    """
    ws = Path(ws)
    ledger = GateLedger(ws).load()
    for spec in register or []:
        spec = {"lane": "checkpoint", "checkpoint": cp, **spec}
        gate = ledger.add(**spec)
        if gate.get("status") in ("open", "invalid"):
            pass                          # 已 open：add 幂等已 note re-asked
    digest = checkpoint_digest(ws, cp, ledger)
    render_human_questions(ws, ledger)
    blocking_open = [g for g in ledger.open_blocking()
                     if g.get("checkpoint") == cp]
    if blocking_open and blocking:
        _log.console_line(f"[porter] gates: {cp} 停车——{len(blocking_open)} 个待答关口"
              f"；批审材料 {digest}；待办见 human_questions.md")
        return 3
    _log.console_line(f"[porter] gates: {cp} 放行（digest：{digest}）")
    return 0


def strategy_checkpoint(ws: Path) -> int:
    """CP1 拆分审：strategy.md 人工审阅关口。

    config review_gates.strategy_review：缺省 human（范围/产品决策——
    #5 必人四点之一）；agent 模式直通并记债（CP 批审兜底）。
    返回 0=放行 / 3=停车。绑定 strategy.md 指纹：人工编辑后指纹刷新，
    旧批准自动失效（重审）。
    """
    import hashlib
    ws = Path(ws)
    st = ws / "P1" / "strategy.md"
    if not st.exists():
        return 0        # 无策略文件 → 走既有缺文件路径（divide rc 2）
    mode = (load_config().get("review_gates") or {}).get(
        "strategy_review", "human")
    # 范围声明层：scope.json 在场 → 指纹= strategy+scope 联合（编辑任一
    # 即失效重批）；不在场 → 原公式（存量工作区零影响）
    st_bytes = st.read_bytes()
    sc_path = ws / "P1" / "scope.json"
    if sc_path.exists():
        sha = hashlib.sha256(
            st_bytes + b"\n--scope--\n" + sc_path.read_bytes()
        ).hexdigest()[:16]
        scope_ctx = ["P1/scope.json"]
        scope_note = ("；P1/scope.json 为迁移范围闭包白名单（文件并集生效，"
                      "分组仅参考）——重点审 strategy「迁移范围」节与清单，"
                      "可直接编辑 scope.json，编辑即需重批")
    else:
        sha = hashlib.sha256(st_bytes).hexdigest()[:16]
        scope_ctx = []
        scope_note = ""
    ledger = GateLedger(ws).load()
    gate = ledger.find("cp1.strategy")
    if mode != "human":
        if gate is None:
            g = ledger.add(
                id="cp1.strategy", lane="checkpoint", kind="approval",
                gate_type="decision", phase="P1", checkpoint="CP1",
                blocking=False,
                question="拆分策略审阅（strategy_review=agent 直通）",
                context_files=["P1/strategy.md"] + scope_ctx,
                answer_form=[{"field": "verdict", "type": "enum",
                              "options": ["approve", "veto"],
                              "required": True}],
                artifact_path="P1/strategy.md", artifact_sha=sha)
            g.update({"answer": {"verdict": "approve"},
                      "answered_by": "agent", "answered_at": _now(),
                      "resolution": "strategy_review=agent 直通（批审兜底）",
                      "status": "applied"})
            ledger.save()
        return 0
    if gate is None:
        gate = ledger.add(
            id="cp1.strategy", lane="checkpoint", kind="approval",
            gate_type="decision", phase="P1", checkpoint="CP1",
            question=("拆分策略审阅（CP1）：模块划分与范围取舍是人的意图"
                      "（agent 无从知道 MVP 边界）。请读 P1/strategy.md，"
                      "可编辑后批准——批准绑定文件指纹，改文件即需重批。"
                      + scope_note),
            context_files=["P1/strategy.md"] + scope_ctx,
            answer_form=[{"field": "verdict", "type": "enum",
                          "options": ["approve", "reject"],
                          "required": True}],
            artifact_path="P1/strategy.md", artifact_sha=sha)
    else:
        if gate.get("status") in ("applied", "resolved"):
            ans_v = (gate.get("answer") or {}).get("verdict", "").lower()
            if ans_v == "approve" and gate.get("artifact_sha") == sha:
                resolve_applied(ledger, "cp1.strategy", "CP1 批准结清")
                return 0
            # 指纹已变或曾 reject → 重新审
        if gate.get("status") in ("open", "invalid"):
            gate["artifact_sha"] = sha        # 人工可能已编辑——刷新指纹
            ledger.save()
    checkpoint_digest(ws, "CP1", ledger)
    render_human_questions(ws, ledger)
    _log.console_line("[porter] gates: CP1 拆分审停车——读/改 P1/strategy.md 后在 "
          "answers.md `## @cp1.strategy` verdict: approve 放行")
    return 3


def _rollback_veto(ws: Path, ledger: GateLedger, gate: dict) -> str:
    """批审否决 → 回滚：标 vetoed + applies_to 范围内重置/重开。

    v1 语义（透明化）：attempts 清零；模块相位按关口类型回拨到重做入口
    （gap/映射类 → p4 重迁移、deferred → p5 重验收）；已 done 切片仍被
    migration.json 跳过——需要强制重迁的片须人工清 migration.json 对应
    条目（digest 中注明）。
    """
    gate["status"] = "vetoed"
    gate["history"].append({"time": _now(), "event": "vetoed",
                            "detail": (gate.get("answer") or {}).get(
                                "rationale", "")[:200] or "批审否决"})
    modules = (gate.get("applies_to") or {}).get("modules") or []
    did: list[str] = []
    sp = Path(ws) / "loop_state.json"
    if sp.exists() and modules:
        state = json.loads(sp.read_text(encoding="utf-8"))
        target = gate.get("target")
        reentry = "p4" if target in ("gap", "mapping") else \
            ("p5" if target == "deferred" else None)
        for m in modules:
            mod = (state.get("modules") or {}).get(m)
            if not mod:
                continue
            att = mod.setdefault("attempts", {})
            for k in att:
                att[k] = 0
            if reentry and mod.get("phase") in ("p4", "p5", "done"):
                mod["phase"] = reentry
            did.append(m)
        sp.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    ledger.save()
    parts = [f"已否决 {gate['id']}"]
    if did:
        parts.append(f"attempts 清零 + 相位回拨：{', '.join(did)}"
                     "（已 done 切片仍跳过；强制重迁须清 migration.json）")
    policy_hint = ("；否决理由可写成 policy.md 规则避免同类再问"
                   if (gate.get("answer") or {}).get("rationale") else "")
    return "".join(parts) + policy_hint
