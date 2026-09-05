"""scaffold.py — P2b 框架引导（发现式骨架，替换旧硬编码 skeleton）。

闭环（用户定案 2026-09-05；同日 session 化改造）：
  ① 方案：agent 读目标树 → 产出施工单 recipe（骨架代码 + 幂等接线编辑
     + 验收特征 + 探针底座契约 + api_claims join 键）——**写文件而非
     消息正文**（编排器下发输出路径，裸 JSON 禁围栏；消息只回"已写入"，
     规避 stdout 提取的截断/围栏坑）。**单 session 贯穿全部轮次**：
     首轮全量任务，后续轮经 --session 续接只发增量（证据指针）。
  ② 落地：recipe_apply 照单施工（marker 幂等；journal 可回滚）
  ③ 验证：三信号（build / boot_with_device + 验收特征 grep / 单测 smoke）
     ——全走 P0 runner 机器；完整验证结果落盘 verify 文件，回炉轮的
     agent 自行读取（指针优于载荷）
  ④ 闭环：FAIL（排除 infra）→ 带证据指针回炉（≤MAX_ROUNDS 轮，每轮
     先 rollback 上一轮施工）→ 仍败 → 人工关口 p2.scaffold.fail；
     输出质量问题（缺文件/坏 JSON/校验缺陷）不烧轮次——同会话微增量
     续接修（≤AGENT_TRIES 次），修不动 = 静态 panic（程序错误类，
     RuntimeError 直抛，不走人工关口）
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
from ..common import scope as _scope
from .. import log as _log
from . import recipe_apply

MAX_ROUNDS = 10              # 方案回炉有界轮数（仿 T3/探针生命周期；
                              #  零知识库 cold-start 实验放宽 3→10）
AGENT_TRIES = 2              # 每轮内 JSON 解析失败的即席重试
AGENT_TIMEOUT_SEC = 1200     # 发现要读全树找先例，比映射批宽

RECIPE_NAME = ("P2", "reports", "scaffold_recipe.json")
MANIFEST_NAME = ("P2", "reports", "scaffold_manifest.json")
JOURNAL_NAME = ("P2", "reports", "scaffold_apply_journal.json")
OUT_DIR = ("P2", "reports", "out")       # agent 侧每轮产物（scaffold_r<N>.json）


def load_manifest(ws: Path) -> dict | None:
    p = ws.joinpath(*MANIFEST_NAME)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def dormitory_abs(ws: Path, target_os: Path) -> Path | None:
    """探针宿舍绝对路径（唯一真值源 = scaffold manifest 的 dormitory）。

    manifest 缺失/无 dormitory → None：意味着 P2b 未成功完成——调用方
    应以前置缺失处理（先跑 p2-scaffold），不猜测路径（2026-09-05 定案：
    不留 Asterinas 约定路径回落）。
    """
    m = load_manifest(ws)
    if m and m.get("dormitory"):
        return target_os / str(m["dormitory"])
    return None


# ---------- prompt ----------

def _prompt(skill: str, target_os: Path, driver: str, categories: list[str],
            ids: list[str], hints: str, redesigns: list[dict],
            out_path: Path) -> str:
    rd = ""
    if redesigns:
        rd = ("\n- 相关换思路裁定（P2a，仅当涉及框架形态时参考）：\n"
              + "\n".join(f"  - {r.get('id', '?')}: "
                          f"{str(r.get('target_approach', ''))[:120]}"
                          for r in redesigns[:8]))
    if ids:
        dev_line = (f"- 设备匹配说明：{ids}（认领键，形态依总线而定——"
                    "PCI 为 vendor:device；以其为准写骨架认领表）")
    else:
        dev_line = ("- 设备匹配说明：未提供——认领键依总线/框架而定"
                    "（PCI vendor:device / USB idVendor:idProduct / "
                    "SPI compatible 字符串等），从 runner 注入配置与"
                    "目标树同类先例自行收敛；纯软件驱动（无硬件设备，"
                    "如 device-mapper 类框架）可无设备注入，boot 验证"
                    "退化为注册/认领特征")
    return (f"{skill}\n\n---\n\n## 任务数据\n"
            f"- 目标 OS 源码树：`{target_os}` = 你的工作目录\n"
            f"- 驱动名：`{driver}`（Linux 侧仅作设备行为参考；"
            f"要搭的是**目标 OS 侧**的驱动框架）\n"
            f"- 设备类别：{categories or '未知'}\n"
            f"{dev_line}\n"
            f"- **施工单输出路径：`{out_path}`**（完整 recipe 以裸 JSON "
            "写入该文件——禁 markdown 围栏；消息正文只回一行"
            "「已写入 <路径>」）\n"
            f"- 三信号验证（编译/带设备 boot/单测）由编排器外部执行——"
            "**禁止你自己运行**（含等价局部命令，见 SKILL 铁律 7）；"
            "结果以文件指针发回，须自行读取\n"
            + (f"\n## 知识库目录（提示，非证据；条目内容自己去读）\n{hints}\n"
               if hints else "")
            + (f"{rd}\n" if rd else "")
            + "\n按 SKILL 产出施工单并写入上述输出路径。")


def _read_recipe(path: Path) -> tuple[dict | None, str]:
    """agent 写的施工单文件 → (recipe, 失败原因)。容 {"recipe": {...}} 包装。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None, "输出文件不存在"
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as ex:
        return None, f"JSON 解析失败: {ex}"
    if isinstance(obj, dict) and isinstance(obj.get("recipe"), dict):
        return obj["recipe"], ""
    if isinstance(obj, dict):
        return obj, ""
    return None, "顶层不是 JSON 对象"


