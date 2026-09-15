"""Run the production sequence loop with a deterministic provider and clock."""
import json
from unittest import mock
from porter.common import agent

RUN_SEQUENCE = agent.run_agent_seq


def replay(turn, prompt, workdir, log_stem, **kwargs):
    clock = [0.0]
    budget = kwargs.get('agent_budget_sec', 600)

    def provider(message, workdir, stem, timeout_sec, session_id=None, **meta):
        outcome = turn(message, workdir, log_stem,
                       resume_session=session_id, **{k: v for k, v in kwargs.items()
                                                    if k != 'resume_session'})
        clock[0] += min(timeout_sec, max(1, budget / 4))
        status = outcome.get('status')
        if status in ('failed', 'stalled'):
            return 1, ''
        if status == 'budget-exhausted':
            clock[0] += budget
            return -1, json.dumps({'type': 'step_finish', 'sessionID': outcome.get('session_id')})
        return 0, json.dumps({'type': 'text', 'sessionID': outcome.get('session_id'),
                             'part': {'text': json.dumps(outcome.get('parsed') or {})}})

    with mock.patch.object(agent, '_opencode_json_runner', provider), \
            mock.patch.object(agent.time, 'monotonic', lambda: clock[0]):
        return RUN_SEQUENCE(prompt, workdir, log_stem, **kwargs)
