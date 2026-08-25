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


def load_skill(name: str) -> str:
    """读取 skills/ 下的 SKILL 文件正文。"""
    path = SKILLS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"SKILL not found: {path}")
    return path.read_text(encoding="utf-8")


def run_agent(prompt: str, workdir: Path, log_stem: str,
              model: str | None = None, timeout_sec: int = 600) -> tuple[int, str]:
    """调用 opencode 非交互模式执行一次 agent 任务。

    返回 (exit_code, stdout_text)。完整输出同时落盘 <log_stem>.log。
    """
    model = model or os.environ.get("PORTER_MODEL", DEFAULT_MODEL)
    log_path = Path(f"{log_stem}.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "opencode", "run", "--auto",
        "--model", model,
        "--dir", str(workdir),
        prompt,
    ]
    print(f"[porter] agent: {log_stem} (model={model})")
    t0 = time.time()
    try:
        proc = subprocess.run(
            args, cwd=str(workdir), capture_output=True, text=True,
            timeout=timeout_sec, env={**os.environ, "NO_COLOR": "1"},
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
    print(f"[porter] agent: {log_stem} rc={rc} {time.time()-t0:.0f}s log={log_path}")
    return rc, out


def extract_json(out: str) -> dict | None:
    """从 agent 输出中提取唯一的 ```json 代码块并解析。

    agent 约定只输出一个 JSON 块（SKILL 中强制）；失败返回 None。
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
    return None
