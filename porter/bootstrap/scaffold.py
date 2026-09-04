"""scaffold.py — P2b 框架引导（发现式骨架，替换旧硬编码 skeleton）。

闭环（用户定案 2026-09-05）：
  ① 方案：agent 读目标树 → 产出施工单 recipe（骨架代码 + 幂等接线编辑
     + 验收特征 + 探针底座契约 + api_claims join 键）
  ② 落地：recipe_apply 照单施工（marker 幂等；journal 可回滚）
  ③ 验证：三信号（build / boot_with_device + 验收特征 grep / 单测 smoke）
     ——全走 P0 runner 机器；日志恢复共享 _recover_boot_log 的 infra 语义
  ④ 闭环：FAIL（排除 infra）→ 带证据回炉 recipe（≤MAX_ROUNDS 轮，
     每轮先 rollback 上一轮施工）→ 仍败 → 人工关口 p2.scaffold.fail
  ⑤ 收尾：scaffold_manifest（含宿舍路径，P2c/probes 消费）+ mapping
     批注（api_claims join → verified_by，置信上调）+ kb 候选 + vcs commit

前置（独立可跑，校准/新 OS 复用此形态）：project.json + runner.json。
mapping.json 存在则注入 redesigns（可选输入）；kb 血统（pitfalls/maps
目录面）注入作提示。幂等：manifest 存在即跳过。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..common import agent
from .. import log as _log
from . import recipe_apply

MAX_ROUNDS = 3               # 方案回炉有界轮数（仿 T3/探针生命周期）
AGENT_TRIES = 2              # 每轮内 JSON 解析失败的即席重试
AGENT_TIMEOUT_SEC = 1200     # 发现要读全树找先例，比映射批宽
# 默认设备 ID：QEMU `-device e1000` = 82540EM（设备侧事实，非 OS 侧；
# P1 策略收敛或 --device-ids 可覆盖）
DEFAULT_DEVICE_IDS = ["0x8086:0x100e"]

RECIPE_NAME = ("P2", "reports", "scaffold_recipe.json")
MANIFEST_NAME = ("P2", "reports", "scaffold_manifest.json")
JOURNAL_NAME = ("P2", "reports", "scaffold_apply_journal.json")


def load_manifest(ws: Path) -> dict | None:
    p = ws.joinpath(*MANIFEST_NAME)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def dormitory_abs(ws: Path, target_os: Path, driver: str) -> Path:
    """探针宿舍绝对路径：scaffold manifest 优先，存量工作区回落旧路径。"""
    m = load_manifest(ws)
    if m and m.get("dormitory"):
        return target_os / str(m["dormitory"])
    return target_os / "kernel" / "core" / "comps" / driver / "src" \
        / "probes.rs"


# ---------- prompt ----------

def _prompt(skill: str, target_os: Path, driver: str, categories: list[str],
            ids: list[str], hints: str, redesigns: list[dict]) -> str:
    rd = ""
    if redesigns:
        rd = ("\n- 相关换思路裁定（P2a，仅当涉及框架形态时参考）：\n"
              + "\n".join(f"  - {r.get('id', '?')}: "
                          f"{str(r.get('target_approach', ''))[:120]}"
                          for r in redesigns[:8]))
    return (f"{skill}\n\n---\n\n## 任务数据\n"
            f"- 目标 OS 源码树：`{target_os}` = 你的工作目录\n"
            f"- 驱动名：`{driver}`（Linux 侧仅作设备行为参考；"
            f"要搭的是**目标 OS 侧**的驱动框架）\n"
            f"- 设备类别：{categories or '未知'}\n"
            f"- 设备 ID 收敛清单：{ids}\n"
            + (f"\n## 知识库目录（提示，非证据；条目内容自己去读）\n{hints}\n"
               if hints else "")
            + (f"{rd}\n" if rd else "")
            + "\n按 SKILL 产出施工单，只输出一个紧凑 JSON 块。")


def _parse_out(out: str) -> dict | None:
    """agent 输出 → JSON 对象。裸 JSON 直解析优先（skill 要求只输出一个
    JSON 对象）；混杂转录时回落共享 extract_json（```json 围栏路径）。"""
    text = (out or "").strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass
    return agent.extract_json(out)


def _evidence_block(res: dict, verdict: dict, build_log_tail: str) -> str:
    lines = ["\n\n---\n\n## 上一轮验证失败证据（修订施工单后重出完整 JSON）\n"]
    v = verdict.get("build") or {}
    lines.append(f"- build：rc 详情 {v.get('detail', '?')}")
    if build_log_tail:
        lines.append("```text\n" + build_log_tail + "\n```")
    if verdict.get("boot_detail"):
        lines.append(f"- boot：{verdict['boot_detail']}")
    pats = verdict.get("patterns") or {}
    if pats:
        miss = [p for p, n in pats.items() if n < 1]
        lines.append(f"- 验收特征命中：{pats}"
                     + (f"——未命中：{miss}" if miss else ""))
    if verdict.get("ut_detail"):
        lines.append(f"- 单测 smoke：{verdict['ut_detail']}")
    if res.get("warnings"):
        lines.append("- 施工告警（编辑未生效的常见根因是锚点/文件漂移）：")
        lines.extend(f"  - {w}" for w in res["warnings"][:12])
    lines.append("\n排查建议：先看施工告警（编辑是否根本没生效），再看 "
                 "build/boot 证据；修订后的施工单对已生效但内容有误的编辑"
                 "必须换 marker/id（旧 marker 会被幂等跳过）。")
    return "\n".join(lines)


def _build_log_tail(ws: Path, label: str) -> str:
    p = ws / "P2" / "logs" / f"{label}.log"
    if not p.exists():
        return ""
    keep = []
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if (ln.startswith(("error", "warning: unused", "panicked"))
                or " --> " in ln or "Error" in ln):
            keep.append(ln)
    return "\n".join(keep[-40:])[-3000:]


# ---------- 验证（三信号） ----------

def _verify(ws: Path, target_os: Path, runner: dict, proj: dict,
            recipe: dict, rnd: int) -> dict:
    """三信号验证。返回 {ok, infra, ...}；infra=True 时判定输入不存在，
    共享助手已登记 infra 关口，调用方应立即中止（exit 3）。"""
    label_b = f"P2B_scaffold_build_r{rnd}"
    from ..env import probe as probe_mod           # 延迟导入防环
    b = probe_mod.probe_build(ws / "P2", target_os, runner, label=label_b)
    if not b["ok"]:
        return {"ok": False, "infra": False, "build": b,
                "build_label": label_b}

    from ..loop import probes as probe_lib
    label_t = f"P2B_scaffold_boot_r{rnd}"
    boot_ok, log, state = probe_lib.boot_and_log(
        ws, "P2", target_os, proj, label_t)
    if state == "missing":
        return {"ok": False, "infra": True}
    patterns = {p: log.count(p)
                for p in (recipe.get("acceptance_patterns") or [])}
    pat_ok = bool(patterns) and all(n >= 1 for n in patterns.values())

    ut = runner.get("unit_test") or {}
    ut_res = None
    if ut and ut.get("mechanism") not in (None, "none") \
            and ut.get("smoke_cmd"):
        from ..loop.ut_verify import smoke_unit_test_config
        ut_res = smoke_unit_test_config(
            ws, target_os, runner, ut, label=f"P2B_scaffold_ut_r{rnd}")

    ok = bool(b["ok"] and boot_ok and pat_ok
              and (ut_res is None or ut_res[0]))
    try:
        _log.judge("P2B_scaffold", ok,
                   detail=f"build={'P' if b['ok'] else 'F'} "
                          f"boot={'P' if boot_ok else 'F'} "
                          f"patterns={'P' if pat_ok else 'F'} "
                          f"ut={'skip' if ut_res is None else ('P' if ut_res[0] else 'F')}",
                   intent="boot", phase=(probe_mod.store_mounted() or None))
    except Exception:
        pass
    return {"ok": ok, "infra": False, "build": b, "boot_ok": boot_ok,
            "patterns": patterns, "ut": ut_res,
            "boot_detail": f"ok={boot_ok} log_state={state}",
            "ut_detail": (None if ut_res is None
                          else f"ok={ut_res[0]} {ut_res[1][:160]}")}


# ---------- 收尾 ----------

def _annotate_mapping(ws: Path, recipe: dict) -> int:
    """api_claims join → 既有条目批注（verified_by + 置信上调）。

    只动 confidence（升 high），不动 risk——骨架 boot 只证明了用法形态，
    DMA/中断语义等风险维度未被锻炼，探针该照照（保守正确）。
    """
    from .mapping import _load_mapping, _save
    p2 = ws / "P2"
    if not (p2 / "mapping.json").exists():
        return 0
    mapping = _load_mapping(p2)
    index = {e["linux_api"]: e for e in mapping.get("entries", [])}
    tag = "verified_by=scaffold(P2b)"
    n = 0
    for c in recipe.get("api_claims") or []:
        e = index.get(str(c.get("linux_api") or ""))
        if not e or e.get("verdict") not in ("direct", "adapt"):
            continue
        if e.get("confidence") != "high":
            e["confidence"] = "high"
            n += 1
        notes = e.get("notes") or ""
        if tag not in notes:
            e["notes"] = (notes.rstrip() + f"｜{tag}").lstrip("｜")
    if n:
        _save(mapping, p2)
    return n


def _finalize(ws: Path, target_os: Path, proj: dict, recipe: dict,
              res: dict, verdict: dict, rnd: int) -> None:
    driver = Path(proj["linux_driver"]).name
    home = str(recipe["driver_home"])
    pc = recipe.get("probe_channel") or {}
    commit_paths = sorted({home, *res["created"],
                           *{e.get("file", "") for e in recipe.get("edits") or []}
                           } - {""})
    manifest = {
        "generated": datetime.now().isoformat(),
        "driver": driver,
        "language": recipe.get("language"),
        "driver_home": home,
        "dormitory": str(Path(home) / str(pc.get("dormitory_rel") or "")),
        "created": res["created"],
        "edits_applied": res["edits_applied"],
        "skipped": res["skipped"],
        "acceptance_log_patterns": recipe.get("acceptance_patterns") or [],
        "probe_channel": pc,
        "test_substrate": recipe.get("test_substrate") or {},
        "verified": {"build": True,
                     "boot_with_device": verdict.get("boot_ok"),
                     "patterns": verdict.get("patterns"),
                     "unit_smoke": verdict.get("ut_detail")},
        "attempts": rnd,
        "commit_paths": commit_paths,
    }
    ws.joinpath(*MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8")
    n_anno = _annotate_mapping(ws, recipe)
    _log.console_line(
        f"[porter] P2b: 框架引导 PASS（{rnd} 轮）——新建 {len(res['created'])} 文件，"
        f"接线 {len(res['edits_applied'])} 处"
        + (f"，跳过 {len(res['skipped'])} 处" if res["skipped"] else "")
        + f"；mapping 批注 {n_anno} 条")
    try:                                # 类 2 钩子：验证过的习语 → 知识候选
        from . import candidates as _cand
        _cand.record_candidate(
            ws, hook="scaffold-verified", ref=driver,
            draft=f"P2b 框架引导三信号验证通过（{recipe.get('language')}"
                  f"骨架，driver_home={home}，接线 {len(res['edits_applied'])}"
                  f" 处）——注册/日志/测试习语与探针底座契约见 "
                  f"scaffold_manifest.json",
            evidence=["P2/reports/scaffold_manifest.json"],
            suggested="maps")
    except Exception:
        pass
    try:                                # vcs：P2b 骨架 + 接线（best-effort）
        from ..common import vcs as _vcs
        _vcs.commit_target(ws, "P2b: scaffold + wiring",
                           paths=commit_paths, phase="P2")
    except Exception:
        pass


# ---------- 主入口 ----------

def run_scaffold(ws: Path, target_os: Path,
                 device_ids: list[str] | None = None) -> int:
    """返回 0=成功；2=前置缺失；3=需人工（回炉耗尽/infra 关口）。幂等。"""
    p2 = ws / "P2"
    if ws.joinpath(*MANIFEST_NAME).exists():
        _log.console_line(f"[porter] P2b: 复用框架（scaffold_manifest 存在；"
                          f"如需重做请删除该文件并回滚目标树施工改动）")
        return 0
    for need, name in ((ws / "project.json", "project.json"),
                       (ws / "runner.json", "runner.json")):
        if not need.exists():
            _log.console_line(f"[porter] P2b: 缺少 {name}（先跑 p0）")
            return 2
    proj = json.loads((ws / "project.json").read_text(encoding="utf-8"))
    runner = json.loads((ws / "runner.json").read_text(encoding="utf-8"))
    driver = Path(proj["linux_driver"]).name
    categories = proj.get("category") or []
    ids = device_ids or DEFAULT_DEVICE_IDS
    (p2 / "logs").mkdir(parents=True, exist_ok=True)
    (p2 / "reports").mkdir(parents=True, exist_ok=True)

    skill = agent.load_skill("P2-scaffold")
    hints = ""
    try:
        from . import kb
        hints = kb.kb_face(ws, ["pitfalls", "maps"])
    except Exception:
        pass
    redesigns: list[dict] = []
    try:
        if (p2 / "mapping.json").exists():
            redesigns = json.loads(
                (p2 / "mapping.json").read_text(encoding="utf-8")
            ).get("redesigns") or []
    except (OSError, json.JSONDecodeError):
        pass

    journal_path = ws.joinpath(*JOURNAL_NAME)
    base_prompt = _prompt(skill, target_os, driver, categories, ids,
                          hints, redesigns)
    feedback = ""
    for rnd in range(1, MAX_ROUNDS + 1):
        _log.console_line(f"[porter] P2b: 框架引导第 {rnd}/{MAX_ROUNDS} 轮"
                          "（发现 → 施工 → 三信号验证）")
        prompt = base_prompt + feedback
        parsed = None
        for attempt in range(1, AGENT_TRIES + 1):
            rc, out = agent.run_agent(
                prompt, workdir=target_os,
                log_stem=str(p2 / "logs" / f"P2B_scaffold_r{rnd}_R{attempt}"),
                timeout_sec=AGENT_TIMEOUT_SEC,
                task={"phase": "P2", "step": "scaffold", "attempt": rnd})
            parsed = _parse_out(out) if rc == 0 else None
            if parsed:
                break
            prompt += ("\n\n---\n\n## 上一次输出的问题\n未见合法 JSON 块"
                       "（可能截断）。只输出一个紧凑 JSON 对象。")
        recipe = parsed.get("recipe") if isinstance(
            parsed, dict) and isinstance(parsed.get("recipe"), dict) \
            else parsed
        if not isinstance(recipe, dict):
            feedback = ("\n\n---\n\n## 上一轮输出无法解析为施工单 JSON"
                        "——重出完整 JSON。")
            continue
        errs = recipe_apply.validate_recipe(recipe, driver)
        if errs:
            feedback = ("\n\n---\n\n## 施工单校验缺陷（修正后重出完整 JSON）\n"
                        + "; ".join(errs[:10]))
            continue
        ws.joinpath(*RECIPE_NAME).write_text(
            json.dumps(recipe, ensure_ascii=False, indent=2),
            encoding="utf-8")

        recipe_apply.rollback(target_os, journal_path)   # 清上一轮残留
        res = recipe_apply.apply_recipe(target_os, recipe, journal_path)
        verdict = _verify(ws, target_os, runner, proj, recipe, rnd)
        if verdict.get("infra"):
            _log.console_line("[porter] P2b: boot 日志不可得——infra 关口"
                              "已登记，本轮中止（exit 3）")
            return 3
        if verdict["ok"]:
            _finalize(ws, target_os, proj, recipe, res, verdict, rnd)
            return 0
        _log.console_line(f"[porter] P2b: 第 {rnd} 轮验证 FAIL——带证据回炉")
        feedback = _evidence_block(
            res, verdict,
            _build_log_tail(ws, verdict.get("build_label") or ""))

    from ..loop import gates
    gates.panic(ws, {
        "id": "p2.scaffold.fail", "kind": "retry", "gate_type": "failure",
        "phase": "P2",
        "question": (f"框架引导 {MAX_ROUNDS} 轮回炉仍未通过三信号验证"
                     f"（驱动 {driver}）。施工单见 P2/reports/"
                     f"scaffold_recipe.json，各轮证据见 P2/logs/P2B_*。"
                     "请人工诊断：OS 是否具备收留该驱动的条件 / 施工单"
                     "锚点是否树漂移 / 验证特征是否选错；修复或给出"
                     "指示后重跑。"),
        "context_files": ["P2/reports/scaffold_recipe.json",
                          "runner.json"],
        "answer_form": [
            {"field": "note", "type": "text", "required": False,
             "hint": "诊断结论或修复说明（如手工修正施工单/换特征串）"}],
    })
    return 3
