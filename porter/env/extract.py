"""extract.py — T3 环境信息提取 v2（4-session 流水线，2026-09-05 定案）。

架构（用户定案；背景与消费侧改造清单见 TODO #17）：

    T3 = 4 个顺序 session：build → boot → inject_device → unit_test
    每 session = P2b 式直连循环（_opencode_json_runner + --session 续接；
    文件即信号，无 phase 协议）：
      - 输入三级：① 用户提示（P0/inputs/hints/<cap>.md，可选，最高权重）
        ② materials ③ 目标树（prompt 明示尽量在前两层解决）
      - 两类禁跑（prompt 级，原因/边界见 skill）：本能力类命令（验证
        权在静态脚本，结果才被采信且不烧 agent 预算）/ 前序 session
        已收敛命令（编排器已验证锁定，按只读材料参考）
      - 测试请求协议：agent 写 {"cmd", "timeout_sec"?}（裸 JSON）→
        静态脚本执行（probe._run，时长在 agent 预算之外）→ 完整结果
        落文件 → 续接消息只给一行结论 + 文件指针（agent 自读）
      - md 随做随记：备选/坑史/依据/不确定项绑事件写（skill 纪律）
      - 完成 = 片段过机器校验（节契约 + md 锚点）且该能力
        probe 真跑 PASS（终验只跑本能力，不重验前序——依赖定案）
      - 质量失败不烧轮：文件缺失/坏 JSON/锚点缺 → 同会话微
        增量续接 ≤QUALITY_TRIES 次 → 仍败 RuntimeError（静态 panic，
        程序错误类不进人工关口）
    资源（三独立上限，同签名检测不做——定案）：测试/终验轮数、
        agent 段总时、墙钟总时
    耗尽 → 人工环（A 类）：同 session 续接一次总结请求（agent 写
        已完成/未完成/给开发者的问题；不可得则编排器拼装兜底）→
        panic 关口 p0.t3.<cap>（exit 3）→ 人答 answers.md @节 →
        账本 → 重跑：新 session 种子 = 总结指针 + 答案全文（最高
        权重）+ 小额资源。session_id 跨进程续接 = TODO（opencode
        会话持久化），本轮总结文件即记忆载体。
    收官：4 对片段拼装 ws/runner.json + ws/runner.md（md 与节 JSON 的
    扩充关系是软性写作指引——skill 产出契约，不配机械等值检查）→
    冻结指纹入 project.json
        ["t3_frozen"]；T3_development.json / memo.md 照旧产出
        （T5/CP0 消费方零改动）。

对外兼容：validate_runner 保留（gate.py 消费）；probe 函数零改动；
老 R1-R3/R4 循环、runbook 目录注入位（后续由 kb runner 域接替，见
TODO #17）一并退役；exit 0=成功 / 3=需人工。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path

from ..common import agent
from . import probe as probe_mod
from .. import log as _log

# ---------- 能力与常量 ----------

CAPS = ("build", "boot", "inject", "unit_test")       # 顺序依赖
SECTION_KEY = {"build": "build", "boot": "boot",
               "inject": "inject_device", "unit_test": "unit_test"}
CAP_TITLE = {"build": "构建（build）", "boot": "启动（boot）",
             "inject": "设备注入（inject_device）",
             "unit_test": "单元测试（unit_test）"}

MAX_ROUNDS = 5                  # 每 session 测试/终验轮上限（定案：比老 3 放宽）
QUALITY_TRIES = 2               # 连续无有效产出的段数上限（P2b 惯例）
AGENT_BUDGET_SEC = {"build": 1800, "boot": 1200,
                    "inject": 1200, "unit_test": 1200}
WALL_BUDGET_SEC = {"build": 5400, "boot": 1800,
                   "inject": 1800, "unit_test": 1800}
TEST_TIMEOUT_DEFAULT = {"build": 3600, "boot": 900,
                        "inject": 900, "unit_test": 1800}
SUMMARY_BUDGET_SEC = 300        # 耗尽后的总结请求小预算
RESUME_ROUNDS = 2               # 人工续跑小额资源（定案）
RESUME_AGENT_SEC = 600
RESUME_WALL_SEC = 1800

STATE_PATH = ("reports", "t3_state.json")     # 相对 p0（ws/P0）

# runner.md（调用手册）必答锚点：通用 + 各能力专有（skill 同款语义；
# 任务数据里由本清单生成——代码是真值源，skill 不列实值防漂移）。
COMMON_ANCHORS = ("### 选择依据", "### 备选与被毙原因",
                  "### 坑史", "### 不确定项")
CAP_ANCHORS = {
    "build": ("### 实测耗时", "### 构建产物",
              "### 环境前置", "### 常见失败形态"),
    "boot": ("### 日志获取", "### 自退出机制",
             "### 成败特征选择", "### 消费点核实"),
    "inject": ("### 设备形态", "### 设备在场确证", "### 注入影响"),
    "unit_test": ("### 作用域收窄", "### 结果输出位置",
                  "### 成败特征选择", "### 内核参数依赖"),
}


def _required_anchors(cap: str) -> tuple[str, ...]:
    return COMMON_ANCHORS + CAP_ANCHORS.get(cap, ())


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------- runner 契约校验（字段级；validate_runner 供 gate.py 复用） ----------

def _check_build(b: dict) -> list[str]:
    defects = []
    if not b.get("cmd"):
        defects.append("build.cmd 为空")
    for f in ("timeout_full_sec", "timeout_inc_sec"):
        v = b.get(f)
        if not isinstance(v, (int, float)) or v <= 0:
            defects.append(f"build.{f} 缺失或非法")
    if isinstance(b.get("timeout_full_sec"), (int, float)) and \
       isinstance(b.get("timeout_inc_sec"), (int, float)) and \
       b["timeout_full_sec"] < b["timeout_inc_sec"]:
        defects.append("build.timeout_full_sec 应 ≥ timeout_inc_sec")
    return defects


def _check_boot(bo: dict) -> list[str]:
    defects = []
    if not bo.get("cmd"):
        defects.append("boot.cmd 为空")
    if not isinstance(bo.get("timeout_sec"), (int, float)) or \
            bo.get("timeout_sec", 0) <= 0:
        defects.append("boot.timeout_sec 缺失或非法"
                       "（probe_boot 强依赖——缺失会裸 KeyError，H10）")
    if not bo.get("log_is_stdout") and not bo.get("log_file"):
        defects.append("boot.log_file 为空（且 log_is_stdout 非 true）")
    for f in ("success_pattern", "panic_pattern"):
        if not bo.get(f):
            defects.append(f"boot.{f} 为空")
    return defects


def _check_inject(inj: dict) -> list[str]:
    defects = []
    mech = inj.get("mechanism")
    if mech not in ("env", "cmd"):
        defects.append("inject_device.mechanism 必须为 env|cmd")
    else:
        carrier = (inj.get("env") or {}) if mech == "env" \
            else {"cmd_suffix": inj.get("cmd_suffix")}
        if not carrier or not any("<DEVICE_ARGS>" in str(v) for v in carrier.values()):
            defects.append(f"inject_device（mechanism={mech}）载体缺 <DEVICE_ARGS> 占位符")
    if not inj.get("example_args"):
        defects.append("inject_device.example_args 为空")
    for f in ("driver_success_pattern", "driver_fail_pattern"):
        v = inj.get(f)
        if v is not None and not (isinstance(v, str) and v.strip()):
            defects.append(f"inject_device.{f} 须为非空字符串或 null"
                           "（null=目标 OS 无该类别内置驱动，不做驱动级判定）")
    return defects


def _check_unit_test(ut: dict) -> list[str]:
    """unit_test 节契约（v2：必填节，显式 none 合法——不允许静默缺课）。"""
    defects = []
    mech = ut.get("mechanism")
    if not mech:
        defects.append("unit_test.mechanism 为空（目标 OS 无内核单测机制时"
                       "显式填 \"none\"）")
    elif mech == "none":
        pass
    else:
        if not ut.get("cmd"):
            defects.append("unit_test.cmd 为空")
        if not isinstance(ut.get("timeout_sec"), (int, float)) or \
                ut.get("timeout_sec", 0) <= 0:
            defects.append("unit_test.timeout_sec 缺失或非法")
        if not ut.get("success_pattern"):
            defects.append("unit_test.success_pattern 为空")
        for f in ("fail_pattern", "smoke_cmd"):
            v = ut.get(f)
            if v is not None and not (isinstance(v, str) and v.strip()):
                defects.append(f"unit_test.{f} 须为非空字符串或 null")
    return defects


_SECTION_CHECKS = {"build": _check_build, "boot": _check_boot,
                   "inject": _check_inject, "unit_test": _check_unit_test}


def validate_runner(r: dict) -> list[str]:
    """完整 runner 契约（T5 门禁消费；行为与 v1 逐字兼容）。"""
    defects = []
    for section in ("build", "boot", "inject_device"):
        if not isinstance(r.get(section), dict):
            defects.append(f"缺 {section} 节")
            r[section] = {}
    defects += _check_build(r["build"])
    defects += _check_boot(r["boot"])
    defects += _check_inject(r["inject_device"])
    return defects


# ---------- 状态 / 路径 ----------

def _out_dir(p0: Path) -> Path:
    d = p0 / "reports" / "out"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_state(p0: Path) -> dict:
    p = p0.joinpath(*STATE_PATH)
    if p.exists():
        try:
            state = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(state.get("caps"), dict):
                state.setdefault("version", 2)
                return state
        except (OSError, json.JSONDecodeError):
            pass
    return {"version": 2, "caps": {c: _new_cap_state() for c in CAPS}}


def _new_cap_state() -> dict:
    return {"status": "pending", "session_id": None, "rounds_used": 0,
            "agent_used_sec": 0.0, "wall_used_sec": 0.0,
            "human_rounds": 0, "last_summary": None,
            "verify_result": None, "converged_at": None}


def _save_state(p0: Path, state: dict) -> None:
    p0.joinpath(*STATE_PATH).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------- prompt ----------

def _hints_text(ws: Path, cap: str) -> str:
    """三级输入之①：用户提示（--hints-dir 拷入 P0/inputs/hints/，可选）。"""
    p = ws / "P0" / "inputs" / "hints" / f"{cap}.md"
    try:
        if p.exists():
            return p.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def _capability_prompt(skill: str, cap: str, target_os: Path,
                       materials: list[Path], categories: list[str],
                       hints: str, prior_caps: list[str],
                       resume_ctx: dict | None, out: Path,
                       budget_line: str) -> str:
    frag_json, frag_md = out / f"{cap}.json", out / f"{cap}.md"
    test_req = out / f"{cap}_test.json"
    mat_lines = "\n".join(f"  - {m.resolve()}" for m in materials) or \
        "  （无——仅凭源码树）"
    lines = [f"{skill}", "", "---", "",
             f"## 任务数据（本次 session：{CAP_TITLE[cap]}）", "",
             "- 你只负责本能力的探明与产出；完成后编排器会验证并锁定，",
             "  后续能力在别的 session 接力。", "",
             "输入（权重降序，冲突时高权重优先，但与真实探测结果矛盾时",
             "以探测为准并在「不确定项」记下矛盾）：",
             "① 用户提示（开发者经验，最高优先级）：",
             hints.strip() or "  （无）",
             "② 开发资料（自己去读，文件或目录均可）：",
             mat_lines,
             f"③ 目标 OS 源码树：`{target_os.resolve()}`（树内 README/"
             "构建文件/CI 同样是资料——树可能很大，尽量在前两层解决）"]
    if categories:
        lines.append(f"- 设备类别标签：{categories}（example_args 至少覆盖"
                     "目标类别；网络类设备**必须**同时给键 `net-user`，"
                     "值 = 一个带用户态网络后端的设备参数实例——后续"
                     "端到端验证消费该键）")
    if prior_caps:
        lines += ["- 前序能力已收敛锁定（编排器已验证；**禁止重跑这些"
                  "命令**，按只读材料参考其形态与措辞）："]
        lines += [f"  - {CAP_TITLE[c]}：`{out / (c + '.json')}` 与 "
                  f"`{out / (c + '.md')}`" for c in prior_caps]
    lines += ["", "## 交互协议（文件即信号；消息正文永远只回一行"
             "「已写入 <路径>」）", "",
             f"- 想验证命令：把 `{json.dumps({'cmd': '<一行完整命令>', 'timeout_sec': 600}, ensure_ascii=False)}`"
             f" 形态的裸 JSON 写入 `{test_req}`（timeout_sec 可选）。"
             "编排器执行后完整结果落文件并以指针发回，你须自行读取。",
             f"- 自认完成：把本能力的节 JSON（与最终 runner.json 该节"
             f"同构）写入 `{frag_json}`（裸 JSON），调用手册节写入 "
             f"`{frag_md}`，然后只回「已写入」。完成前提 = 产出通过"
             "编排器校验与真实探测（探测失败会带证据打回）。",
             f"- `{frag_md}` 必须包含以下小节标题（逐字；缺一不收敛；"
             "内容须来自你本轮的真实探索，给 file:line 证据）："]
    lines += [f"  - {a}" for a in _required_anchors(cap)]
    if resume_ctx:
        lines += ["", "## 人工介入后的续跑", "",
                  f"- 上一轮（人工介入前）的总结：`{resume_ctx['summary']}`"
                  "（自行读取，含已完成/未完成/问题清单）",
                  "- 开发者的书面回答（**最高优先级输入**；与探测矛盾时"
                  "以探测为准并记录）：", "",
                  resume_ctx["answers"].strip(), "",
                  f"本次为续跑，资源有限（{RESUME_ROUNDS} 轮），优先利用"
                  "回答收敛。"]
    lines += ["", budget_line, "",
              "按 SKILL 方法开始。"]
    return "\n".join(lines)


def _budget_line(rounds: int, rounds_limit: int, agent_left: float,
                 wall_left: float) -> str:
    return (f"（资源余量：测试/终验轮 {rounds}/{rounds_limit}，"
            f"agent 时间 ~{int(agent_left)}s，墙钟 ~{int(wall_left)}s）")


# ---------- 静态执行 ----------

def _exec_test_request(p0: Path, cap: str, req: dict, seq: int,
                       target_os: Path) -> tuple[dict, float]:
    """执行测试请求（agent 预算外）。完整结果落文件，返回摘要。"""
    cmd = req["cmd"]
    timeout = int(req.get("timeout_sec") or TEST_TIMEOUT_DEFAULT[cap])
    log_path = p0 / "logs" / f"T3_{cap}_test_r{seq}.log"
    t0 = time.time()
    rc, _out = probe_mod._run(cmd, cwd=target_os,
                              env=probe_mod._base_env(target_os, {}),
                              timeout_sec=timeout, log_path=log_path)
    elapsed = time.time() - t0
    result = {"cmd": cmd, "rc": rc, "elapsed_sec": round(elapsed, 1),
              "timeout_sec": timeout, "log": str(log_path.resolve())}
    (p0 / "logs" / f"T3_{cap}_test_r{seq}.result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result, elapsed


_DEVICE_RE = re.compile(r"-device\s+([A-Za-z0-9_.-]+)")
_NET_MODEL_RE = re.compile(r"model=([A-Za-z0-9_.-]+)")


def _injection_evidence_check(inj: dict, log_injected: str,
                              log_bare: str) -> tuple[bool, str]:
    """注入生效判据——差分语义（e2e 轮 3-6 实录四连进化的定稿）。

    证据 = 注入 boot 与裸 boot 的**判定日志对照**（各自跑一次真实
    boot 后读 boot.log_file；由 _final_verify 编排）：
    - driver_success_pattern 非空：须 注入轮命中 ∧ 裸轮未命中
      （裸轮也命中 = 特征与设备无关——轮 6 实录：锚在默认网卡行）；
    - example_args 设备名 token（-device x / model=x）：须存在
      "注入轮命中 ∧ 裸轮未命中"的差分 token（单侧命中/双侧命中都
      不算——轮 4/5 实录：stdout 回显与语法变体可伪造单侧证据）；
    - 两者皆无 → 无可判定证据，打回（要求给出驱动特征或含设备名
      的参数形态）。
    已知残余攻击面：宿主 shell 向日志文件追加伪造行——结构性解法
    是 TODO #12（命令侧自包含注入），本判据为其前的验证层防线。
    """
    tokens: set[str] = set()
    for v in (inj.get("example_args") or {}).values():
        s = str(v)
        tokens.update(_DEVICE_RE.findall(s))
        tokens.update(_NET_MODEL_RE.findall(s))
    pat = inj.get("driver_success_pattern")
    if not pat and not tokens:
        return False, ("无可判定证据：driver_success_pattern 为空且 "
                       "example_args 无设备名（-device x / model=x 形态）"
                       "——给出设备在场才有的驱动特征行，或参数中带"
                       "设备名")
    if pat:
        if pat in log_injected and pat not in log_bare:
            return True, f"驱动特征差分命中（{pat!r}）"
        if pat in log_bare:
            return False, (f"驱动特征 {pat!r} 在裸 boot（无注入）也"
                           "命中——特征与注入无关，不能证明设备在场；"
                           "改选设备 probe/初始化才有的行")
        return False, (f"驱动特征 {pat!r} 注入轮未命中——设备未出现"
                       "或特征错误")
    diff = sorted(t for t in tokens
                  if t in log_injected and t not in log_bare)
    if diff:
        return True, f"设备名差分命中（{', '.join(diff)}）"
    both = sorted(t for t in tokens
                  if t in log_injected and t in log_bare)
    detail = "设备名无差分命中（注入轮与裸 boot 无设备相关差异）"
    if both:
        detail += f"；{', '.join(both)} 两轮都命中（与注入无关）"
    return False, detail + "——注入疑似静默无效（启动命令链可能不消费" \
                           "注入载体；命令自身回显不算证据）"


def _final_verify(ws: Path, p0: Path, cap: str, section: dict,
                  categories: list[str], seq: int,
                  target_os: Path) -> tuple[dict, float]:
    """终验：只跑本能力的 probe（不重验前序——定案）。

    返回 (result, wall_elapsed)；result = {"item", "ok", "detail"}
    （item 归一为 T3_development.json 消费名）。质量契约已在片段
    校验阶段把关，这里只剩真实执行。
    """
    label = f"T3_{cap}_verify_r{seq}"
    t0 = time.time()
    if cap == "build":
        r = probe_mod.probe_build(p0, target_os, {"build": section},
                                  label=label)
        r = {"item": "build", "ok": r["ok"], "detail": r["detail"]}
    elif cap == "boot":
        r = probe_mod.probe_boot(p0, target_os, {"boot": section},
                                 label=label)
        r = {"item": "boot", "ok": r["ok"], "detail": r["detail"]}
    elif cap == "inject":
        boot = _converged_section(p0, "boot")
        runner_stub = {"boot": boot, "inject_device": section}
        r = probe_mod.probe_boot_with_device(
            p0, target_os, runner_stub, categories,
            label=label, check_driver=True)
        r = {"item": "boot_with_device", "ok": r["ok"],
             "detail": r["detail"]}
        # 差分证据：注入轮日志（此刻 qemu.log=注入 boot 的最后写入）
        lf = boot.get("log_file")
        if not boot.get("log_is_stdout") and lf:
            lp = Path(lf) if Path(lf).is_absolute() else target_os / lf
            log_inj = _read_text(lp) or ""
            (p0 / "logs" / f"{label}.bootlog.injected").write_text(
                log_inj, encoding="utf-8")
            bare = probe_mod.probe_boot(p0, target_os, {"boot": boot},
                                        label=f"{label}_bare")
            log_bare = _read_text(lp) or ""
            (p0 / "logs" / f"{label}.bootlog.bare").write_text(
                log_bare, encoding="utf-8")
            if not bare.get("ok"):
                r["ok"] = False
                r["detail"] += " bare-boot=FAIL（裸 boot 基线异常）"
            else:
                ok4, note = _injection_evidence_check(
                    section, log_inj, log_bare)
                r["detail"] += (f" injection-evidence={'PASS' if ok4 else 'FAIL'}"
                                + (f"（{note}）" if note else ""))
                if not ok4:
                    r["ok"] = False
        else:
            r["detail"] += (" injection-evidence=skip"
                            "（stdout 模式无独立判定日志——差分判据"
                            "不适用，依赖驱动特征单轮判定与人工审阅）")
    else:                                   # unit_test
        if section.get("mechanism") == "none":
            r = {"item": "unit_test", "ok": True,
                 "detail": "mechanism=none（目标 OS 无内核单测机制，"
                           "显式结论，不探测）"}
        else:
            from ..loop.ut_verify import run_and_verify
            cmd = section.get("smoke_cmd") or section["cmd"]
            ok, detail, _o = run_and_verify(
                cmd, cwd=target_os,
                env=probe_mod._base_env(target_os, {}),
                timeout_sec=int(section.get("timeout_sec", 1800)),
                log_path=p0 / "logs" / f"{label}.log",
                success_pattern=section.get("success_pattern"),
                fail_pattern=section.get("fail_pattern"))
            r = {"item": "unit_test", "ok": bool(ok), "detail": detail}
    # 裸 boot 判定日志副本（boot 能力终验留档；inject 的双份在其分支内）
    if cap == "boot":
        bo = section
        lf = bo.get("log_file")
        if lf and not bo.get("log_is_stdout"):
            src = Path(lf) if Path(lf).is_absolute() else target_os / lf
            content = _read_text(src)
            if content is not None:
                (p0 / "logs" / f"{label}.bootlog").write_text(
                    content, encoding="utf-8")
    elapsed = time.time() - t0
    ev_path = p0 / "logs" / f"{label}.evidence.json"
    ev_path.write_text(json.dumps(r, ensure_ascii=False, indent=2),
                       encoding="utf-8")
    return r, elapsed


def _converged_section(p0: Path, cap: str) -> dict:
    section, err = _read_section(_out_dir(p0) / f"{cap}.json", cap)
    if section is None:
        raise RuntimeError(f"T3: 前序能力 {cap} 片段不可读（{err}）")
    return section


# ---------- 片段质量校验（机器可静态判定的缺陷 = 质量，不烧轮） ----------

def _read_section(frag_json: Path, cap: str) -> tuple[dict | None, str]:
    """读节片段；容 {SECTION_KEY: {...}} 包装。返回 (section, 失败原因)。"""
    try:
        raw = frag_json.read_text(encoding="utf-8")
    except OSError:
        return None, "片段文件不存在"
    return _parse_section(raw, cap)


def _parse_section(raw: str, cap: str) -> tuple[dict | None, str]:
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as ex:
        return None, f"JSON 解析失败: {ex}"
    if isinstance(obj, dict) and isinstance(obj.get(SECTION_KEY[cap]), dict) \
            and set(obj.keys()) == {SECTION_KEY[cap]}:
        obj = obj[SECTION_KEY[cap]]
    if not isinstance(obj, dict):
        return None, "顶层不是 JSON 对象"
    return obj, ""


def _anchor_body_empty(md_text: str, anchor: str) -> bool:
    """锚点小节标题在场但正文为空（e2e 轮 3 实录：标题凑数内容空）。"""
    lines = md_text.splitlines()
    idx = next((i for i, ln in enumerate(lines)
                if ln.strip() == anchor), None)
    if idx is None:
        return True
    for ln in lines[idx + 1:]:
        if ln.startswith("#"):
            break
        if ln.strip():
            return False
    return True


def _frag_quality_defects(cap: str, section: dict, md_text: str) -> list[str]:
    defects = list(_SECTION_CHECKS[cap](section))
    if not md_text.strip():
        defects.append("调用手册片段（.md）缺失或为空")
        return defects
    for a in _required_anchors(cap):
        if a not in md_text:
            defects.append(f"调用手册缺小节「{a}」")
        elif _anchor_body_empty(md_text, a):
            defects.append(f"小节「{a}」内容为空（必答项须实答，"
                           "给 file:line 证据或实测结果）")
    return defects


# ---------- 消息块 ----------

def _read_text(path: Path) -> "str | None":
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _pointer_message(title: str, body_lines: list[str],
                     budget: str) -> str:
    return ("---\n\n" + f"## {title}\n\n"
            + "\n".join(body_lines)
            + "\n\n请自行读取文件定位问题后继续（写下一个测试请求或"
              "最终片段）。" + f"\n{budget}")


def _exhaustion_summary_fallback(p0: Path, cap: str, st: dict,
                                 rounds_limit: int, agent_budget: float,
                                 wall_budget: float,
                                 summary_path: Path) -> None:
    """agent 总结不可得时的编排器拼装兜底（观测缺口兜底，必须产出文件）。"""
    out = _out_dir(p0)
    logs = sorted((p0 / "logs").glob(f"T3_{cap}_*"),
                  key=lambda p_: p_.stat().st_mtime)[-8:]
    frag = "在" if (out / f"{cap}.json").exists() else "不在场"
    lines = [f"# {CAP_TITLE[cap]} session 资源耗尽（编排器自动总结——"
             "agent 总结不可得）", "",
             f"- 资源：轮 {st.get('rounds_used', 0)}/{rounds_limit}，"
             f"agent {st.get('agent_used_sec', 0):.0f}s/{agent_budget:.0f}s，"
             f"墙钟 {st.get('wall_used_sec', 0):.0f}s/{wall_budget:.0f}s",
             f"- 最终片段：{frag}（`P0/reports/out/{cap}.json`）",
             "", "## 最近证据文件", ""]
    lines += [f"- {lp.name}" for lp in logs] or ["- （无）"]
    lines += ["", "## 给开发者的问题（自动生成）", "",
              "Q1: 自动提取未收敛——请人工提供本能力的正确命令/判据或",
              "    关键提示（环境前置、日志落点、注入钩子位置等），",
              "    或指出应换的方向。"]
    summary_path.write_text("\n".join(lines), encoding="utf-8")


# ---------- 单能力 session 循环 ----------

def _run_cap_session(ws: Path, p0: Path, cap: str, target_os: Path,
                     materials: list[Path], categories: list[str],
                     state: dict, resume_ctx: dict | None) -> str:
    """跑一个能力的 session。返回 "converged" | "exhausted"。

    质量失败（无有效产出/契约缺陷）→ 同会话微增量 ≤QUALITY_TRIES 次
    → RuntimeError；session 不可得 → RuntimeError（P2b 惯例：程序
    错误类不进人工关口）。
    """
    skill = agent.load_skill("P0-env-extract")
    out = _out_dir(p0)
    st = state["caps"][cap]
    frag_json, frag_md = out / f"{cap}.json", out / f"{cap}.md"
    test_req = out / f"{cap}_test.json"
    summary_path = out / f"{cap}_summary.md"
    rounds_limit = RESUME_ROUNDS if resume_ctx else MAX_ROUNDS
    agent_budget = RESUME_AGENT_SEC if resume_ctx else AGENT_BUDGET_SEC[cap]
    wall_budget = RESUME_WALL_SEC if resume_ctx else WALL_BUDGET_SEC[cap]
    prior_caps = [c for c in CAPS[:CAPS.index(cap)]
                  if state["caps"][c]["status"] == "converged"]
    if not resume_ctx:                      # 防脏读（scaffold 教训）
        for pth in (test_req, frag_json, frag_md):
            pth.unlink(missing_ok=True)
    session_id: str | None = None
    seg = 0
    rounds = 0
    agent_used = 0.0
    wall_used = 0.0
    quality_fails = 0
    seen = {"test": "", "frag": ""}
    stem_base = str(p0 / "logs" / f"T3_{cap}")
    bl = _budget_line(rounds, rounds_limit, agent_budget, wall_budget)
    message = _capability_prompt(skill, cap, target_os, materials,
                                 categories, _hints_text(ws, cap),
                                 prior_caps, resume_ctx, out, bl)

    def _sync_state(status: str) -> None:
        st.update({"status": status, "session_id": session_id,
                   "rounds_used": rounds,
                   "agent_used_sec": round(agent_used, 1),
                   "wall_used_sec": round(wall_used, 1)})
        _save_state(p0, state)

    def _consume_file(path: Path, key: str) -> tuple[str, str]:
        """(state, content)：absent/unchanged/present(+content)。"""
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return "absent", ""
        h = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if h == seen[key]:
            return "unchanged", content
        seen[key] = h
        return "present", content

    def _consume_pair(jpath: Path, mpath: Path, key: str) \
            -> tuple[str, str, str]:
        """片段对（json+md）组合指纹——只修 md 也算有效修订。"""
        jcontent = _read_text(jpath)
        if jcontent is None:
            return "absent", "", ""
        mcontent = _read_text(mpath) or ""
        h = hashlib.sha256((jcontent + "\x00" + mcontent)
                           .encode("utf-8")).hexdigest()
        if h == seen[key]:
            return "unchanged", jcontent, mcontent
        seen[key] = h
        return "present", jcontent, mcontent

    while True:
        if rounds >= rounds_limit or agent_used >= agent_budget \
                or wall_used >= wall_budget:
            break
        remaining = agent_budget - agent_used
        if remaining < 30:
            break
        seg += 1
        stem = f"{stem_base}_S{seg}"
        t0 = time.time()
        rc, out_txt = agent._opencode_json_runner(
            message, workdir=p0, log_stem=stem,
            timeout_sec=int(remaining) + 1, session_id=session_id,
            task={"phase": "P0", "step": f"t3-{cap}", "attempt": seg})
        elapsed = time.time() - t0
        agent_used += elapsed
        ev = agent._parse_events(out_txt)   # rc≠0 也抢救 session_id
        if ev and ev.get("session_id"):
            session_id = ev["session_id"]
        if session_id is None:
            _sync_state("running")
            raise RuntimeError(
                f"T3[{cap}]: session id not found (seg {seg} rc={rc}; "
                f"log={stem}.log)——opencode --format json 未产出 "
                "sessionID 事件；检查 opencode 版本与登录状态")
        _sync_state("running")
        bl = _budget_line(rounds, rounds_limit, agent_budget - agent_used,
                          wall_budget - wall_used)

        # ---- 1) 最终片段优先 ----
        fstate, jcontent, mcontent = _consume_pair(frag_json, frag_md, "frag")
        if fstate == "present":
            test_req.unlink(missing_ok=True)    # 清同段误写的测试请求
            section, err = _parse_section(jcontent, cap)
            defects: list[str] = []
            if section is None:
                defects = [f"片段不可用：{err}"]
                md_text = ""
            else:
                md_text = mcontent
                defects = _frag_quality_defects(cap, section, md_text)
            if defects:
                quality_fails += 1
                if quality_fails > QUALITY_TRIES:
                    raise RuntimeError(
                        f"T3[{cap}]: 片段质量问题经 {QUALITY_TRIES} 次同会话"
                        f"续接未修复（seg {seg}；详见 {stem}.log）"
                        "——会话疑似空转")
                message = _pointer_message(
                    "上一次片段的问题（修订后重写两个文件，内容须有变化）",
                    [f"- {d}" for d in defects[:12]], bl)
                continue
            quality_fails = 0
            # 终验（烧轮）
            rounds += 1
            _sync_state("running")
            result, wsec = _final_verify(ws, p0, cap, section,
                                         categories, rounds, target_os)
            wall_used += wsec
            _sync_state("running")
            if result["ok"]:
                st["verify_result"] = result
                _sync_state("converged")
                _log.console_line(f"[porter] T3: {cap} 节收敛 ✓"
                                  f"（{rounds} 轮，agent "
                                  f"{agent_used:.0f}s）")
                return "converged"
            bl = _budget_line(rounds, rounds_limit, agent_budget - agent_used,
                              wall_budget - wall_used)
            message = _pointer_message(
                f"终验 FAIL（第 {rounds} 轮）——修订片段",
                [f"- {result['item']}: FAIL（{result['detail']}）",
                 f"- 证据：{p0 / 'logs' / f'T3_{cap}_verify_r{rounds}.evidence.json'}"
                 "（探测日志见同目录同名 .log 与 probe 落盘日志）",
                 f"- 探测只验本能力；修好后重写 `{frag_json}` 与 "
                 f"`{frag_md}`（内容须有变化）"], bl)
            continue
        if fstate == "unchanged":
            quality_fails += 1
            if quality_fails > QUALITY_TRIES:
                raise RuntimeError(
                    f"T3[{cap}]: 片段连续原样重提交（seg {seg}）——"
                    "会话疑似空转")
            message = _pointer_message(
                "片段内容未变化",
                ["- 重提交的片段与上一份逐字相同——请实际修订后再写。"],
                bl)
            continue

        # ---- 2) 测试请求 ----
        tstate, tcontent = _consume_file(test_req, "test")
        if tstate == "present":
            try:
                req = json.loads(tcontent)
                assert isinstance(req.get("cmd"), str) and req["cmd"].strip()
                if "timeout_sec" in req and req["timeout_sec"] is not None:
                    v = req["timeout_sec"]
                    assert isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
                    req["timeout_sec"] = int(v)
                else:
                    req.pop("timeout_sec", None)
            except (json.JSONDecodeError, AssertionError, AttributeError):
                quality_fails += 1
                if quality_fails > QUALITY_TRIES:
                    raise RuntimeError(
                        f"T3[{cap}]: 测试请求格式问题持续（seg {seg}；"
                        f"详见 {stem}.log）——会话疑似空转")
                message = _pointer_message(
                    "测试请求格式问题",
                    ['- 须为裸 JSON：{"cmd": "<一行完整命令>", '
                     '"timeout_sec": <可选正整数>}'], bl)
                continue
            quality_fails = 0
            rounds += 1
            _sync_state("running")
            result, wsec = _exec_test_request(p0, cap, req, rounds,
                                              target_os)
            wall_used += wsec
            _sync_state("running")
            test_req.unlink(missing_ok=True)    # 消费即清（防旧请求重放）
            _log.console_line(f"[porter] T3[{cap}]: 测试 r{rounds} "
                              f"rc={result['rc']} "
                              f"{result['elapsed_sec']:.0f}s")
            bl = _budget_line(rounds, rounds_limit, agent_budget - agent_used,
                              wall_budget - wall_used)
            message = _pointer_message(
                f"测试请求已执行（第 {rounds} 次）",
                [f"- 命令：{result['cmd'][:200]}",
                 f"- rc={result['rc']}，耗时 {result['elapsed_sec']}s",
                 f"- 完整输出：{result['log']}",
                 f"- 结果摘要：{p0 / 'logs' / f'T3_{cap}_test_r{rounds}.result.json'}"],
                bl)
            continue
        if tstate == "unchanged":
            quality_fails += 1
            if quality_fails > QUALITY_TRIES:
                raise RuntimeError(
                    f"T3[{cap}]: 测试请求连续原样重提交（seg {seg}）——"
                    "会话疑似空转")
            message = _pointer_message(
                "测试请求内容未变化",
                ["- 与上一份逐字相同；换命令或去掉此请求、直接产出片段。"],
                bl)
            continue

        # ---- 3) 无有效产出 ----
        quality_fails += 1
        if quality_fails > QUALITY_TRIES:
            raise RuntimeError(
                f"T3[{cap}] 连续 {QUALITY_TRIES} 段无有效产出文件"
                f"（seg {seg}；详见 {stem}.log）——会话疑似空转")
        message = _pointer_message(
            "上一段的问题",
            ["- 未产出任何有效文件。写测试请求（探索）或最终片段"
             "（自认完成）二者其一；消息正文只回「已写入 <路径>」。"],
            bl)

    # ---- 耗尽 → 总结 → 关口（人工环 A 类） ----
    _exhaust_with_summary(ws, p0, cap, st, state, session_id, stem_base,
                          rounds, rounds_limit, agent_used, agent_budget,
                          wall_used, wall_budget, summary_path)
    return "exhausted"


def _exhaust_with_summary(ws: Path, p0: Path, cap: str, st: dict,
                          state: dict, session_id: str | None,
                          stem_base: str, rounds: int, rounds_limit: int,
                          agent_used: float, agent_budget: float,
                          wall_used: float, wall_budget: float,
                          summary_path: Path) -> None:
    summary_path.unlink(missing_ok=True)
    wrote = False
    if session_id:
        try:
            agent._opencode_json_runner(
                "---\n\n## 资源已耗尽——请写总结后结束\n\n"
                f"把总结写入 `{summary_path}`（Markdown），三部分：\n"
                "1. 已完成的事（已确认的事实、试过的命令及结果）\n"
                "2. 未完成的事（还缺什么）\n"
                "3. 给开发者的问题（逐题编号：为什么难、已试过什么、"
                "附证据文件指针）\n\n"
                "写完消息只回一行「已写入 <路径>」。这是人工介入的"
                "唯一材料，问题质量直接决定能否被解决。",
                workdir=p0, log_stem=f"{stem_base}_SUMMARY",
                timeout_sec=SUMMARY_BUDGET_SEC, session_id=session_id,
                task={"phase": "P0", "step": f"t3-{cap}", "attempt": -1})
            wrote = summary_path.exists()
        except Exception:
            wrote = False
    if not wrote:                           # 兜底：编排器拼装
        _exhaustion_summary_fallback(p0, cap, st, rounds_limit,
                                     agent_budget, wall_budget,
                                     summary_path)

    from ..loop import gates as gates_mod
    gate_id = f"p0.t3.{cap}"
    gates_mod.panic(ws, {
        "id": gate_id, "kind": "fact", "gate_type": "decision",
        "phase": "P0",
        "question": (
            f"{CAP_TITLE[cap]} session 资源耗尽未收敛。agent 总结见 "
            f"P0/reports/out/{cap}_summary.md（已完成/未完成/问题清单），"
            "请逐题作答；答案将作为最高优先级输入注入该能力的续跑"
            "（小额资源）。"),
        "context_files": [f"P0/reports/out/{cap}_summary.md",
                          "P0/reports/t3_state.json"],
        "answer_form": [
            {"field": "answers", "type": "text", "required": True,
             "hint": "逐题编号作答（自由文本，全文注入续跑 session）"}],
    })
    # 关口已答过（上次人工轮的旧答案）→ 重开：新答案才能走 fresh 路径
    ledger = gates_mod.GateLedger(ws).load()
    g = ledger.find(gate_id)
    if g and g.get("status") in ("answered", "applied", "invalid"):
        g["status"] = "open"
        g["answer"] = None
        g["answered_by"] = None
        g["answered_at"] = None
        g["resolution"] = None
        g["history"].append({"time": _now_iso(), "event": "re-asked-reset",
                             "detail": "再次耗尽，重开等待新一轮人工答案"})
        ledger.save()
    st["status"] = "exhausted"
    st["last_summary"] = str(summary_path)
    _save_state(p0, state)
    try:
        from ..loop import events as _ev
        _ev.append_event("t3_exhausted", subject=gate_id,
                         summary=f"{cap} 资源耗尽（轮 {rounds}/{rounds_limit}，"
                                 f"agent {agent_used:.0f}s，墙钟 "
                                 f"{wall_used:.0f}s）")
    except Exception:
        pass


def _gate_answer(ws: Path, cap: str) -> str:
    """账本里该能力关口的最新人工答案（无 → ""）。"""
    try:
        from ..loop import gates as gates_mod
        ledger = gates_mod.GateLedger(ws).load()
        g = ledger.find(f"p0.t3.{cap}")
        if g and g.get("answer"):
            return str((g["answer"].get("answers") or "")).strip()
    except Exception:
        pass
    return ""


# ---------- 收官：拼装 + 冻结 ----------

def _extract_uncertain(md_text: str) -> list[str]:
    """抽「### 不确定项」小节的条目行（memo.md 素材）。"""
    out: list[str] = []
    inside = False
    for ln in md_text.splitlines():
        if ln.startswith("### "):
            inside = ln.strip() == "### 不确定项"
            continue
        if inside and ln.strip().startswith(("- ", "* ")):
            out.append(ln.strip().lstrip("-* ").strip())
    return out


