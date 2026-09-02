"""agent.py — opencode 非交互调用的最小封装。

设计约定（来自 driver-migration 实验的既定原则）：
- agent 只承担"判断性"动作（类别识别/文档翻译/检索），确定性动作一律走脚本
- 模型经 PORTER_MODEL 环境变量配置，默认 zhipu-ai/glm-5.2
- 每次调用的完整输出落盘日志，供人工审核与成本归因
- SKILL 文件是行为指令的唯一来源：调用方组装 prompt = SKILL 正文 + 任务数据
"""

from __future__ import annotations

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


def run_agent(prompt: str, workdir: Path, log_stem: str,
              model: str | None = None, timeout_sec: int = 600,
              task: dict | None = None) -> tuple[int, str]:
    """调用 opencode 非交互模式执行一次 agent 任务。

    返回 (exit_code, stdout_text)。完整输出落盘 <log_stem>.log；输入
    原文归档 <log_stem>.prompt.md（与输出成对，docs/log.md 类 2）。
    观测埋桩（log 子系统）：events 绑定在场时前后写意图/结果事件，
    run_id = log_stem；task 传 {phase,module,step,attempt} 元数据
    （v1.1 结构字段）。
    """
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
