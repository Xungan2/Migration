"""core.py — record()：log 子系统的唯一写入口（双 sink 分发 + 上下文戳）。

语义：
- 一次调用双 sink：console（人读，[porter] 行）+ events.jsonl（机读）；
  console_only / store_only 单走一面。
- console 行 = console_msg（显式，逐字兼容迁移用）或由
  scope+summary 派生（`[porter] <scope>: <summary>`）；两者皆缺时
  仅落 store。
- 上下文戳优先级：显式参数 > ctx() 戳 > bind() 兜底（mount 兼容面）。
- 派生事件助手：phase_begin/phase_end（时间线）、judge（双信号判定
  证据流）。kind 命名：存量族冻结，新增族 snake_case（docs/sub-systems/log.md）。
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from . import console, store

# 进程级上下文戳：{phase, module, step, attempt}（loop 内逐模块/逐步骤推进）
_CTX: dict = {}


@contextmanager
def ctx(**stamp):
    """临时上下文戳（相位/模块/步骤/尝试号）；作用域内 record 自动携带。"""
    saved = {k: _CTX.get(k) for k in stamp}
    _CTX.update({k: v for k, v in stamp.items() if v is not None})
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                _CTX.pop(k, None)
            else:
                _CTX[k] = v


def ctx_stamp() -> dict:
    return dict(_CTX)


def record(kind: str, subject: str | None = None, summary: str | None = None,
           intent: str | None = None, cmd: str | None = None,
           rc: int | None = None, *, scope: str | None = None,
           console_msg: str | None = None, console_only: bool = False,
           store_only: bool = False, level: str = "info",
           phase: str | None = None, module: str | None = None,
           step: str | None = None, attempt: int | None = None,
           run_id: str | None = None, ref: dict | None = None,
           mount: str | None = None, ws: Path | None = None,
           **extra) -> bool:
    """写一条记录（双 sink）。返回 store 是否落盘（console-only 恒 True）。

    console 行来源：console_msg > scope+summary 派生 > 无（仅 store）。
    """
    eff_phase = phase if phase is not None else _CTX.get("phase")
    eff_mount = mount if mount is not None else eff_phase   # mount=phase 旧名
    eff = {"module": module, "step": step, "attempt": attempt}
    stamped = {k: (v if v is not None else _CTX.get(k))
               for k, v in eff.items()}
    eff_scope = scope or eff_phase
    wrote = True
    if not store_only:
        line = console_msg
        if line is None and eff_scope and summary:
            line = console.format_line(str(eff_scope), str(summary))
        if line is not None:
            console.emit_line(line, level)
    if not console_only:
        wrote = store.append_event(
            kind, subject=subject, intent=intent, cmd=cmd, rc=rc,
            summary=summary, mount=eff_mount, ws=ws,
            phase=eff_phase, run_id=run_id, ref=ref,
            module=stamped["module"], step=stamped["step"],
            attempt=stamped["attempt"], level=level if level != "info"
            else None, **extra)
    return wrote


def console_only(scope: str, text: str, level: str = "info") -> bool:
    """纯 console 行（不落 events.jsonl）——闲聊/装饰性输出。"""
    return console.emit(scope, text, level)


def console_line(line: str, level: str = "info") -> bool:
    """整行直打（存量 [porter] 格式行的机械收编通路；级别门控）。

    print 扫尾（docs/sub-systems/log.md §8）的统一映射：print(f"[porter] …") →
    _log.console_line(f"[porter] …")——输出 byte 兼容，获得
    PORTER_LOG_LEVEL 门控与单一咽喉点。
    """
    return console.emit_line(line, level)


# ---------- 派生事件助手（新增 kind 族，snake_case） ----------

def phase_begin(phase: str, module: str | None = None,
                summary: str | None = None, **kw) -> bool:
    s = summary or (f"{phase}({module}) 开始" if module
                    else f"{phase} 开始")
    return record("phase_begin", subject=module, summary=s,
                  phase=phase, module=module, **kw)


def phase_end(phase: str, module: str | None = None,
              summary: str | None = None, rc: int | None = None,
              **kw) -> bool:
    s = summary or (f"{phase}({module}) 结束" if module
                    else f"{phase} 结束")
    return record("phase_end", subject=module, summary=s, rc=rc,
                  phase=phase, module=module, **kw)


def judge(subject: str, ok: bool, detail: str = "", *,
          intent: str = "build", log_ref: str | None = None,
          rc: int | None = None, phase: str | None = None,
          module: str | None = None, **kw) -> bool:
    """双信号判定证据流：每次 build/boot/ut 判定一行（可 grep 的时序真值）。"""
    return record("judge", subject=subject, intent=intent,
                  rc=rc if rc is not None else (0 if ok else 1),
                  summary=f"{'PASS' if ok else 'FAIL'} {detail}".strip(),
                  level="info" if ok else "error",
                  phase=phase, module=module,
                  ref={"log": log_ref} if log_ref else None, **kw)
