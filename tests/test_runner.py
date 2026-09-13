import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from porter.runner import RunnerError, load
from porter.common import agent


class TestRunnerContract(unittest.TestCase):
    def test_load_preserves_agent_extensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runner.json"
            path.write_text(json.dumps({"build": {"cmd": "make"},
                                        "agent_extension": {"mode": "fast"}}),
                            encoding="utf-8")
            value = load(path)
            self.assertEqual(value["agent_extension"]["mode"], "fast")

    def test_load_rejects_bad_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "runner.json"
            path.write_text('{"build": "make"}', encoding="utf-8")
            with self.assertRaises(RunnerError):
                load(path)

    def test_timeout_keeps_session_for_outer_retry(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(agent, "_opencode_json_runner",
                                  return_value=(-1, '{"sessionID":"sid-1"}')):
            outcome = agent.run_agent_seq("continue", Path(tmp),
                                          str(Path(tmp) / "run"),
                                          agent_budget_sec=1)
        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(outcome["retryable"], "timeout")
        self.assertEqual(outcome["session_id"], "sid-1")


if __name__ == "__main__":
    unittest.main()