def _write_verify_evidence(ws: Path, target_os: Path, res: dict,
                           verdict: dict, rnd: int, runner: dict) -> Path:
    """三信号验证完整结果落盘——回炉轮续接 prompt 的指针目标（自读）。"""
    p = ws / "P2" / "logs" / f"P2B_scaffold_verify_r{rnd}.log"
    lines = [f"# P2b 框架引导第 {rnd} 轮三信号验证结果：FAIL", ""]
    v = verdict.get("build") or {}
    lines.append(f"- build：{v.get('detail', '?')}")
    label = verdict.get("build_label") or ""
    bl = ws / "P2" / "logs" / f"{label}.log"
    if label and bl.exists():
        lines.append(f"- 完整 build 日志：{bl.resolve()}（自行 grep/tail）")
        tail = _build_log_tail(ws, label)
        if tail:
            lines.append("- build 关键行过滤（error/warning/panic，尾 40 行）：")
            lines.append("```text")
            lines.append(tail)
            lines.append("```")
    if verdict.get("boot_detail"):
        lines.append(f"- boot：{verdict['boot_detail']}")
        lf = ((runner.get("boot") or {}).get("log_file"))
        if lf:
            lines.append(f"- 完整 boot 日志：{(target_os / lf).resolve()}"
                         "（验收特征须在此日志中真实出现）")
    pats = verdict.get("patterns") or {}
    if pats:
        miss = [p_ for p_, n in pats.items() if n < 1]
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
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _rework_message(verdict: dict, verify_path: Path, prev_out: Path,
                    new_out: Path) -> str:
    """回炉轮的续接消息：一行结论摘要 + 指针（细节 agent 自读文件）。"""
    pats = verdict.get("patterns") or {}
    miss = [p_ for p_, n in pats.items() if n < 1]
    ut = verdict.get("ut")
    summ = (f"build={'P' if (verdict.get('build') or {}).get('ok') else 'F'} "
            f"boot={'P' if verdict.get('boot_ok') else 'F'} "
            f"patterns={'F（未命中 ' + str(len(miss)) + ' 条）' if miss else 'P'}"
            f" ut={'skip' if ut is None else ('P' if ut[0] else 'F')}")
    return ("---\n\n## 上一轮三信号验证 FAIL——修订施工单\n\n"
            f"- 结论：{summ}\n"
            f"- 完整验证结果与证据：`{verify_path}`（**自行读取**定位问题"
            "——验证由编排器执行，勿自行运行编译/启动/单测，只读证据"
            "文件与源码）\n"
            f"- 上轮施工单：`{prev_out}`（修订基础，勿改动）\n"
            f"- 修订后的**完整**施工单写入：`{new_out}`（裸 JSON，整文件重写）\n"
            "\n提醒：对已生效但内容有误的编辑必须换 marker/id"
            "（旧 marker 会被幂等跳过）。")


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
    driver = _scope.driver_name_of(proj)
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
    driver = _scope.driver_name_of(proj)
    if not proj.get("driver_name"):
        _log.console_line(f"[porter] P2b: ⚠️ project.json 无 driver_name"
              f"（无 scope 提案）——回退目录名 {driver!r} 作驱动身份；"
              "一个目录住多套体系时身份可能错（建议走意图+scope 流程）")
    categories = proj.get("category") or []
    ids = device_ids or []           # 无默认：未提供时任务数据给引导行
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
    (ws.joinpath(*OUT_DIR)).mkdir(parents=True, exist_ok=True)
    base_prompt = _prompt(skill, target_os, driver, categories, ids,
                          hints, redesigns,
                          ws.joinpath(*OUT_DIR) / "scaffold_r1.json")
    session_id: str | None = None
    feedback = ""                # 回炉轮续接消息（证据指针）
    last_stem = ""
    for rnd in range(1, MAX_ROUNDS + 1):
        _log.console_line(f"[porter] P2b: 框架引导第 {rnd}/{MAX_ROUNDS} 轮"
                          "（发现 → 施工 → 三信号验证）")
        out_path = ws.joinpath(*OUT_DIR) / f"scaffold_r{rnd}.json"
        # 防脏读：上次运行可能残留同名旧文件——若 agent 本次忘写，
        # _read_recipe 会把旧文件误当本次产物。每轮首发前清掉。
        out_path.unlink(missing_ok=True)
        recipe, quality_note = None, ""
        for attempt in range(1, AGENT_TRIES + 1):
            if attempt == 1:
                message = base_prompt if rnd == 1 else feedback
                # r1 首发 = skill+任务数据；r≥2 = 续接增量（证据指针）
            else:
                message = quality_note      # 同轮质量续接（微增量，不烧轮）
            stem = str(p2 / "logs" / f"P2B_scaffold_r{rnd}_R{attempt}")
            last_stem = stem
            rc, out = agent._opencode_json_runner(
                message, workdir=target_os, log_stem=stem,
                timeout_sec=AGENT_TIMEOUT_SEC, session_id=session_id,
                task={"phase": "P2", "step": "scaffold", "attempt": rnd})
            ev = agent._parse_events(out)   # rc≠0 也试：先抢救 session_id
            if ev and ev.get("session_id"):
                session_id = ev["session_id"]
            if session_id is None:
                # 静态 panic（程序错误类，非人工关口）：会话化后 session
                # 是硬依赖——解析不到 = opencode 版本/登录层面的问题
                raise RuntimeError(
                    "P2b: session id not found "
                    f"(round {rnd} attempt {attempt} rc={rc}; "
                    f"log={stem}.log)——opencode --format json 未产出 "
                    "sessionID 事件；检查 opencode 版本与登录状态")
            recipe, read_err = _read_recipe(out_path)
            if recipe is not None:
                errs = recipe_apply.validate_recipe(recipe, driver)
                if not errs:
                    break
                quality_note = (
                    "---\n\n## 上一次施工单的校验缺陷（修订后重写整个文件）\n"
                    + "; ".join(errs[:10])
                    + f"\n\n修订后的完整施工单（裸 JSON）重写到 `{out_path}`。")
                recipe = None
            else:
                quality_note = (
                    "---\n\n## 上一次输出的问题\n"
                    f"施工单文件不可用：{read_err}。"
                    f"把**完整**施工单（裸 JSON）写完到 `{out_path}`。")
        if recipe is None:
            raise RuntimeError(
                f"P2b: 施工单输出质量问题经 {AGENT_TRIES} 次同轮续接仍未"
                f"修复（round {rnd}；输出 {out_path}；日志 {last_stem}.log）"
                "——会话疑似空转，需人工检查会话状态与模型输出")
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
        _log.console_line(f"[porter] P2b: 第 {rnd} 轮验证 FAIL——带证据回炉"
                          "（同会话续接）")
        verify_path = _write_verify_evidence(ws, target_os, res, verdict,
                                             rnd, runner)
        feedback = _rework_message(
            verdict, verify_path, out_path,
            ws.joinpath(*OUT_DIR) / f"scaffold_r{rnd + 1}.json")

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
