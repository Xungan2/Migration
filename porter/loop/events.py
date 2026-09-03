"""events.py — 观测地基门面（实现已迁 porter/log/，docs/sub-systems/log.md 为规范）。

本模块保留为兼容 re-export：既有 14 个调用点与行为级测试
（test_events/test_diagnose/test_replay/test_mounts/test_s15_bypass）
经 `porter.loop.events` 的导入路径不变。新代码请直接 import porter.log。
"""

from ..log.store import (append_event, bind, bound, note_agent_end,
                         note_agent_start, note_cmd_end, note_cmd_start,
                         read_events, tail_events, unbind)
from ..log.snapshot import take_failure_snapshot

__all__ = ["append_event", "bind", "bound", "unbind", "read_events",
           "tail_events", "note_agent_start", "note_agent_end",
           "note_cmd_start", "note_cmd_end", "take_failure_snapshot"]
