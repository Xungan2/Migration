"""console.py — console sink（log 子系统的人读面）。

行格式标准（docs/sub-systems/log.md §格式类 5）：`[porter] <scope>: <text>`——
scope ∈ {P0..P7, T3, loop, gates, kb, …}，与存量 301 处 print 的既有
约定一致，本模块将其定型。级别标记（⚠️/✖ 等）写在 text 内（存量
约定），level 只做可见性过滤（PORTER_LOG_LEVEL，缺省 info）。
永不抛异常。
"""

from __future__ import annotations

import os
import sys

_LEVELS = {"debug": 0, "info": 1, "warn": 2, "error": 3}


def _threshold() -> int:
    return _LEVELS.get(os.environ.get("PORTER_LOG_LEVEL", "info"), 1)


def format_line(scope: str, text: str) -> str:
    """渲染一行（不打印）——测试与 console_msg 复用。"""
    return f"[porter] {scope}: {text}"


def emit(scope: str, text: str, level: str = "info") -> bool:
    """打印一行 `[porter] <scope>: <text>`（低于阈值跳过）。永不抛异常。"""
    return emit_line(format_line(scope, text), level)


def emit_line(line: str, level: str = "info") -> bool:
    """打印已格式化的整行（record 的 console 通路）。永不抛异常。"""
    try:
        if _LEVELS.get(level, 1) < _threshold():
            return False
        print(line)
        return True
    except Exception:
        return False