def _assemble_and_freeze(ws: Path, p0: Path, state: dict,
                         target_os: Path) -> None:
    out = _out_dir(p0)
    sections: dict = {}
    md_parts: list[str] = []
    uncertain: list[str] = []
    for cap in CAPS:
        section, err = _read_section(out / f"{cap}.json", cap)
        if section is None:                 # 收敛后的片段必然在场
            raise RuntimeError(f"T3 收官：{cap} 片段不可用（{err}）")
        sections[SECTION_KEY[cap]] = section
        md = (out / f"{cap}.md").read_text(encoding="utf-8").strip()
        md_parts.append(md)
        uncertain += _extract_uncertain(md)
    runner = {**sections, "meta": {"generated_by": "porter/P0-env-extract",
                                   "reviewed": False}}
    runner_json = json.dumps(runner, ensure_ascii=False, indent=2)
    (ws / "runner.json").write_text(runner_json, encoding="utf-8")

    header = ["# runner 调用手册（T3 产出，冻结）", "",
              f"- 目标 OS 源码树：`{target_os.resolve()}`",
              f"- 冻结时间：{_now_iso()}（指纹记于 project.json"
              " [\"t3_frozen\"]；修改即关口报错）",
              "- 调用手册是各节 JSON 的扩充——每条命令/特征/参数在正文"
              "有来龙去脉，不得有正文不解释的字段（软性写作指引）", "",
              "## 第一部分：构建/编译", "", "### 1. 模块编译", "",
              "### 2. 镜像编译", "", "## 第二部分：启动", "",
              "### 1. 设备自启动", "", "### 2. 设备注入与交互", "",
              "## 第三部分：单元测试", ""]
    (ws / "runner.md").write_text(
        "\n".join(header) + "\n\n---\n\n".join(md_parts) + "\n",
        encoding="utf-8")

    # T3_development.json（T5 门禁消费；三行结论来自各节终验）
    dev_results = [state["caps"]["build"]["verify_result"],
                   state["caps"]["boot"]["verify_result"],
                   state["caps"]["inject"]["verify_result"]]
    for r in dev_results:
        if not r:
            raise RuntimeError("T3 收官：verify_result 缺失（状态损坏）")
    (p0 / "reports").mkdir(exist_ok=True)
    (p0 / "reports" / "T3_development.json").write_text(
        json.dumps({"kind": "development", "results": dev_results,
                    "hard_gate_pass": True}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    # memo.md（CP0 引用；不确定项 = 非阻塞确认，H20 备忘语义）
    lines = ["# 非阻塞确认项（探测已全绿，仅备忘——详见 runner.md 各节"
             "「不确定项」）", ""]
    lines += [f"- {u}" for u in uncertain] or ["- （无）"]
    (p0 / "reports" / "memo.md").write_text("\n".join(lines),
                                            encoding="utf-8")
    if uncertain:
        try:
            from ..loop import events as _ev
            _ev.append_event("memo", subject="p0.t3.uncertain",
                             summary=f"{len(uncertain)} 项非阻塞不确定项"
                                     "（P0/reports/memo.md）")
        except Exception:
            pass

    # 冻结指纹
    proj_path = ws / "project.json"
    proj = json.loads(proj_path.read_text(encoding="utf-8"))
    proj["t3_frozen"] = {
        "runner_sha256": _sha256_file(ws / "runner.json"),
        "runner_md_sha256": _sha256_file(ws / "runner.md"),
        "frozen_at": _now_iso()}
    proj_path.write_text(json.dumps(proj, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    _log.console_line("[porter] T3: runner.json + runner.md 就绪并冻结"
                      "（reviewed=false）+ 四节终验全绿")


# ---------- 主入口 ----------

def extract_env(ws: Path, target_os: Path, materials: list[Path],
                categories: list[str]) -> int:
    """返回 0=成功（runner.json/runner.md 已冻结）；3=需人工（关口已开）。"""
    try:                                # 观测埋桩（H12）：P0 相位
        from ..loop import events as _ev
        _ev.bind(ws, "p0")
    except Exception:
        pass
    p0 = ws / "P0"
    (p0 / "logs").mkdir(parents=True, exist_ok=True)
    (p0 / "reports").mkdir(exist_ok=True)
    _out_dir(p0)
    runner_path = ws / "runner.json"
    if runner_path.exists():
        _log.console_line(f"[porter] T3: 复用 {runner_path}")
        return 0

    state = _load_state(p0)
    for cap in CAPS:
        st = state["caps"].setdefault(cap, _new_cap_state())
        if st["status"] == "converged":
            continue
        resume_ctx = None
        if st["status"] == "exhausted":
            answers = _gate_answer(ws, cap)
            if not answers:
                _log.console_line(
                    f"[porter] T3: {cap} 等待人工（p0.t3.{cap}）——总结见 "
                    f"P0/reports/out/{cap}_summary.md（exit 3）")
                return 3
            summary = st.get("last_summary") or \
                f"P0/reports/out/{cap}_summary.md"
            resume_ctx = {"summary": summary, "answers": answers}
            st["human_rounds"] = int(st.get("human_rounds", 0)) + 1
            _log.console_line(f"[porter] T3: {cap} 人工答案在场——续跑"
                              f"（第 {st['human_rounds']} 轮人工介入后，"
                              f"小额资源）")
        st["status"] = "running"
        _save_state(p0, state)
        _log.console_line(f"[porter] T3: {CAP_TITLE[cap]} session 开始"
                          + ("（续跑）" if resume_ctx else ""))
        outcome = _run_cap_session(ws, p0, cap, target_os, materials,
                                   categories, state, resume_ctx)
        if outcome == "exhausted":
            _log.console_line(f"[porter] T3: {cap} session 资源耗尽 → "
                              f"人工关口 p0.t3.{cap}（exit 3）")
            return 3
        # converged（状态与片段已由 session 内落盘）
    _assemble_and_freeze(ws, p0, state, target_os)
    return 0
