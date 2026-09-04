"""agent.py — opencode 非交互调用的最小封装。

设计约定（来自 driver-migration 实验的既定原则）：
- agent 只承担"判断性"动作（类别识别/文档翻译/检索），确定性动作一律走脚本
- 模型经 PORTER_MODEL 环境变量配置，默认 zhipu-ai/glm-5.2
- 每次调用的完整输出落盘日志，供人工审核与成本归因
- SKILL 文件是行为指令的唯一来源：调用方组装 prompt = SKILL 正文 + 任务数据
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

DEFAULT_MODEL = "zhipu-ai/glm-5.2"
TOOL_ROOT = Path(__file__).resolve().parent.parent.parent
SKILLS_DIR = TOOL_ROOT / "skills"

# opencode 将每步 max_output_tokens 静默钳到 min(model.limit.output, 32000)
# （anomalyco/opencode#29363）。推理模型的思考与输出共享该预算，大调用
# 思考烧满 32K → reason:"length"、output:0 零产出（P1-divide R1/R2 实测）。
# 逃生门 = OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX。glm-5.2 官方上限 128K
# （=131072，docs.bigmodel.cn GLM-5.2 模型卡，与 models.dev 注册表一致；
# context 1M 下无 overflow 压缩副作用）。
OUTPUT_TOKEN_MAX_DEFAULT = "131072"


def load_skill(name: str) -> str:
    """读取 skills/ 下的 SKILL 文件正文。"""
    path = SKILLS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"SKILL not found: {path}")
    return path.read_text(encoding="utf-8")


def _bound_ws() -> "Path | None":
    """log 子系统绑定的工作区（未绑定 → None；观测面永不打断）。"""
    try:
        from ..log import store as _store
        b = _store.bound()
        if b and b.get("ws"):
            return Path(b["ws"])
    except Exception:
        pass
    return None


def run_agent(prompt: str, workdir: Path, log_stem: str,
              model: str | None = None, timeout_sec: int = 600,
              task: dict | None = None) -> tuple[int, str]:
    """调用 opencode 非交互模式执行一次 agent 任务。

    返回 (exit_code, stdout_text)。完整输出落盘 <log_stem>.log；输入
    原文归档 <log_stem>.prompt.md（与输出成对，docs/sub-systems/log.md 类 2）。
    观测埋桩（log 子系统）：events 绑定在场时前后写意图/结果事件，
    run_id = log_stem；task 传 {phase,module,step,attempt} 元数据
    （v1.1 结构字段）。vcs 隔离：调用前后各一个工作区 commit
    （pre-agent/agent 成对），diff 即该次调用的 ws 侧产物。
    """
    vws = _bound_ws()                    # vcs 隔离点的工作区（可能 None）
    if vws is not None:
        try:
            from . import vcs as _vcs
            _vcs.agent_pre(vws, str(log_stem))
        except Exception:
            pass
    model = model or os.environ.get("PORTER_MODEL", DEFAULT_MODEL)
    log_path = Path(f"{log_stem}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path = Path(f"{log_stem}.prompt.md")
    try:                                    # 输入归档（观测面永不打断）
        prompt_path.write_text(prompt, encoding="utf-8")
    except OSError:
        pass
    stem = str(log_stem)
    ref = {"log": str(log_path), "prompt": str(prompt_path)}
    tmeta = {k: (task or {}).get(k) for k in
             ("phase", "module", "step", "attempt")}
    try:
        from ..log import core as _log
        _log.record("agent_start", intent=stem, cmd=prompt,
                    summary=f"model={model}",
                    console_msg=f"[porter] agent: {log_stem} "
                                f"(model={model})",
                    run_id=stem, ref=ref, **tmeta)
    except Exception:
        pass
    args = [
        "opencode", "run", "--auto",
        "--model", model,
        "--dir", str(workdir),
        prompt,
    ]
    t0 = time.time()
    env = {**os.environ, "NO_COLOR": "1"}
    env.setdefault("OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX",
                   OUTPUT_TOKEN_MAX_DEFAULT)
    try:
        proc = subprocess.run(
            args, cwd=str(workdir), capture_output=True, text=True,
            timeout=timeout_sec, env=env,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        out = "TIMEOUT"
        rc = -1
    except FileNotFoundError:
        out = "opencode executable not found in PATH"
        rc = 127
    log_path.write_text(out, encoding="utf-8")
    elapsed = time.time() - t0
    try:
        from ..log import core as _log
        _log.record("agent_end", intent=stem, rc=rc,
                    summary=(out or "")[-300:].strip()
                    .replace("\n", " ⏎ "),
                    console_msg=f"[porter] agent: {log_stem} rc={rc} "
                                f"{elapsed:.0f}s log={log_path}",
                    run_id=stem, **tmeta)
    except Exception:
        pass
    try:                                # vcs：agent 调用后的隔离点（best-effort）
        if vws is not None:
            from . import vcs as _vcs
            _vcs.agent_post(vws, str(log_stem), rc)
    except Exception:
        pass
    return rc, out


def extract_json(out: str) -> dict | None:
    """从 agent 输出中提取 moves JSON 并解析。

    优先取 ```json 代码块（SKILL 约定）；兜底容忍裸 JSON：以最后一个
    "moves" 为锚点做配平括号搜索（opencode run 的输出混有工具转录，
    不能全文贪婪匹配）。失败返回 None。
    """
    blocks = re.findall(r"```json\s*(.*?)```", out, re.DOTALL)
    if not blocks:
        blocks = re.findall(r"```\s*(\{.*?\})\s*```", out, re.DOTALL)
    for block in blocks:
        try:
            obj = json.loads(block.strip())
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    # 兜底：裸 JSON（无围栏）。从 "moves" 锚点向前找配平的 { ... }
    anchor = out.rfind('"moves"')
    while anchor >= 0:
        start = out.rfind("{", 0, anchor)
        while start >= 0:
            depth = 0
            end = -1
            for k in range(start, len(out)):
                if out[k] == "{":
                    depth += 1
                elif out[k] == "}":
                    depth -= 1
                    if depth == 0:
                        end = k
                        break
            if end > 0:
                try:
                    obj = json.loads(out[start:end + 1])
                    if isinstance(obj, dict) and "moves" in obj:
                        return obj
                except json.JSONDecodeError:
                    pass
            start = out.rfind("{", 0, start)
        anchor = out.rfind('"moves"', 0, anchor)
    return None


# =====================================================================
# run_agent_seq —— split_long_op：长操作拆分的非交互 agent 调用（2026-09-04）
#
# 动机：一次 agent 调用里若自己跑长操作（编译 10-30 分钟/测试/启动），
# 操作时间会吃掉 agent 的 timeout 预算。拆分 = agent 段 × N + 中间静态段：
#   agent 段（思考/改码，受总时间预算约束）
#     → 请求 run_static → 静态段在外部执行（独立时长，不吃 agent 预算）
#     → 结果发回 → 同一会话继续 → … → done
#
# 段间接续（无信息损失，目标 = "像同一次非交互 agent 跑的一样"）：
#   主路径  opencode run --session <id>（原生会话续接；1.18.27 实测：
#           非交互下可用，跨段记忆携带经暗号回溯验证）
#   兜底    解析不到 session id 时，退化为"模仿交互式对话轮次"的
#           prompt 注入（任务原文 + 用户/助手交替轮次 + 新消息）
#
# 防打转（不设轮数上限，两道针对性护栏）：
#   ① 连续 SEQ_SAME_SIG_REPEAT 次静态段失败结果规范化签名相同
#     （同一个错误修不动）→ stalled 早退
#   ② 每段必带上下文（会话记忆 / 兜底 transcript），避免重复已做的事
#
# 观测：每段经 _opencode_json_runner 照常落 .log/.prompt.md 并记
# agent_start/end 事件（run_agent 同款约定）；另落 <stem>.seq.json
# 轮次日志。run_agent 本身一字节不动（向后兼容）。
# =====================================================================

SEQ_SAME_SIG_REPEAT = 2    # 同签名静态失败连发次数 = 零进展早退（工具惯例）
SEQ_TRANSCRIPT_TURNS = 8   # 兜底 transcript 保留的最近轮数（更早压一行）
SEQ_TAIL_LINES = 40        # 静态段结果注入尾行数（同 p4/errorloop 惯例）
SEQ_TURN_CHARS = 1500      # 兜底 transcript 单轮文本上限（防膨胀）
_SEQ_PHASES = ("run_static", "done")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _parse_events(out: str) -> dict | None:
    """从 opencode --format json 的 JSONL 事件流提取 {session_id, text}。

    实测格式（1.18.27）：每行一个 JSON 对象，顶层 sessionID；
    type=="text" 事件的 part.text 为助手文本（可多条，按序拼接）。
    防御式兼容：字段名变体（sessionID/session_id）、非 { 开头的
    噪音行（stderr 合并）一律跳过。无可解析事件返回 None。
    """
    session_id = None
    texts: list[str] = []
    for ln in (out or "").splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            ev = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        sid = ev.get("sessionID") or ev.get("session_id")
        if sid and session_id is None:
            session_id = str(sid)
        if ev.get("type") == "text":
            part = ev.get("part")
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                texts.append(part["text"])
    if session_id is None and not texts:
        return None
    return {"session_id": session_id, "text": "\n".join(texts).strip()}


def _opencode_json_runner(message: str, workdir: Path, log_stem: str,
                          timeout_sec: int, session_id: str | None = None,
                          model: str | None = None,
                          task: dict | None = None) -> tuple[int, str]:
    """内部调用器：opencode run --auto --format json（可选 --session 续接）。

    与 run_agent 同款归档/观测约定（.prompt.md/.log/agent_start/end，
    run_id=log_stem；vcs 隔离：段前后各一个工作区 commit，静态段结果
    文件由此随 agent 调用粒度入库），差异仅三点：输出为 JSON 事件流
    （供 _parse_events）、可续接会话、message 为增量消息（续接时不再
    重发任务全文）。
    """
    vws = _bound_ws()                    # vcs 隔离点的工作区（可能 None）
    if vws is not None:
        try:
            from . import vcs as _vcs
            _vcs.agent_pre(vws, str(log_stem))
        except Exception:
            pass
    model = model or os.environ.get("PORTER_MODEL", DEFAULT_MODEL)
    log_path = Path(f"{log_stem}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path = Path(f"{log_stem}.prompt.md")
    try:
        prompt_path.write_text(message, encoding="utf-8")
    except OSError:
        pass
    stem = str(log_stem)
    ref = {"log": str(log_path), "prompt": str(prompt_path)}
    tmeta = {k: (task or {}).get(k) for k in
             ("phase", "module", "step", "attempt")}
    try:
        from ..log import core as _log
        _log.record("agent_start", intent=stem, cmd=message,
                    summary=f"model={model}"
                            + (f" session={session_id}" if session_id else ""),
                    console_msg=f"[porter] agent: {log_stem} "
                                f"(model={model})",
                    run_id=stem, ref=ref, **tmeta)
    except Exception:
        pass
    args = ["opencode", "run", "--auto", "--format", "json",
            "--model", model, "--dir", str(workdir)]
    if session_id:
        args += ["--session", session_id]
    args.append(message)
    t0 = time.time()
    env = {**os.environ, "NO_COLOR": "1"}
    env.setdefault("OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX",
                   OUTPUT_TOKEN_MAX_DEFAULT)
    try:
        proc = subprocess.run(
            args, cwd=str(workdir), capture_output=True, text=True,
            timeout=timeout_sec, env=env,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        out = "TIMEOUT"
        rc = -1
    except FileNotFoundError:
        out = "opencode executable not found in PATH"
        rc = 127
    log_path.write_text(out, encoding="utf-8")
    elapsed = time.time() - t0
    try:
        from ..log import core as _log
        _log.record("agent_end", intent=stem, rc=rc,
                    summary=(out or "")[-300:].strip()
                    .replace("\n", " ⏎ "),
                    console_msg=f"[porter] agent: {log_stem} rc={rc} "
                                f"{elapsed:.0f}s log={log_path}",
                    run_id=stem, **tmeta)
    except Exception:
        pass
    try:                                # vcs：段调用后的隔离点（best-effort）
        if vws is not None:
            from . import vcs as _vcs
            _vcs.agent_post(vws, str(log_stem), rc)
    except Exception:
        pass
    return rc, out


def _validate_schema(obj, gen_schema: dict | None) -> list[str]:
    """必填字段 + 浅类型校验（str/int/list/dict；bool 不算 int）。"""
    if not isinstance(obj, dict):
        return ["输出不是 JSON 对象"]
    checks = {
        "str": lambda v: isinstance(v, str),
        "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "list": lambda v: isinstance(v, list),
        "dict": lambda v: isinstance(v, dict),
    }
    errs: list[str] = []
    for field, typ in (gen_schema or {}).items():
        if field not in obj or obj[field] is None:
            errs.append(f"缺必填字段 {field}")
            continue
        fn = checks.get(str(typ))
        if fn and not fn(obj[field]):
            errs.append(f"字段 {field} 期望 {typ}，"
                        f"实际 {type(obj[field]).__name__}")
    return errs


def _parse_phase(text: str) -> dict | None:
    """从段消息提取 phase JSON（```json 块优先，容忍裸整段 JSON）。

    合法形态：
      新协议  {"phase":"run_static"|"done", ...}
      老契约  {"status":"done"|"blocked", ...}——现存 skill（P4-migrate
              等）教的输出格式；done 等价 phase=done，blocked 作为
              携带 status 的 done 交还调用方走既有 panic 流程。
    找不到返回 None。
    """
    if not text:
        return None

    def _ok(obj) -> dict | None:
        if not isinstance(obj, dict):
            return None
        if obj.get("phase") in _SEQ_PHASES:
            return obj
        if obj.get("status") in ("done", "blocked"):
            return {**obj, "phase": "done"}
        return None

    blocks = re.findall(r"```json\s*(.*?)```", text, re.DOTALL) \
        or re.findall(r"```\s*(\{.*?\})\s*```", text, re.DOTALL)
    for b in blocks:
        try:
            hit = _ok(json.loads(b.strip()))
        except json.JSONDecodeError:
            continue
        if hit:
            return hit
    t = text.strip()
    if t.startswith("{"):
        try:
            return _ok(json.loads(t))
        except json.JSONDecodeError:
            pass
    return None


def _static_sig(text: str, tail_lines: int = SEQ_TAIL_LINES) -> str:
    """静态段失败结果的规范化签名（去 ANSI/路径→basename/时间戳→TS/
    独立数字→N，尾 N 行，sha1[:12]）。

    与 errorloop.failure_signature 同算法的本地实现（common 层不得
    import loop 层——防层倒置）。签名相同 = 同一个错误没修动。
    """
    if not text:
        return ""
    text = _ANSI_RE.sub("", text)
    out = []
    for ln in text.splitlines()[-tail_lines:]:
        ln = re.sub(r"/?(?:[\w.\-]+/)+([\w.\-]+)", r"\1", ln)
        ln = re.sub(r"\d{4}-\d{2}-\d{2}[T ][\d:]+(\.\d+)?", "TS", ln)
        ln = re.sub(r"\b\d{1,2}:\d{2}:\d{2}\b", "TS", ln)
        ln = re.sub(r"(?<![A-Za-z\d])\d+", "N", ln)
        ln = re.sub(r"\s+", " ", ln).strip()
        if ln:
            out.append(ln)
    return hashlib.sha1("\n".join(out).encode("utf-8")).hexdigest()[:12]


def _seq_preamble(static: dict | None, gen_schema: dict | None) -> str:
    """运行协议注入：禁令（禁止自行执行静态操作）+ phase 输出约定。"""
    lines = ["", "---", "", "## 运行协议（外部编排，必须遵守）"]
    n = 1
    if static:
        describe = static.get("describe") or "外部验证操作"
        lines.append(f"{n}. **禁止你自己执行「{describe}」**（含任何等价"
                     "命令）。该操作由外部执行并计时于 agent 预算之外；"
                     "你自己跑只会浪费时间且结果不被采信。")
        n += 1
        lines.append(f"{n}. 需要执行该操作时：停止工具调用，在消息末尾输出"
                     '唯一一个 ```json 块：`{"phase":"run_static",'
                     '"message":"<本轮做了什么/要验证什么>"}`。外部执行后'
                     "结果以【文件路径】提供（不注入原文），你须自行读取"
                     "该文件获取详情，届时继续。")
        n += 1
    fields = ""
    if gen_schema:
        fl = ", ".join(f'"{k}": <{v}>' for k, v in gen_schema.items())
        fields = f"，另含 {fl}"
    lines.append(f"{n}. 任务完成时：在消息末尾输出唯一一个 ```json 块："
                 f'`{{"phase":"done"{fields}}}`。')
    n += 1
    lines.append(f"{n}. 每轮消息末尾必须有且只有一个上述 JSON 块；其余"
                 "正文自由（简述本轮所为即可）。")
    return "\n".join(lines)


def _static_result_block(ok: bool, output: str, describe: str) -> str:
    """静态段结果的注入块（写盘失败时的降级 fallback）。"""
    from ..log import query as _lq
    tail = _lq.tail_text(output or "", SEQ_TAIL_LINES)
    verdict = "成功" if ok else "失败"
    block = (f"## 外部执行结果：{describe} —— {verdict}\n"
             f"输出尾 {SEQ_TAIL_LINES} 行：\n```\n{tail}\n```")
    if not ok:
        block += ("\n请根据以上输出修复问题后继续"
                  "（按运行协议输出下一个 phase JSON）。")
    return block


def _static_pointer_block(ok: bool, static_path: Path, describe: str) -> str:
    """静态段结果的指针块（2026-09-04 定案：指针优于载荷）。

    只含 verdict + 完整输出文件绝对路径 + 指引，零内容注入——
    agent 按需自读（tail/grep 自选窗口）；完整输出文件留在工作区，
    经 vcs 隔离点（agent_pre/agent_post）随 agent 调用粒度入库。
    """
    verdict = "成功" if ok else "失败"
    lines = [f"## 外部执行结果：{describe} —— {verdict}",
             f"完整输出：{static_path.resolve()}"
             "（自行 tail/grep 按需读取）"]
    if not ok:
        lines.append("请自行读取该文件定位问题，修复后继续"
                     "（按运行协议输出下一个 phase JSON）。")
    return "\n".join(lines)


def _transcript_block(turns: list[dict]) -> str:
    """兜底模式：模仿交互式对话轮次的注入块。

    turns = [{"role": "user"|"assistant", "text": ...}]。最近
    SEQ_TRANSCRIPT_TURNS 轮全量（单轮截 SEQ_TURN_CHARS），更早压一行。
    """
    if not turns:
        return ""
    recent = turns[-SEQ_TRANSCRIPT_TURNS:]
    older = turns[:-SEQ_TRANSCRIPT_TURNS]
    parts = []
    if older:
        digests = []
        for t in older:
            head = next((ln for ln in (t.get("text") or "")
                         .splitlines() if ln.strip()), "")
            digests.append(f"（{t['role']}）{head.strip()[:80]}")
        parts.append("更早轮次（摘要）：\n" + "\n".join(digests))
    body = []
    for t in recent:
        text = (t.get("text") or "").strip()[:SEQ_TURN_CHARS]
        who = "用户" if t["role"] == "user" else "助手"
        body.append(f"[{who}]\n{text}")
    parts.append("## 此前对话（接续上下文）\n" + "\n\n".join(body))
    return "\n\n".join(parts)


def run_agent_structured(prompt: str, workdir, log_stem: str, *,
                         gen_schema: dict,
                         max_tries: int = 2,
                         model: str | None = None,
                         timeout_sec: int = 600,
                         task: dict | None = None) -> tuple[int, str,
                                                            dict | None]:
    """单次结构化调用：run_agent + done 协议 + schema 校验 + 反馈重试。

    协议：agent 末尾输出 ```json {"phase":"done",...gen_schema 字段}```；
    校验失败（缺字段/类型错/无 JSON 块）自动带反馈重试 ≤ max_tries 次。
    返回 (rc, out, parsed)：parsed 为通过校验的 done 对象，未通过为 None。
    （run_agent_seq 的 done 校验复用同一套 _parse_phase/_validate_schema。）
    """
    base = prompt + "\n" + _seq_preamble(None, gen_schema)
    msg = base
    rc, out = -1, ""
    for attempt in range(1, max(1, max_tries) + 1):
        stem = str(log_stem) if max_tries <= 1 \
            else f"{log_stem}_R{attempt}"
        rc, out = run_agent(msg, workdir=Path(workdir), log_stem=stem,
                            model=model, timeout_sec=timeout_sec,
                            task=task)
        obj = _parse_phase(out) if rc == 0 else None
        if obj and obj.get("phase") == "done":
            if obj.get("status") == "blocked":
                return rc, out, obj      # 停车信号交还调用方（不校验 schema）
            errs = _validate_schema(obj, gen_schema)
            if not errs:
                return rc, out, obj
            feedback = ("---\n\n## 上一次输出的问题\n"
                        + "；".join(errs) + "。重新输出完整 JSON。")
        else:
            feedback = ("---\n\n## 上一次输出的问题\n未见合法 phase JSON"
                        '（{"phase":"done",...}）。重新输出。')
        msg = base + "\n\n" + feedback
    return rc, out, None


def run_agent_seq(task_prompt: str, workdir, log_stem: str, *,
                  static: dict | None = None,
                  agent_budget_sec: int = 1200,
                  gen_schema: dict | None = None,
                  final_static: bool = False,
                  model: str | None = None,
                  task: dict | None = None) -> dict:
    """split_long_op：agent 段 × N + 中间静态段的非交互长任务执行。

    参数：
      static          {"describe": 人话描述（用于禁令/结果块）,
                       "fn": () -> (ok: bool, output_text: str)}
                      ——通用长操作（编译/测试/启动皆可）；fn 自带超时
                      （编排器不代管；异常按失败处理）
      agent_budget_sec 所有 agent 段的总时间预算（原 timeout 语义；
                      静态段时长在预算之外）
      gen_schema      done 时必填字段+浅类型，如 {"files": "list"}
      final_static    True 时 done 后编排器再强制跑一次静态段终验，
                      失败则带结果回循环（仿 p4 probe_build 后验）

    段间接续：主路径 --session（无信息损失）；session id 解析不到时
    兜底为"模仿交互式轮次"的 prompt 注入（outcome["fallback"]=True）。

    返回 outcome：{"status": done|stalled|budget-exhausted|failed|no-agent,
      "session_id", "fallback", "rounds": [{seg, stem, rc, elapsed_sec,
      phase, schema_errs, static: {ok, sig}|None}], "parsed", 
      "total_agent_sec"}。轮次日志落 <log_stem>.seq.json。
    """
    workdir = Path(workdir)
    stem_base = str(log_stem)
    outcome: dict = {"status": None, "session_id": None, "fallback": False,
                     "rounds": [], "parsed": None, "total_agent_sec": 0.0}
    if os.environ.get("PORTER_NO_AGENT"):
        outcome["status"] = "no-agent"
        return outcome

    def _journal() -> None:
        try:
            Path(f"{stem_base}.seq.json").write_text(
                json.dumps(outcome, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            pass

    turns: list[dict] = []          # 兜底 transcript（用户/助手轮次）
    used = 0.0
    seg = 0
    prev_sig = ""
    sig_repeat = 1
    pending_user: str = ""          # 下一段要发的新增消息（静态结果/反馈）
    session_id: str | None = None

    while outcome["status"] is None:
        remaining = agent_budget_sec - used
        if remaining <= 0:
            outcome["status"] = "budget-exhausted"
            break
        seg += 1
        stem = f"{stem_base}_S{seg}"
        budget_note = (f"\n\n（agent 时间预算剩余约 "
                       f"{int(agent_budget_sec - used)}s；继续任务，按运行"
                       "协议输出下一 phase JSON。）")
        if seg == 1:
            message = task_prompt + "\n" + _seq_preamble(static, gen_schema)
        elif session_id:
            message = pending_user + budget_note
        else:
            outcome["fallback"] = True
            message = (task_prompt + "\n"
                       + _seq_preamble(static, gen_schema)
                       + "\n\n---\n\n" + _transcript_block(turns)
                       + "\n\n---\n\n## 请继续\n" + pending_user
                       + budget_note)
        t_seg = time.time()
        rc, out = _opencode_json_runner(
            message, workdir, stem, timeout_sec=int(remaining) + 1,
            session_id=session_id, model=model, task=task)
        elapsed = time.time() - t_seg
        if rc == 127:               # opencode 缺失：立即失败不烧预算
            outcome["status"] = "failed"
            outcome["rounds"].append({"seg": seg, "stem": stem, "rc": rc,
                                      "elapsed_sec": 0.0, "phase": None,
                                      "schema_errs": [],
                                      "static": None})
            break
        used += elapsed
        outcome["total_agent_sec"] = round(used, 1)
        parsed_ev = _parse_events(out) if rc == 0 else None
        if parsed_ev and parsed_ev.get("session_id"):
            session_id = parsed_ev["session_id"]
            outcome["session_id"] = session_id
        final_text = (parsed_ev or {}).get("text") or ""
        assistant_text = final_text or _raw_tail(out)
        turns.append({"role": "assistant", "text": assistant_text})
        phase_obj = _parse_phase(final_text) or _parse_phase(out)
        round_rec: dict = {"seg": seg, "stem": stem, "rc": rc,
                           "elapsed_sec": round(elapsed, 1),
                           "phase": (phase_obj or {}).get("phase"),
                           "schema_errs": [], "static": None}

        # ---- 静态段执行（run_static 请求 / final_static 终验共用） ----
        def _run_static() -> None:
            nonlocal prev_sig, sig_repeat, pending_user
            describe = (static or {}).get("describe") or "外部验证操作"
            try:
                ok, output = static["fn"]()
            except Exception as ex:            # 静态段异常按失败处理
                ok, output = False, f"静态段异常：{ex!r}"
            sig = ""
            if not ok:
                sig = _static_sig(output)
                if sig and sig == prev_sig:
                    sig_repeat += 1
                else:
                    sig_repeat = 1
                    prev_sig = sig
            else:
                prev_sig, sig_repeat = "", 1
            static_path = Path(f"{stem}_static.log")
            wrote = True
            try:                        # 完整输出落盘（指针目标；观测面不打断）
                static_path.write_text(str(output or ""), encoding="utf-8")
            except OSError:
                wrote = False
            round_rec["static"] = {"ok": bool(ok), "sig": sig or None,
                                   "log": str(static_path) if wrote
                                   else None}
            pending_user = (
                _static_pointer_block(bool(ok), static_path, describe)
                if wrote else
                _static_result_block(bool(ok), str(output or ""), describe))
            turns.append({"role": "user", "text": pending_user})

        if phase_obj is None:
            pending_user = ("## 上一段输出的问题\n未见合法 phase JSON"
                            "（run_static/done）。请按运行协议重新输出。")
            turns.append({"role": "user", "text": pending_user})
            outcome["rounds"].append(round_rec)
            _journal()
            continue

        if phase_obj.get("phase") == "run_static":
            if not static:
                pending_user = ("## 上一段请求被拒\n本次任务未配置外部"
                                "静态操作；请直接完成并输出 done JSON。")
                turns.append({"role": "user", "text": pending_user})
                outcome["rounds"].append(round_rec)
                _journal()
                continue
            _run_static()
            outcome["rounds"].append(round_rec)
            _journal()
            if sig_repeat >= SEQ_SAME_SIG_REPEAT:
                outcome["status"] = "stalled"
                break
            continue

        # ---- phase == done ----
        if phase_obj.get("status") == "blocked":
            # 老契约 blocked：携带 status 交还调用方走既有 panic 流程
            # （停车信号不是完成任务——不进 gen_schema 校验）
            outcome["status"] = "done"
            outcome["parsed"] = phase_obj
            outcome["rounds"].append(round_rec)
            _journal()
            break
        errs = _validate_schema(phase_obj, gen_schema)
        round_rec["schema_errs"] = errs
        if errs:
            pending_user = ("## 上一次 done 输出的问题\n"
                            + "；".join(errs)
                            + "。修复后重新输出完整 done JSON。")
            turns.append({"role": "user", "text": pending_user})
            outcome["rounds"].append(round_rec)
            _journal()
            continue
        if final_static and static:
            _run_static()
            outcome["rounds"].append(round_rec)
            _journal()
            if round_rec["static"] and round_rec["static"]["ok"]:
                outcome["status"] = "done"
                outcome["parsed"] = phase_obj
                break
            if sig_repeat >= SEQ_SAME_SIG_REPEAT:
                outcome["status"] = "stalled"
                break
            continue
        outcome["status"] = "done"
        outcome["parsed"] = phase_obj
        outcome["rounds"].append(round_rec)
        _journal()
        break

    if outcome["status"] is None:
        outcome["status"] = "budget-exhausted"
    try:
        from ..log import core as _log
        _log.record("agent_seq_end", intent=stem_base,
                    summary=f"{outcome['status']} segs={len(outcome['rounds'])} "
                            f"agent_sec={outcome['total_agent_sec']} "
                            f"fallback={outcome['fallback']}",
                    console_msg=f"[porter] agent-seq: {log_stem} "
                                f"status={outcome['status']} "
                                f"segs={len(outcome['rounds'])} "
                                f"agent_sec={outcome['total_agent_sec']}",
                    run_id=stem_base)
    except Exception:
        pass
    _journal()
    return outcome


def _raw_tail(out: str, lines: int = 20) -> str:
    """无可解析事件时的助手消息兜底：原始输出尾 N 行。"""
    return "\n".join((out or "").splitlines()[-lines:])
