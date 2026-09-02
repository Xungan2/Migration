"""porter.log — 对外唯一进口（稳定 API 面；规范见 docs/log.md）。

写：record（双 sink）/ console_only / event（仅 store）/
    ctx（上下文戳）/ phase_begin / phase_end / judge
存：store.append_event / read_events / tail_events / bind / unbind / bound
据：query.*（runs / context_block / timeline）
证：snapshot.take_failure_snapshot

兼容门面：porter/loop/events.py re-export 本包（旧调用点零改动）。
"""

from . import console, core, query, snapshot, store
from .console import emit, format_line
from .core import (console_only, ctx, ctx_stamp, judge, phase_begin,
                   phase_end, record)
from .snapshot import take_failure_snapshot
from .store import (append_event, bind, bound, note_agent_end,
                    note_agent_start, note_cmd_end, note_cmd_start,
                    read_events, tail_events, unbind)

__all__ = [
    "console", "core", "query", "snapshot", "store",
    "record", "console_only", "emit", "format_line",
    "ctx", "ctx_stamp", "phase_begin", "phase_end", "judge",
    "take_failure_snapshot",
    "append_event", "bind", "bound", "unbind",
    "read_events", "tail_events",
    "note_agent_start", "note_agent_end", "note_cmd_start", "note_cmd_end",
]
