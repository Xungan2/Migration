"""Compatibility import surface for the handoff protocol."""

from . import (Execution, Manager, TaskSpec, current_execution, digest,
               fingerprints, latest_success, prepare_agent_prompt,
               publish_handoff, require_success, run_task)

__all__ = ["Execution", "Manager", "TaskSpec", "current_execution", "digest",
           "fingerprints", "latest_success", "prepare_agent_prompt",
           "publish_handoff", "require_success", "run_task"]
