import json
import tempfile
import unittest
from pathlib import Path
from porter.log.store import append_event
from porter.monitor import Monitor, analyze_log, discover_tasks


class MonitorTest(unittest.TestCase):
    def test_timeout_log_keeps_session_and_reason(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "run.log"
            path.write_text('TIMEOUT\n{"sessionID":"sid-7"}\nerror: compiler hung\n')
            value = analyze_log(path)
            self.assertTrue(value["timeout"])
            self.assertEqual(value["session_id"], "sid-7")
            self.assertIn("compiler hung", value["reason"])

    def test_poll_writes_taskboard_and_escalates_after_budget(self):
        with tempfile.TemporaryDirectory() as root:
            ws = Path(root)
            handoffs = ws / "prepare" / "handoffs"
            handoffs.mkdir(parents=True)
            delivery = handoffs / "task-1.md"
            (ws / "prepare" / "state.json").write_text(json.dumps({
                "tasks": [{"id": "task-1", "status": "running",
                           "handoff": str(delivery), "goals": []}]}))
            log_path = ws / "run.log"
            log_path.write_text('TIMEOUT\n{"sessionID":"sid-1"}\nerror: hung\n')
            append_event("agent_start", intent="run", run_id="run", task_id="task-1",
                         ws=ws, ref={"log": str(log_path)}, session_id="sid-1")
            append_event("agent_end", intent="run", run_id="run", task_id="task-1",
                         rc=124, ws=ws, session_id="sid-1")
            rows = Monitor(ws, owner_pid=999999, budget=0).poll_once()
            self.assertEqual(rows[0]["status"], "waiting-human")
            self.assertTrue((ws / "HUMAN.md").exists())
            self.assertIn("waiting-human", (ws / "taskboard.md").read_text())

    def test_done_declared_task_without_handoff_is_reported(self):
        with tempfile.TemporaryDirectory() as root:
            ws = Path(root)
            (ws / "prepare").mkdir()
            (ws / "prepare" / "state.json").write_text(json.dumps({
                "tasks": [{"id": "task-1", "status": "done",
                           "handoff": str(ws / "missing.md"), "goals": []}]}))
            log_path = ws / "run.log"
            log_path.write_text('{"type":"text","part":{"text":"done"}}\n')
            append_event("agent_start", intent="run", run_id="run", task_id="task-1",
                         ws=ws, ref={"log": str(log_path)})
            append_event("agent_end", intent="run", run_id="run", task_id="task-1",
                         rc=0, ws=ws)
            self.assertEqual(discover_tasks(ws)[0]["status"], "done")
            self.assertIn("without its declared handoff", discover_tasks(ws)[0]["reason"])


if __name__ == "__main__":
    unittest.main()
