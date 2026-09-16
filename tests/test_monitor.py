import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from porter import main as cli
from porter.log.store import append_event
from porter.monitor import Monitor, analyze_log, discover_tasks


class MonitorTest(unittest.TestCase):
    def add_run(self, ws, run_id, rc, task_id="exp-mono.translate.m2", text="ok"):
        path = ws / f"{run_id}.log"
        path.write_text(text)
        append_event("agent_start", ws=ws, run_id=run_id, task_id=task_id,
                     ref={"log": str(path)})
        if rc is not None:
            append_event("agent_end", ws=ws, run_id=run_id, rc=rc)

    def test_success_log_prose_does_not_trigger_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            ws = Path(root)
            self.add_run(ws, "run", 0, text='SPI-NOR timeout handling {"status":"blocked"}')
            monitor = Monitor(ws, owner_pid=999999)
            with mock.patch.object(monitor, "_recover") as recover:
                rows = monitor.poll_once()
            self.assertEqual(rows[0]["status"], "done")
            recover.assert_not_called()
            self.assertFalse(analyze_log(ws / "run.log")["timeout"])

    def test_latest_run_replaces_old_failures(self):
        with tempfile.TemporaryDirectory() as root:
            ws = Path(root)
            for i in range(11):
                self.add_run(ws, f"old-{i}", 124)
            self.add_run(ws, "new", 0)
            monitor = Monitor(ws, owner_pid=999999)
            monitor.state["tasks"]["exp-mono.translate.m2"] = {
                "status": "waiting-human", "attempts": 1}
            with mock.patch.object(monitor, "_recover") as recover:
                rows = monitor.poll_once()
            self.assertEqual([(r["run_id"], r["status"]) for r in rows], [("new", "done")])
            recover.assert_not_called()

    def test_pass_ledger_supersedes_failed_run(self):
        with tempfile.TemporaryDirectory() as root:
            ws = Path(root)
            self.add_run(ws, "old", 124)
            (ws / "exp-mono").mkdir()
            (ws / "exp-mono/ledger.json").write_text(json.dumps({"modules": {
                "m2": {"status": "pass"}}}))
            monitor = Monitor(ws, owner_pid=999999)
            with mock.patch.object(monitor, "_recover") as recover:
                rows = monitor.poll_once()
            self.assertEqual(rows[0]["status"], "done")
            recover.assert_not_called()

    def test_recovered_host_publishes_exit(self):
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.dict(os.environ, {"PORTER_NO_MONITOR": "1"}), \
                    mock.patch("porter.exp.mono.run_exp_mono", return_value=0):
                self.assertEqual(cli.main(["mono", "--output-dir", root]), 0)
            self.assertEqual(json.loads((Path(root) / ".porter-exit.json").read_text())["rc"], 0)

    def test_clean_exit_stops_without_recovering_old_failure(self):
        with tempfile.TemporaryDirectory() as root:
            ws = Path(root)
            self.add_run(ws, "old", 124)
            (ws / ".porter-exit.json").write_text(json.dumps({"owner_pid": 999999, "rc": 0}))
            monitor = Monitor(ws, owner_pid=999999)
            with mock.patch.object(monitor, "_recover") as recover, \
                    mock.patch("porter.monitor.time.sleep", side_effect=AssertionError("did not stop")):
                self.assertEqual(monitor.run(), 0)
            recover.assert_not_called()

    def test_stale_exit_does_not_suppress_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            ws = Path(root)
            self.add_run(ws, "run", 124)
            (ws / ".porter-exit.json").write_text(json.dumps({"owner_pid": 888888, "rc": 0}))
            monitor = Monitor(ws, owner_pid=999999)
            with mock.patch.object(monitor, "_recover") as recover:
                monitor.poll_once()
            recover.assert_called_once()

    def test_recovery_starts_only_one_host_per_poll(self):
        with tempfile.TemporaryDirectory() as root:
            ws = Path(root)
            self.add_run(ws, "a", 124, task_id="a")
            self.add_run(ws, "b", 124, task_id="b")
            (ws / ".porter-command.json").write_text(json.dumps({
                "argv": ["mono", "--output-dir", root], "owner_pid": 999999}))
            monitor = Monitor(ws)
            with mock.patch.object(monitor, "_run_opencode", return_value={"rc": 0}), \
                    mock.patch("porter.monitor.subprocess.Popen") as spawn:
                spawn.return_value.pid = os.getpid()
                monitor.poll_once()
                monitor.poll_once()
            spawn.assert_called_once()
            self.assertEqual(spawn.call_args.kwargs["env"]["PORTER_NO_MONITOR"], "1")

    def test_existing_monitor_still_registers_new_owner(self):
        with tempfile.TemporaryDirectory() as root:
            ws = Path(root)
            (ws / ".monitor.pid").write_text(str(os.getpid()))
            (ws / ".porter-exit.json").write_text('{"rc":0}')
            with mock.patch.dict(os.environ, {}, clear=True), \
                    mock.patch("pathlib.Path.read_bytes", return_value=b"porter.monitor"), \
                    mock.patch("porter.monitor.start") as start:
                cli._start_monitor(ws, ["mono", "--output-dir", root])
            start.assert_not_called()
            self.assertFalse((ws / ".porter-exit.json").exists())
            self.assertEqual(json.loads((ws / ".porter-command.json").read_text())["owner_pid"], os.getpid())

    def test_orphan_log_is_evidence_not_recoverable_task(self):
        with tempfile.TemporaryDirectory() as root:
            ws = Path(root)
            (ws / "old.log").write_text("TIMEOUT")
            (ws / "old.prompt.md").write_text("old prompt")
            monitor = Monitor(ws, owner_pid=999999)
            with mock.patch.object(monitor, "_recover") as recover:
                monitor.poll_once()
            recover.assert_not_called()

    def test_missing_declared_handoff_keeps_recovery_budget(self):
        with tempfile.TemporaryDirectory() as root:
            ws = Path(root)
            (ws / "prepare").mkdir()
            (ws / "prepare/state.json").write_text(json.dumps({"tasks": [{
                "id": "task", "status": "done", "handoff": "missing.md"}]}))
            self.add_run(ws, "run", 0, task_id="task")
            monitor = Monitor(ws, owner_pid=999999)
            with mock.patch.object(monitor, "_run_opencode", return_value={"rc": 0}) as repair:
                monitor.poll_once()
                monitor.poll_once()
            repair.assert_called_once()
            self.assertEqual(monitor.state["tasks"]["task"]["status"], "waiting-human")

    def test_new_failed_run_does_not_reset_recovery_budget(self):
        with tempfile.TemporaryDirectory() as root:
            ws = Path(root)
            self.add_run(ws, "new", 124)
            monitor = Monitor(ws, owner_pid=999999)
            monitor.state["tasks"]["exp-mono.translate.m2"] = {
                "run_id": "old", "attempts": 1, "status": "debugged"}
            with mock.patch.object(monitor, "_run_opencode") as repair:
                rows = monitor.poll_once()
            repair.assert_not_called()
            self.assertEqual(rows[0]["status"], "waiting-human")

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
