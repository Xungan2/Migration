"""ut_verify.py — unit_test 配置的机器烟测（"agent 说"→"机器验"）。

背景（2026-08-30 用户定案，双道烟测）：首切片实测暴露——补探 agent 声称
"结果打 stdout"是错的（实际落在 workspace 根 qemu-serial.log），且 "ok"
被 ANSI 颜色码包裹不可逐字匹配。因此：
  第一道（P0 门禁，env/gate.py）：真跑 agent 提供的 smoke_cmd（目标树里
    最小的已有单测 crate）验证机制与输出获取；
  第二道（loop 补探回填，loop/p4.py）：真跑驱动级 cmd 复核。

断言规则：命令退出码 0 + success_pattern 出现在去 ANSI 后的输出 +
fail_pattern（若有）不出现。
"""

from __future__ import annotations

from pathlib import Path

from ..env.probe import _base_env, _run, _strip_ansi


def verify_output(output_text: str, success_pattern: str,
                  fail_pattern: str | None = None) -> tuple[bool, str]:
    """纯判定：成功特征在 + 失败特征不在。返回 (ok, 说明)。"""
    text = _strip_ansi(output_text)
    if success_pattern not in text:
        return False, f"输出未见成功特征 {success_pattern!r}（去 ANSI 后）"
    if fail_pattern and fail_pattern in text:
        return False, f"输出出现失败特征 {fail_pattern!r}"
    return True, "特征命中"


def run_and_verify(cmd: str, cwd: Path, env: dict, timeout_sec: int,
                   log_path: Path, success_pattern: str,
                   fail_pattern: str | None = None) -> tuple[bool, str, str]:
    """真跑一次并断言。返回 (ok, 判定说明, 去 ANSI 后的完整输出)。"""
    rc, out = _run(cmd, cwd=cwd, env=env, timeout_sec=timeout_sec,
                   log_path=log_path)
    out = _strip_ansi(out)
    if rc != 0:
        return False, f"退出码 {rc}", out
    ok, detail = verify_output(out, success_pattern, fail_pattern)
    return ok, detail, out


def feedback_block(detail: str, out: str, tail_lines: int = 25) -> str:
    """给 agent 的失败反馈块（判定说明 + 观测输出尾部）。"""
    tail = "\n".join(out.splitlines()[-tail_lines:])
    return (f"\n\n---\n\n## 上一次烟测失败（{detail}）\n"
            f"观测输出尾部（去 ANSI）：\n```\n{tail}\n```\n"
            "请修正 cmd（确保结果文本送达 stdout——机制写文件就在命令尾部"
            "拼接读取）与 success/fail_pattern（避开 ANSI 包裹的 token），"
            "重新输出完整 JSON。")


def smoke_unit_test_config(ws: Path, target_os: Path, runner: dict,
                           ut: dict, label: str) -> tuple[bool, str]:
    """对一条 ut 配置（或其 smoke_cmd）做烟测。返回 (ok, 说明)。

    - cmd 为空 / mechanism=none：跳过（ok=True，说明注明）。
    - 有 smoke_cmd：跑 smoke_cmd（P0 门禁场景——驱动 crate 可能尚不存在）。
    - 否则跑 cmd 本体（loop 回填场景）。
    日志落 <ws>/logs/<label>.log。
    """
    if ut.get("mechanism") == "none" or not (ut.get("cmd") or
                                             ut.get("smoke_cmd")):
        return True, "mechanism=none 或无命令——跳过烟测"
    cmd = ut.get("smoke_cmd") or ut["cmd"]
    ok, detail, _out = run_and_verify(
        cmd, cwd=target_os, env=_base_env(target_os, runner),
        timeout_sec=int(ut.get("timeout_sec", 1800)),
        log_path=ws / "logs" / f"{label}.log",
        success_pattern=ut.get("success_pattern", "test result: ok"),
        fail_pattern=ut.get("fail_pattern"))
    return ok, detail
