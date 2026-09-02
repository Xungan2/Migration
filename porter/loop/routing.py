"""routing.py — 三级分流路由（组件四：谁第一个应答）。

设计（与用户对齐的定案）：
- 配置写**概念层**（rules / agent / human），不写实现名；实现按关口
  gate_type 分派：
    rules 层：decision → policy.md（工作区自然语言常备规则，agent 解释、
              命中留痕）；failure → 已由相位内 triage 消费（层已消费语义）
    agent 层：decision → gate-answer skill（照表单作答+置信度）；
              failure → diagnose（有界诊断，挂载点已消费则跳过）
- 两级配置：仓级 porter/config.json 的 routing 节 + 工作区 routing.json
  覆写（键特异性：p3.gap.register_fill > p3.gap > default）。
- 硬路由（必人四点：#4 环境 / #5 拆分 / #12 L4 / #16 晋升 + #7 例外）：
  默认 ["human"]；放开给 agent 须显式全局开关 allow_agent_on_human_gates
  （问责链弱化应当是一次显式、全局、留痕的授权）。
- 自动应答只作用于 kind=decision 关口（fact 的 agent 层已在相位内消耗、
  retry 的 agent 层=诊断链）。PORTER_NO_AGENT=1 时两层全跳过（守护闸门）。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

TIERS = ("rules", "agent", "human")

# 必人关口（判定表内置默认；放开须 allow_agent_on_human_gates）
HARD_HUMAN_IDS = {
    "p0.t5.env_gate",        # #4 物理环境（agent 只备料）
    "cp1.strategy",          # #5 拆分/范围决策
    "p6.l4.finalize",        # #12 L4 签字
    "cp5.promote",           # #16 知识策展
}

_REPO_CONFIG = Path(__file__).resolve().parent.parent / "config.json"

DEFAULT_CHAIN = ["rules", "agent", "human"]


def _is_hard_human(gate_id: str) -> bool:
    return gate_id in HARD_HUMAN_IDS


def _builtin_default(gate_id: str) -> list[str] | None:
    """内置判定表 fallback（配置无覆盖时生效）。

    注意：#7 例外（register-fill 动平台 gap）的硬路由在 p3 分类代码层
    实现（转 human 策略进 gap 关口），不经路由键。
    """
    if gate_id in HARD_HUMAN_IDS:
        return ["human"]
    if gate_id in ("p0.t3.extract", "p0.category.none",
                   "p0.category.unparseable"):
        return ["human"]            # agent 层已消费（T3 三轮 / T2 识别）
    if gate_id == "p1.resolve.cycles":
        return ["agent", "human"]   # agent 搬运已 3 轮
    if gate_id == "loop.budget":
        return ["human"]
    return None


def load_routing(ws: Path | None = None) -> dict:
    """两级合并：工作区 routing.json > 仓级 config.json routing 节。"""
    cfg: dict = {}
    try:
        cfg = json.loads(_REPO_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cfg = {}
    routing = dict(cfg.get("routing") or {})
    if ws is not None:
        wp = Path(ws) / "routing.json"
        if wp.exists():
            try:
                wcfg = json.loads(wp.read_text(encoding="utf-8"))
                wr = wcfg.get("routing") or {}
                routing.setdefault("gates", {}).update(wr.get("gates") or {})
                for k in ("default",):
                    if k in wr:
                        routing[k] = wr[k]
                routing["_workspace_file"] = str(wp)
            except (OSError, json.JSONDecodeError):
                pass
    return routing


def validate_routing(routing: dict) -> list[str]:
    """配置护栏：未知层 / 空链 / 链缺 human 且非全自动点。返回警告清单。"""
    warns: list[str] = []
    default = routing.get("default") or DEFAULT_CHAIN
    for t in default:
        if t not in TIERS:
            warns.append(f"default 层未知: {t}（合法 {TIERS}）")
    if not default:
        warns.append("default 链为空")
    for gid, chain in (routing.get("gates") or {}).items():
        if not isinstance(chain, list) or not chain:
            warns.append(f"{gid}: 链为空或非数组")
            continue
        for t in chain:
            if t not in TIERS:
                warns.append(f"{gid}: 层未知 {t}")
        if "human" not in chain and "agent" not in chain:
            warns.append(f"{gid}: 链既无 human 也无 agent——未命中规则时"
                         "无兜底（全自动点需显式确认）")
    return warns


def route_for(gate_id: str, ws: Path | None = None,
              routing: dict | None = None) -> list[str]:
    """关口 → 有序层链。优先级：硬路由保护 > 键覆盖 > 内置默认 > default。"""
    routing = routing if routing is not None else load_routing(ws)
    gates_cfg = routing.get("gates") or {}
    allow = bool(routing.get("allow_agent_on_human_gates"))
    override = None
    parts = gate_id.split(".")
    for i in range(len(parts), 0, -1):     # 特异性：长前缀优先
        key = ".".join(parts[:i])
        if key in gates_cfg:
            override = list(gates_cfg[key])
            break
    if _is_hard_human(gate_id):
        if not allow:
            return ["human"]               # 硬锁：显式放开前链强制 human
        base = override or ["agent", "human"]
        return base if "human" in base else base + ["human"]
    if override is not None:
        return override
    builtin = _builtin_default(gate_id)
    if builtin is not None:
        return list(builtin)
    return list(routing.get("default") or DEFAULT_CHAIN)


# ---------- policy.md（rules 层·决策型） ----------

def policy_path(ws: Path) -> Path:
    routing = load_routing(ws)
    name = routing.get("policy_file") or \
        (load_repo_raw().get("policy_file") or "policy.md")
    return Path(ws) / name


def load_repo_raw() -> dict:
    try:
        return json.loads(_REPO_CONFIG.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def consult_policy(ws: Path, gate: dict) -> dict | None:
    """rules 层：policy.md 命中查询（agent 解释，1 次有界）。

    返回 {hit, rule_id, answer, confidence} 或 None（无 policy 文件 /
    PORTER_NO_AGENT / agent 失败——失败不阻塞，链进下一层）。命中即记
    遥测（ws/policy_hits.json + events）。
    """
    p = policy_path(ws)
    if not p.exists() or os.environ.get("PORTER_NO_AGENT"):
        return None
    from ..common import agent as agent_mod
    skill = agent_mod.load_skill("gate-answer")
    prompt = (f"{skill}\n\n---\n\n## 常备规则（policy.md，人工事先写定）\n"
              f"{p.read_text(encoding='utf-8')[:6000]}"
              f"\n\n## 待裁关口\n- id: {gate['id']}\n- 问题: "
              f"{gate.get('question', '')[:400]}\n- 表单: "
              f"{json.dumps(gate.get('answer_form') or [], ensure_ascii=False)}"
              "\n\n## 任务（rules 层）\n若某条规则明确覆盖此关口，输出 "
              '{"hit": true, "rule_id": "...", "answer": {表单字段: 值}, '
              '"confidence": "high|low"}；否则输出 {"hit": false}。'
              "只输出一个紧凑 JSON。")
    try:
        rc, out = agent_mod.run_agent(
            prompt, workdir=Path(ws), timeout_sec=300,
            log_stem=str(Path(ws) / "P0" / "logs" / "policy_consult"))
        parsed = agent_mod.extract_json(out) if rc == 0 else None
    except Exception:
        return None
    if not (parsed and parsed.get("hit")):
        return None
    _record_hit(ws, parsed.get("rule_id") or "?", gate["id"], gate=gate)
    return parsed


def _record_hit(ws: Path, rule_id: str, gate_id: str,
                gate: dict | None = None) -> None:
    hp = Path(ws) / "policy_hits.json"
    try:
        data = json.loads(hp.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {"hits": {}}
    data["hits"][rule_id] = data["hits"].get(rule_id, 0) + 1
    hp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                  encoding="utf-8")
    try:
        from . import events as _ev
        _ev.append_event("policy-hit", subject=gate_id,
                         summary=f"rule={rule_id}",
                         module=(gate or {}).get("module"))
    except Exception:
        pass


# ---------- agent 层（gate-answer skill·决策型） ----------

def agent_answer(ws: Path, gate: dict) -> dict | None:
    """agent 层：知识库检索 + 照表单作答。

    返回 {answer, confidence, rationale, kb_consulted?} 或 None。
    检索面 = 已审分区（知识库目录 + base）——temp 草稿不参与自动应答
    （信任分层定案：自动行为只依据人写的规则与人审过的知识）。
    域选择 = 关口类型的确定性映射（suggest_class + pitfalls 兜底）。
    """
    if os.environ.get("PORTER_NO_AGENT"):
        return None
    from ..common import agent as agent_mod
    skill = agent_mod.load_skill("gate-answer")
    # KB 面：确定性域预选（无 agent 参与）→ 总纲 + 条目目录
    kb_doms: list[str] = []
    face = ""
    try:
        from ..bootstrap import candidates as _cand
        from ..bootstrap import kb as _kb
        for d in ([_cand.suggest_class(gate.get("id", ""))] + ["pitfalls"]):
            if d not in kb_doms:
                kb_doms.append(d)
        face = _kb.kb_face(ws, kb_doms, include_temp=False)
    except Exception:
        pass
    ctx = ""
    for c in (gate.get("context_files") or [])[:4]:
        p = Path(ws) / c
        if p.is_file():
            try:
                ctx += f"\n### {c}\n" + p.read_text(
                    encoding="utf-8", errors="replace")[:3000] + "\n"
            except OSError:
                pass
    prompt = (f"{skill}\n\n---\n\n## 待答关口\n- id: {gate['id']}\n"
              f"- 问题: {gate.get('question', '')[:600]}\n- 表单: "
              f"{json.dumps(gate.get('answer_form') or [], ensure_ascii=False)}"
              + (f"\n---\n\n{face}\n" if face else "")
              + f"\n## 证据材料{ctx or '（无）'}"
              "\n\n## 任务（agent 层）\n先按知识面规则检索（规则 0），"
              "再照表单作答。输出紧凑 JSON："
              '{"answer": {字段: 值}, "confidence": "high|low", '
              '"rationale": "...", "kb_consulted": [读过的条目文件名]}'
              "（kb_consulted 未读则省略）。只输出一个 JSON。")
    try:
        rc, out = agent_mod.run_agent(
            prompt, workdir=Path(ws), timeout_sec=600,
            log_stem=str(Path(ws) / "P0" / "logs" / "gate_answer"))
        parsed = agent_mod.extract_json(out) if rc == 0 else None
    except Exception:
        return None
    if not (parsed and parsed.get("answer")):
        return None
    cons = parsed.get("kb_consulted")
    if isinstance(cons, list) and kb_doms:
        try:
            from ..bootstrap import kb as _kb
            kb_dir = _kb.kb_dir_for(ws)
            for d in kb_doms:
                _kb.record_consulted(kb_dir, d, cons)
        except Exception:
            pass
    return parsed


def maybe_auto_answer(ws: Path, ledger, gate: dict) -> bool:
    """在关口开给人之前按层链尝试自动应答（rules → agent）。

    仅 kind=decision 参与（fact 的 agent 层已在相位内消耗；retry 的
    agent 层=诊断链）。命中并应用 → True（关口转为决策债 applied）；
    否则 False（开给人）。所有自动应答记 debt_class 供债计数收窄。
    """
    if gate.get("kind") != "decision":
        return False
    chain = route_for(gate["id"], ws)
    ans = None
    answered_by = None
    conf = "low"
    rule_id = None
    if "rules" in chain:
        hit = consult_policy(ws, gate)
        if hit:
            ans, answered_by = hit.get("answer"), "policy"
            conf = hit.get("confidence") or "low"
            rule_id = hit.get("rule_id")
    if ans is None and "agent" in chain:
        got = agent_answer(ws, gate)
        if got and got.get("confidence") == "high":
            ans, answered_by = got.get("answer"), "agent"
            conf = got.get("confidence")
    if ans is None or answered_by is None:
        return False
    # 校验（不合格视同未命中——链继续开给人）
    errs = None
    from .gates import validate_answer
    errs = validate_answer(gate.get("answer_form"), ans)
    if errs:
        return False
    gate["answer"] = ans
    gate["answered_by"] = answered_by
    gate["answered_at"] = datetime.now().isoformat(timespec="seconds")
    gate["agent_draft"] = {"confidence": conf, "policy_hit": rule_id}
    gate["debt_class"] = _debt_class(gate, ans)
    resolution = _apply_answer(ws, gate, ans)
    gate["resolution"] = resolution
    gate["status"] = "applied"
    gate["history"].append({"time": datetime.now().isoformat(
        timespec="seconds"), "event": "auto-answered",
        "detail": f"{answered_by} conf={conf}; {resolution[:300]}"})
    print(f"[porter] routing: {answered_by} 层自动应答 {gate['id']}"
          f"（conf={conf}）→ 决策债")
    try:
        from . import events as _ev
        _ev.append_event("gate-auto-answered", subject=gate["id"],
                         summary=f"{answered_by} conf={conf}",
                         module=gate.get("module"))
    except Exception:
        pass
    return True


def _apply_answer(ws: Path, gate: dict, ans: dict) -> str:
    from . import gates as gates_mod
    return gates_mod._apply(ws, gate, ans)


def _debt_class(gate: dict, ans: dict) -> str:
    """债分类（收窄计数用）：
    skip=跳过决策（bypass/not-migrated 类）、measure=改量尺、low=低置信
    放行、general=其余（下游有机器验证兜底，不计数）。"""
    if gate.get("target") == "gap" and \
            str(ans.get("strategy", "")) in ("bypass",):
        return "skip"
    if gate.get("id", "").startswith("criteria"):
        return "measure"
    if (gate.get("agent_draft") or {}).get("confidence") == "low":
        return "low"
    return "general"


def debt_count(ledger) -> int:
    """收窄后的债计数：skip/measure/low 三类（general 不计——
    下游机器验证兜底，现实本身就是复核者）。"""
    return sum(1 for g in ledger.pending_review()
               if g.get("debt_class") in ("skip", "measure", "low"))


def debt_limit(ws: Path | None = None) -> int:
    raw = load_routing(ws)
    cp = raw.get("checkpoints") or load_repo_raw().get("checkpoints") or {}
    return int(cp.get("decision_debt_limit", 30))
