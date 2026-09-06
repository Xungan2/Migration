"""Public interface for Porter's durable handoff module."""

from .core import (Execution, HandoffError, HandoffManager, Material, NotReady,
                   StaleInputs, TaskSpec, current_execution,
                   prepare_agent_prompt, run_task)
from .integration import (execute_cli, execute_phase, module_dependencies,
                          module_task_id)

__all__ = [
    "Execution", "HandoffError", "HandoffManager", "Material", "NotReady", "StaleInputs",
    "TaskSpec", "current_execution", "prepare_agent_prompt", "run_task",
    "execute_cli", "execute_phase", "module_dependencies", "module_task_id",
]
