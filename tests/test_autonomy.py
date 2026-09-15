"""Regression checks at the real provider/sequence boundary."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from porter.common import agent
from porter.handoff import publish_handoff, require_success
from porter import pre_mono


def event(text, message='final', kind='text'):
    return json.dumps({'type': kind, 'sessionID': 's',
                       'part': {'messageID': message, 'text': text}})


class AutonomyTests(unittest.TestCase):
    def test_message_boundaries_fragments_and_tool_output(self):
        output = '\n'.join([event('{"phase":"run_static"}', 'old'),
                            event('{"phase":"do', 'new'),
                            event('ne","notes":"a', 'new'),
                            event('b"}', 'new'),
                            event('{"phase":"run_static"}', 'tool', 'tool_use')])
        parsed = agent._parse_phase(agent._parse_events(output)['text'])
        self.assertEqual(parsed, {'phase': 'done', 'notes': 'ab'})
        self.assertIsNone(agent._parse_phase('```json\n{"phase":"run_static"}\n```\n'
                                            '```json\n{"phase":"done"}\n```'))
        self.assertEqual(agent._parse_phase('完成。 {"phase":"done"} 谢谢')['phase'], 'done')
        self.assertEqual(agent._parse_phase('[日志](build.log)\n```JSON\n{"phase":"done"}\n```')['phase'], 'done')
        self.assertIsNone(agent._parse_phase('[{"phase":"done"}]'))
        self.assertIsNone(agent._parse_phase('{broken {"phase":"done"}}'))

    def test_completion_repairs_and_session_loss_share_budget(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            clock, calls = [0], []
            def provider(message, _workdir, _stem, **kw):
                clock[0] += 1
                calls.append((message, kw))
                if len(calls) == 2:
                    return 1, 'session not found'
                return 0, event('{"phase":"done"}')
            attempts = []
            def validate(parsed):
                attempts.append(parsed)
                return len(attempts) >= 5, 'repair the saved artifact'
            with mock.patch.object(agent, '_opencode_json_runner', provider), \
                    mock.patch.object(agent.time, 'monotonic', lambda: clock[0]):
                result = agent.run_agent_seq('task', root, str(root/'seq'),
                                            agent_budget_sec=10, complete_check=validate)
            self.assertEqual(result['status'], 'done')
            self.assertEqual(result['total_agent_sec'], 6)
            self.assertEqual([kw['timeout_sec'] for _, kw in calls], [10, 9, 8, 7, 6, 5])
            self.assertIsNone(calls[2][1]['session_id'])
            self.assertIn('repair the saved artifact', calls[2][0])

    def test_changed_and_missing_handoffs_are_reviewed_again(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            runner = root/'runner.json'
            runner.write_text('{}')
            handoff = publish_handoff(root, 'upstream', summary='passed', artifacts=[runner])
            handoff.write_text('Corrected handoff')
            runner.rename(root/'moved-runner.json')
            self.assertEqual(len(require_success(root, 'upstream')['changes']), 2)
            calls = []
            def provider(message, *_args, **_kw):
                calls.append(message)
                if len(calls) == 1:
                    runner.write_text('{"build":{}}')
                return 0, event('{"phase":"done"}')
            with mock.patch.object(agent, '_opencode_json_runner', provider):
                result = agent.run_agent_seq('review', root, str(root/'seq'),
                                            handoff_inputs=(root, ('upstream',)))
            self.assertEqual(result['status'], 'done')
            self.assertEqual(len(calls), 2)
            self.assertIn('再次变化', calls[1])
            self.assertEqual(len(result['input_reviews']), 2)

    def test_interruption_keeps_feedback_and_prior_logs(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            with mock.patch.object(agent, '_opencode_json_runner', side_effect=KeyboardInterrupt):
                result = agent.run_agent_seq('task', root, str(root/'seq'))
            self.assertEqual(result['status'], 'interrupted')
            self.assertEqual(json.loads((root/'seq.seq.json').read_text())['status'], 'interrupted')
            with mock.patch.object(agent, '_opencode_json_runner', return_value=(0, event('{"phase":"done"}'))):
                result = agent.run_agent_seq('resume', root, str(root/'seq'))
            self.assertEqual(result['status'], 'done')
            self.assertTrue(list(root.glob('seq.seq.*/seq.seq.json')))

    def test_pre_mono_repairs_bad_json_and_missing_module_in_one_session(self):
        with TemporaryDirectory() as raw:
            ws = Path(raw)
            (ws/'prepare').mkdir()
            (ws/'prepare/state.json').write_text('{"status":"complete"}')
            (ws/'migration-plan.md').write_text('Plan')
            publish_handoff(ws, 'prepare', summary='ready', artifacts=[ws/'migration-plan.md'])
            calls = []
            def provider(message, *_args, **kw):
                calls.append(kw.get('session_id'))
                (ws/'module-division.md').write_text('Division')
                (ws/'migration-plan.json').write_text('{"order":["m"]}')
                (ws/'module-division.json').write_text(
                    '{' if len(calls) == 1 else '{"order":["m"],"modules":{"m":{}},"driver_home":"driver"}')
                if len(calls) >= 3:
                    (ws / "runner.json").write_text("[]" if len(calls) == 3 else "{}")
                    directory = ws/'mono-input/modules/m'
                    directory.mkdir(parents=True, exist_ok=True)
                    (directory/'module.json').write_text('{}')
                    (directory/'spec.md').write_text('Module spec')
                return 0, event('{"phase":"done"}')
            with mock.patch.object(agent, '_opencode_json_runner', provider):
                self.assertEqual(pre_mono.run(ws), 0)
            self.assertEqual(calls, [None, 's', 's', 's'])
            self.assertTrue(require_success(ws, 'pre-mono'))

    def test_prepare_to_accept_with_corrections_and_approval(self):
        import shutil
        from test_unify import UnifiedTest
        from test_exp_mono import _mk_fixture, _run_with_fakes, _write_module_product
        from test_accept import _write_pair, SECS_T1, SECS_INJECT, SEC_E2E
        from porter.exp import accept
        from support.sequence import replay

        prepared = UnifiedTest()
        prepared.setUp()
        self.addCleanup(prepared.doCleanups)
        result = prepared.cli()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ws = prepared.ws
        fixture_root = prepared.root / 'mono-fixture'
        fixture_root.mkdir()
        fixture = _mk_fixture(fixture_root)
        pre_calls = []
        def decompose(_message, *_args, **_kw):
            pre_calls.append(1)
            for source in fixture['ws'].iterdir():
                if source.name == 'handoffs':
                    continue
                target = ws / source.name
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True)
                else:
                    shutil.copyfile(source, target)
            for name in ('fx-a', 'fx-b'):
                (ws / 'mono-input/modules' / name / 'spec.md').write_text('Executable module')
            (ws / 'module-division.json').write_text(
                '{' if len(pre_calls) == 1 else (ws / 'module-divsion.json').read_text())
            return 0, event('{"phase":"done"}')
        with mock.patch.object(agent, '_opencode_json_runner', decompose):
            self.assertEqual(pre_mono.run(ws), 0)
        self.assertGreaterEqual(len(pre_calls), 2)
        fixture['ws'] = ws
        mono = _run_with_fakes(self, fixture,
            lambda module: _write_module_product(fixture['home'], module.replace('-', '_')),
            research_bad={'fx-a': 1})
        self.assertEqual(mono['rc'], 0)
        self.assertTrue(require_success(ws, 'mono'))

        calls = {}
        manifest_path = ws / "module-division.json"
        saved_manifest = manifest_path.read_text()
        manifest_path.write_text("{")
        def acceptance_turn(_prompt, _workdir, _stem, **kw):
            tier = kw['task']['step']
            calls[tier] = calls.get(tier, 0) + 1
            if tier == "inputs":
                manifest_path.write_text(saved_manifest)
                return {"status": "done", "session_id": "input-repair", "parsed": {"phase": "done"}}
            directory = ws / 'exp-accept/acceptance'
            directory.mkdir(parents=True, exist_ok=True)
            sections = (SECS_T1 if tier == 't1' else SECS_INJECT if tier == 'inject'
                        else {SEC_E2E[0]: SEC_E2E[1:]})
            for number, (slug, cmd, contains) in sections.items():
                if tier == 'e2e' and calls[tier] == 1:
                    contains = 'deliberate first verification failure'
                _write_pair(directory, number, slug, cmd, contains)
            return {'status': 'done', 'session_id': tier,
                    'parsed': {'status': 'done', 'deliverable': str(directory), 'notes': 'Verified revision'}}
        def sequence(prompt, workdir, log_stem, **kw):
            return replay(acceptance_turn, prompt, workdir, log_stem, **kw)
        with mock.patch.object(agent, 'run_agent_seq', sequence), \
                mock.patch('porter.common.vcs.commit_target', return_value=[]):
            for tier in ('t1', 'inject', 'e2e'):
                self.assertEqual(accept.run_accept(ws), 3)
                self.assertFalse(require_success(ws, 'mono') is None)
                with (ws / 'answers.md').open('a') as answers:
                    answers.write(f"## @{accept._GATE_OF[tier]}\nverdict: approve\nnote: reviewed {tier}\n\n")
            self.assertEqual(accept.run_accept(ws), 0)
            self.assertEqual(accept.run_accept(ws, execute=True), 0)
        self.assertEqual(calls['e2e'], 2)
        self.assertEqual(calls['inputs'], 1)
        self.assertTrue(require_success(ws, 'accept'))
