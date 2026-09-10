"""Public CLI with a local opencode executable; no model or target OS needed."""
import json
import fcntl
import signal
import time
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

CLI = Path(__file__).resolve().parents[1] / 'porter/main.py'


class UnifiedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.target = self.root / 'target'
        self.ws = self.root / 'workspace'
        self.source.mkdir()
        self.target.mkdir()
        (self.source / 'driver.c').write_text('int init(void) { return 0; }\n')
        self.args = ['prepare', '--linux-driver', str(self.source), '--target-os',
                     str(self.target), '--output-dir', str(self.ws), '--budget', '10']
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        executable = self.bin / 'opencode'
        shutil.copyfile(Path(__file__).parent / 'support/opencode.py', executable)
        executable.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'])

    def cli(self, *extra):
        return subprocess.run([sys.executable, str(CLI), *self.args, *extra],
                              env=self.env, capture_output=True, text=True, timeout=20)

    def test_prepare_inputs_without_claiming_acceptance(self):
        result = self.cli('--prepare-only')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        project = json.loads((self.ws / 'project.json').read_text())
        self.assertEqual(project['linux_driver'], str(self.source))
        self.assertEqual(project['target_os'], str(self.target))
        self.assertFalse((self.ws / 'knowledgebase/verification.md').exists())
        self.assertFalse((self.ws / 'runner.json').exists())

    def test_both_acceptances_and_knowledge_required(self):
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        state = json.loads((self.ws / 'prepare/state.json').read_text())
        self.assertEqual(state['status'], 'complete')
        self.assertEqual(state['acceptance']['skeleton']['status'], 'pass')
        self.assertEqual(state['acceptance']['planning']['status'], 'pass')
        self.assertTrue((self.ws / 'knowledgebase/verification.md').is_file())
        self.assertFalse((self.ws / 'P1/modules').exists())

    def test_partial_acceptance_missing_evidence_and_unsynced_knowledge_block(self):
        for scenario in ('skeleton-only', 'planning-only', 'missing-evidence', 'knowledge-failed'):
            with self.subTest(scenario=scenario):
                if self.ws.exists():
                    shutil.rmtree(self.ws)
                self.env['SCENARIO'] = scenario
                result = self.cli()
                self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
                state = json.loads((self.ws / 'prepare/state.json').read_text())
                self.assertEqual(state['status'], 'blocked')
                self.assertTrue(list((self.ws / 'prepare/handoffs').glob('*.md')))

    def test_knowledge_conflict_returns_to_owner_for_resolution(self):
        self.env['SCENARIO'] = 'knowledge-conflict'
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        calls = [json.loads(line) for line in (self.ws / 'provider-calls.jsonl').read_text().splitlines()]
        self.assertTrue(any(c['role'] == 'owner' and c['data'].get('knowledge_receipt') for c in calls))
        knowledge_calls = [c for c in calls if c['role'] == 'knowledge']
        self.assertGreaterEqual(len(knowledge_calls), 2)
        self.assertEqual({c['session'] for c in knowledge_calls}, {'knowledge-session'})

    def test_tasks_get_selected_handoffs_and_knowledge_session_continues(self):
        self.env['SCENARIO'] = 'tasks'
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        calls = [json.loads(line) for line in (self.ws / 'provider-calls.jsonl').read_text().splitlines()]
        tasks = [c for c in calls if c['role'] == 'task']
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0]['data']['inputs'], [str(self.source / 'driver.c')])
        self.assertEqual(len(tasks[1]['data']['inputs']), 1)
        handoff = Path(tasks[1]['data']['inputs'][0])
        self.assertIn('Task findings', handoff.read_text())
        self.assertNotIn('handoff_index', tasks[1]['data'])
        self.assertNotIn('acceptance', tasks[1]['data'])
        knowledge = [c for c in calls if c['role'] == 'knowledge']
        self.assertGreaterEqual(len(knowledge), 3)
        self.assertEqual({c['session'] for c in knowledge}, {'knowledge-session'})

    def test_resume_preserves_valid_acceptance_and_rechecks_changed_evidence(self):
        self.assertEqual(self.cli().returncode, 0)
        before = json.loads((self.ws / 'prepare/state.json').read_text())['acceptance']
        self.assertEqual(self.cli().returncode, 0)
        after = json.loads((self.ws / 'prepare/state.json').read_text())['acceptance']
        self.assertEqual(before, after)
        (self.target / 'skeleton.c').write_text('changed target code')
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        calls = [json.loads(line) for line in (self.ws / 'provider-calls.jsonl').read_text().splitlines()]
        changed = [c['data']['acceptance'] for c in calls if c['role'] == 'owner'
                   and c['data'].get('acceptance', {}).get('skeleton', {}).get('status') == 'needs-review']
        self.assertTrue(changed)
        self.assertEqual(changed[-1]['planning']['status'], 'pass')

    def test_session_unavailable_recovers_from_durable_inputs(self):
        self.assertEqual(self.cli().returncode, 0)
        self.env['SCENARIO'] = 'session-lost'
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        calls = [json.loads(line) for line in (self.ws / 'provider-calls.jsonl').read_text().splitlines()]
        self.assertTrue(calls[-1]['data']['acceptance_report'])
        self.assertTrue(calls[-1]['data']['knowledge_index'])

    def test_knowledge_reorganization_preserves_historical_handoffs(self):
        self.env['SCENARIO'] = 'reorganize'
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        old = self.ws / 'knowledgebase/build/details.md'
        self.assertIn('new/notes.md', old.read_text())
        self.assertTrue((old.parent / 'new/notes.md').is_file())
        self.assertTrue(any('build/details.md' in p.read_text()
                            for p in (self.ws / 'prepare/handoffs').glob('*task*.md')))

    @unittest.skipUnless(shutil.which('cc'), 'local C compiler required')
    def test_local_build_and_load_evidence_survives_delivery(self):
        self.env['SCENARIO'] = 'local-compile'
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        evidence = (self.ws / 'build-load.log').read_text()
        self.assertIn('skeleton.c', evidence)
        self.assertIn('loaded fixture', evidence)

    def test_invalid_intent_and_identity_do_not_overwrite_workspace(self):
        intent = self.root / 'intent.md'
        intent.write_text('Preserve zero behavior.')
        self.assertEqual(self.cli('--prepare-only', '--intent-file', str(intent)).returncode, 0)
        before = (self.ws / 'project.json').read_bytes()
        for content in (b'', b'  ', b'\xff'):
            intent.write_bytes(content)
            self.assertEqual(self.cli('--prepare-only', '--intent-file', str(intent)).returncode, 2)
            self.assertEqual((self.ws / 'project.json').read_bytes(), before)
            self.assertEqual((self.ws / 'goals.md').read_text(), 'Preserve zero behavior.')
        intent.unlink()
        os.mkfifo(intent)
        self.assertEqual(self.cli('--prepare-only', '--intent-file', str(intent)).returncode, 2)
        other = self.root / 'other-target'
        other.mkdir()
        self.assertEqual(self.cli('--prepare-only', '--target-os', str(other)).returncode, 2)
        self.assertEqual((self.ws / 'project.json').read_bytes(), before)
        self.assertEqual(self.cli('--budget', '0').returncode, 2)

    def test_changed_intent_reaches_owner_without_discarding_evidence(self):
        self.assertEqual(self.cli().returncode, 0)
        evidence = (self.ws / 'build-load.log').read_bytes()
        intent = self.root / 'intent.md'
        intent.write_text('Keep zero behavior; exclude ioctl.')
        self.assertEqual(self.cli('--intent-file', str(intent)).returncode, 0)
        calls = [json.loads(line) for line in (self.ws / 'provider-calls.jsonl').read_text().splitlines()]
        self.assertTrue(any(c['role'] == 'owner' and
                            c['data'].get('acceptance', {}).get('planning', {}).get('status') == 'needs-review'
                            for c in calls))
        self.assertEqual((self.ws / 'build-load.log').read_bytes(), evidence)
        self.assertEqual(self.cli('--prepare-only').returncode, 0)
        self.assertEqual((self.ws / 'goals.md').read_text(), intent.read_text())

    def test_workspace_lock_rejects_concurrent_execution(self):
        self.assertEqual(self.cli('--prepare-only').returncode, 0)
        with (self.ws / '.porter.lock').open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.cli()
        self.assertEqual(result.returncode, 2)
        self.assertIn('already running', result.stderr)
        self.assertFalse((self.ws / 'provider-calls.jsonl').exists())

    def test_legacy_workspace_and_retired_commands_are_not_started(self):
        self.ws.mkdir()
        project = self.ws / 'project.json'
        project.write_text('{"linux_driver": "legacy"}')
        self.assertEqual(self.cli().returncode, 2)
        self.assertEqual(project.read_text(), '{"linux_driver": "legacy"}')
        self.args[0] = 'p1'
        self.assertEqual(self.cli().returncode, 2)

    def test_timeout_saves_partial_output_and_stops_descendants(self):
        self.env['SCENARIO'] = 'timeout'
        result = self.cli('--budget', '1')
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        self.assertTrue(any('partial output' in p.read_text() for p in (self.ws / 'prepare/logs').glob('*.log')))
        status = Path('/proc') / (self.ws / 'child.pid').read_text() / 'stat'
        if status.exists():
            self.assertEqual(status.read_text().split()[2], 'Z')
        self.env['SCENARIO'] = 'success'
        self.assertEqual(self.cli().returncode, 0)

    def test_interrupt_preserves_state_and_logs(self):
        self.env['SCENARIO'] = 'timeout'
        with subprocess.Popen([sys.executable, str(CLI), *self.args], env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
            deadline = time.monotonic() + 5
            while not (self.ws / 'child.pid').exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.ws / 'child.pid').exists())
            proc.send_signal(signal.SIGINT)
            proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 130)
        self.assertEqual(json.loads((self.ws / 'prepare/state.json').read_text())['status'], 'blocked')
        self.assertTrue(list((self.ws / 'prepare/logs').glob('*.log')))

    def test_malformed_delivery_and_missing_provider_are_blocked(self):
        self.env['SCENARIO'] = 'bad-response'
        self.assertEqual(self.cli().returncode, 3)
        (self.bin / 'opencode').unlink()
        self.env['PATH'] = str(self.bin)
        self.assertEqual(self.cli().returncode, 3)
        state = json.loads((self.ws / 'prepare/state.json').read_text())
        self.assertEqual(state['status'], 'blocked')
        self.assertIn('127', state['error'])

    def test_task_cannot_publish_shared_knowledge_or_rewrite_history(self):
        for scenario in ('task-writes-knowledge', 'rewrite-handoff'):
            with self.subTest(scenario=scenario):
                if self.ws.exists():
                    shutil.rmtree(self.ws)
                self.env['SCENARIO'] = scenario
                result = self.cli()
                self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
                self.assertEqual(json.loads((self.ws / 'prepare/state.json').read_text())['status'], 'blocked')

    def test_provider_progress_text_does_not_replace_final_delivery(self):
        self.env['SCENARIO'] = 'commentary'
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_workspace_outputs_do_not_invalidate_enclosing_source_or_material(self):
        self.ws = self.source / 'work'
        self.args[self.args.index('--output-dir') + 1] = str(self.ws)
        result = self.cli('--materials', str(self.source))
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(self.cli('--materials', str(self.source)).returncode, 0)

    def test_interrupted_task_preserves_partial_handoff(self):
        self.env['SCENARIO'] = 'task-timeout'
        result = self.cli('--budget', '1')
        self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
        handoffs = list((self.ws / 'prepare/handoffs').glob('*task*.md'))
        self.assertTrue(any('Partial finding' in p.read_text() for p in handoffs))
        self.env['SCENARIO'] = 'success'
        self.assertEqual(self.cli().returncode, 0)
        self.assertTrue(any('Partial finding' in p.read_text() for p in handoffs))

    def test_lost_knowledge_session_replays_pending_handoffs(self):
        self.env['SCENARIO'] = 'knowledge-failed'
        self.assertEqual(self.cli().returncode, 3)
        accepted = json.loads((self.ws / 'prepare/state.json').read_text())['acceptance']
        self.env['SCENARIO'] = 'session-lost'
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        state = json.loads((self.ws / 'prepare/state.json').read_text())
        self.assertEqual(state['acceptance'], accepted)
        calls = [json.loads(line) for line in (self.ws / 'provider-calls.jsonl').read_text().splitlines()]
        knowledge = [c for c in calls if c['role'] == 'knowledge']
        self.assertEqual([c['resumed'] for c in knowledge], [False, True, False])
        self.assertEqual(knowledge[-1]['data']['handoffs'], knowledge[-2]['data']['handoffs'])
        self.assertTrue(all(p in state['incorporated'] for p in knowledge[-1]['data']['handoffs']))

    def test_repointed_source_reference_requires_owner_review(self):
        first = self.target / 'first.c'
        first.write_text('initial target source')
        link = self.target / 'skeleton.c'
        link.symlink_to(first)
        self.assertEqual(self.cli().returncode, 0)
        second = self.target / 'second.c'
        second.write_text('changed source selected by build')
        link.unlink()
        link.symlink_to(second)
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        calls = [json.loads(line) for line in (self.ws / 'provider-calls.jsonl').read_text().splitlines()]
        self.assertTrue(any(c['role'] == 'owner' and
                            c['data'].get('acceptance', {}).get('skeleton', {}).get('status') == 'needs-review'
                            for c in calls))


if __name__ == '__main__':
    unittest.main()
