"""probe.py — T3 探测执行器（纯脚本，读 runner）。

关键语义（extract.py 调用）：
- 路径注入：所有命令经 shell 消费环境变量 PORTER_TARGET_OS_ROOT（=<目标树
  绝对路径>），runner.cmd 中以 ${PORTER_TARGET_OS_ROOT} 引用——脚本不做文本替换
- log_file：相对目标树根 或 宿主机绝对路径均可
- 双信号判定：退出码 + 日志特征（缺一不可）
- inject_device 双机制：env=合并环境变量；cmd=向 boot.cmd 追加 cmd_suffix
- build 用 timeout_full_sec（保守——缓存状态未知）
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from ..log import core as _log


def _base_env(target_os: Path, runner: dict, extra: dict | None = None) -> dict:
    return {**os.environ, **(runner.get("env") or {}),
            "PORTER_TARGET_OS_ROOT": str(target_os.resolve()), **(extra or {})}


def _run(cmd: str, cwd: Path, env: dict, timeout_sec: int,
         log_path: Path) -> tuple[int, str]:
    _log.record(
        "cmd_start", cmd=cmd, summary=str(log_path),
        console_msg=f"[porter] probe: "
                    f"{cmd[:110]}{'…' if len(cmd) > 110 else ''}",
        ref={"log": str(log_path)})
    t0 = time.time()
    try:
        proc = subprocess.run(["bash", "-c", cmd], cwd=str(cwd), env=env,
                              capture_output=True, text=True, timeout=timeout_sec)
        out = (proc.stdout or "") + (proc.stderr or "")
        rc = proc.returncode
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or "") if isinstance(e.stdout, str) else "") + \
            f"\nTIMEOUT after {timeout_sec}s"
        rc = -1
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(out, encoding="utf-8", errors="replace")
    elapsed = time.time() - t0
    _log.record(
        "cmd_end", cmd=cmd, rc=rc,
        summary=f"{elapsed:.0f}s log={log_path}",
        console_msg=f"[porter] probe: rc={rc} {elapsed:.0f}s "
                    f"log={log_path.name}",
        extra_out_tail=(out or "")[-200:].strip())
    return rc, out


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", text)


def _resolve_log(target_os: Path, bo: dict) -> tuple[Path | None, str]:
    """返回 (日志路径, 状态)。log_is_stdout=True 时无独立日志文件。"""
    if bo.get("log_is_stdout"):
        return None, "stdout"
    lf = bo.get("log_file")
    if not lf:
        return None, "unset"
    p = Path(lf) if lf.startswith("/") else target_os / lf
    return p, "file"


def probe_build(ws: Path, target_os: Path, runner: dict,
                label: str = "build") -> dict:
    b = runner["build"]
    log_path = ws / "logs" / f"{label}.log"
    rc, out = _run(b["cmd"], cwd=target_os,
                   env=_base_env(target_os, runner),
                   timeout_sec=int(b["timeout_full_sec"]),
                   log_path=log_path)
    ok = rc == 0
    if ok and b.get("success_pattern"):
        ok = b["success_pattern"] in _strip_ansi(out)
    try:                                    # judge 证据流（docs/log.md）
        _log.judge(label, ok, detail=f"rc={rc}" + (
            "" if not b.get("success_pattern")
            else f" pattern={'hit' if ok else 'MISS'}"),
            intent="build", log_ref=str(log_path),
            phase=(store_mounted() or None))
    except Exception:
        pass
    return {"item": "build", "ok": ok,
            "detail": f"rc={rc}" + ("" if not b.get("success_pattern")
                                    else f" pattern={'hit' if ok else 'MISS'}")}


def store_mounted() -> str | None:
    """当前 log 绑定的挂载点（judge/phase 事件的 phase 戳来源）。"""
    try:
        from ..log import store as _st
        return (_st.bound() or {}).get("mount")
    except Exception:
        return None


def probe_boot(ws: Path, target_os: Path, runner: dict,
               extra_env: dict | None = None, cmd_suffix: str | None = None,
               label: str = "boot") -> dict:
    bo = runner["boot"]
    cmd = bo["cmd"] + (f" {cmd_suffix}" if cmd_suffix else "")
    log_path, mode = _resolve_log(target_os, bo)
    if log_path is not None:
        try:
            log_path.unlink()      # 清旧日志防串判
        except FileNotFoundError:
            pass
    rc, out = _run(cmd, cwd=target_os,
                   env=_base_env(target_os, runner, extra_env),
                   timeout_sec=int(bo["timeout_sec"]),
                   log_path=ws / "logs" / f"T3_{label}.log")
    bo_log = ""
    if mode == "stdout":
        bo_log = _strip_ansi(out)
    elif log_path is not None:
        try:
            if log_path.exists():
                bo_log = _strip_ansi(log_path.read_text(encoding="utf-8",
                                                        errors="replace"))
        except OSError:
            bo_log = ""
    success = bo["success_pattern"] in bo_log
    panic = bo["panic_pattern"].lower() in bo_log.lower()
    log_state = mode if bo_log else f"{mode}:missing_or_empty"
    ok = (rc == 0) and success and not panic
    try:                                    # judge 证据流（boot 双信号）
        _log.judge(label, ok,
                   detail=f"rc={rc} success_pattern="
                          f"{'hit' if success else 'MISS'} "
                          f"panic={'yes' if panic else 'no'} "
                          f"log={log_state}",
                   intent="boot", log_ref=str(log_path),
                   rc=rc, phase=(store_mounted() or None))
    except Exception:
        pass
    return {"item": label, "ok": ok,
            "detail": (f"rc={rc} success_pattern={'hit' if success else 'MISS'} "
                       f"panic={'yes' if panic else 'no'} log={log_state}"),
            "log_empty": not bo_log}


def probe_boot_with_device(ws: Path, target_os: Path, runner: dict,
                           categories: list[str],
                           label: str = "boot_with_device") -> dict:
    inj = runner["inject_device"]
    examples = inj.get("example_args") or {}
    picked = next(((c, examples[c]) for c in categories if c in examples), None)
    if picked is None and examples:
        c0 = next(iter(examples))
        picked = (c0, examples[c0])
        _log.console_line(f"[porter] probe: ⚠️ 类别 {categories} 无设备实例，"
              f"以 {c0} 实例做机制验证")
    if picked is None:
        return {"item": label, "ok": False,
                "detail": "runner 无任何 example_args"}
    cat, dev_args = picked
    mech = inj.get("mechanism", "env")
    if mech == "env":
        extra = {k: v.replace("<DEVICE_ARGS>", dev_args)
                 for k, v in (inj.get("env") or {}).items()}
        r = probe_boot(ws, target_os, runner, extra_env=extra, label=label)
    else:
        suffix = (inj.get("cmd_suffix") or "").replace("<DEVICE_ARGS>", dev_args)
        r = probe_boot(ws, target_os, runner, cmd_suffix=suffix, label=label)
    r.update({"device_category": cat, "device_args": dev_args, "mechanism": mech})
    return r


def probe_development(ws: Path, target_os: Path, runner: dict,
                      categories: list[str]) -> dict:
    """三项顺序执行（extract 循环外的兼容入口）。"""
    p0 = ws / "P0"
    (p0 / "logs").mkdir(parents=True, exist_ok=True)
    (p0 / "reports").mkdir(exist_ok=True)
    results = [probe_build(p0, target_os, runner)]
    if results[-1]["ok"]:
        results.append(probe_boot(p0, target_os, runner))
        if results[-1]["ok"]:
            results.append(probe_boot_with_device(p0, target_os, runner,
                                                  categories))
    report = {"kind": "development", "results": results,
              "hard_gate_pass": all(r["ok"] for r in results)}
    (p0 / "reports" / "T3_development.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in results:
        try:
            _log.record("t3_verdict", subject=r["item"], rc=0 if r["ok"]
                        else 1, level="info" if r["ok"] else "error",
                        console_msg=f"[porter] T3: {r['item']:<18} "
                                    f"{'PASS' if r['ok'] else 'FAIL'}  "
                                    f"{r['detail']}",
                        ref={"report": "P0/reports/T3_development.json"})
        except Exception:
            pass
    return report
