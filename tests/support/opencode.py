#!/usr/bin/env python3
"""Deterministic external provider for CLI integration tests."""
import json
import os
from pathlib import Path
import sys
import subprocess
import time

message = sys.stdin.read()
data = json.loads(message.split('\nTASK INPUT\n', 1)[1])
ws = Path(data['workspace'])
role = data['role']
scenario = os.environ.get('SCENARIO', 'success')
session = sys.argv[sys.argv.index('--session') + 1] if '--session' in sys.argv else role + '-session'
with (ws / 'provider-calls.jsonl').open('a') as stream:
    stream.write(json.dumps({'role': role, 'session': session, 'resumed': '--session' in sys.argv, 'data': data}) + '\n')
history = [json.loads(line) for line in (ws / 'provider-calls.jsonl').read_text().splitlines()]
if scenario == 'task-timeout' and role == 'task':
    Path(data['handoff']).write_text('Partial finding: driver has one entry point. Next: inspect dependencies.')
if scenario == 'timeout' or (scenario == 'task-timeout' and role == 'task'):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    (ws / 'child.pid').write_text(str(child.pid))
    print(json.dumps({'type': 'text', 'sessionID': session, 'part': {'text': 'partial output'}}), flush=True)
    time.sleep(60)
if scenario == 'bad-response':
    print(json.dumps({'type': 'text', 'sessionID': session, 'part': {'text': 'not a JSON delivery'}}))
    sys.exit(0)
if scenario == 'session-lost' and '--session' in sys.argv:
    print('session not found', file=sys.stderr)
    sys.exit(1)
if scenario == 'task-writes-knowledge' and role == 'task':
    (ws / 'knowledgebase').mkdir(exist_ok=True)
    (ws / 'knowledgebase/unauthorized.md').write_text('task must not own this')
if scenario == 'mixed-provider-failure' and role == 'task':
    (ws / 'partial-plan.md').write_text('Scope: driver. Module: core (driver.c). Dependencies: none. Order: core.')
    Path(data['handoff']).write_text('Planning complete: partial-plan.md. Protocol check failed; cause unknown.')
    print('Protocol check transport failed', flush=True)
    sys.exit(1)
if scenario.startswith('optional-error') and role == 'task':
    Path(data['handoff']).write_text('Partial check: controller unavailable. Cause unknown.')
    print('check execution failed', flush=True)
    if scenario == 'optional-error-interrupted':
        (ws / 'optional-started').write_text(str(os.getpid()))
        time.sleep(60)
    if scenario == 'optional-error-timeout':
        time.sleep(60)
    if scenario == 'optional-error-integrity':
        (ws / 'knowledgebase').mkdir(exist_ok=True)
        (ws / 'knowledgebase/unauthorized.md').write_text('unauthorized')
    sys.exit(1)
if role == 'owner':
    tasks = [c for c in history if c['role'] == 'task']
    latest_goals = {g['id']: (g, t) for t in data.get('tasks', []) for g in t['goals']}
    unfinished = [(g, t) for g, t in latest_goals.values() if g['required'] and g.get('status') != 'delivered']
    if scenario == 'success' and unfinished:
        goal, previous = unfinished[0]
        result = {'action': 'task', 'id': previous['id'], 'category': previous['category'],
                  'prompt': 'Resume the interrupted source investigation from its checkpoint.',
                  'inputs': [previous['handoff']], 'goals': [dict(goal, retry={
                      'evidence': [previous['handoff']], 'correction': 'Resume from saved checkpoint with available budget'})]}
    elif scenario == 'optional-resume' and not (ws / 'resume-requested').exists():
        (ws / 'resume-requested').write_text('requested')
        result = {'action': 'task', 'id': 'renamed-after-resume', 'category': 'planning',
                  'prompt': 'Try the controller again', 'inputs': [],
                  'goals': [{'id': 'controller', 'objective': 'Check controller with new arguments',
                             'required': False, 'reason': 'Supplemental', 'completion': 'Controller responds'}]}
    elif scenario == 'required-unresolved' and not tasks:
        result = {'action': 'task', 'id': 'must-build', 'category': 'skeleton',
                  'prompt': 'Build skeleton', 'inputs': []}
    elif scenario in ('selected-category', 'malformed-goal', 'mixed-provider-failure') and not tasks:
        result = {'action': 'task', 'id': 'selected', 'category': os.environ.get('TASK_CATEGORY', 'planning'),
                  'prompt': 'Deliver the requested work and optional protocol check.', 'inputs': [],
                  'goals': [{'id': 'main', 'objective': 'Deliver work', 'required': True,
                             'reason': 'Planning or skeleton acceptance', 'completion': 'Evidence delivered'},
                            {'id': 'protocol', 'objective': 'Check protocol', 'required': False,
                             'reason': 'Supplemental only', 'completion': 'Check succeeds'}]}
    elif scenario.startswith('required-retry') and len(tasks) < 2:
        result = {'action': 'task', 'id': 'build', 'category': 'skeleton',
                  'prompt': 'Build and load skeleton', 'inputs': [],
                  'goals': [{'id': 'build-load', 'objective': 'Build and load', 'required': True,
                             'reason': 'Skeleton acceptance', 'completion': 'Build and load evidence'}]}
        if tasks and scenario == 'required-retry-with-evidence':
            result['goals'][0]['retry'] = {'evidence': [tasks[-1]['data']['handoff']],
                                         'correction': 'Use the supported compiler after missing-tool failure'}
    elif scenario.startswith('optional-error') and not tasks:
        result = {'action': 'task', 'id': 'optional-error', 'category': 'planning',
                  'prompt': 'Check controller once', 'inputs': [], 'timeout': 0.2,
                  'goals': [{'id': 'controller', 'objective': 'Check controller', 'required': False,
                             'reason': 'Supplemental', 'completion': 'Controller responds'}]}
        if scenario == 'optional-error-interrupted':
            result['timeout'] = 5
    elif scenario == 'optional-once' and not data.get('task_feedback', {}).get('rejected'):
        result = {'action': 'task', 'id': 'plan-check', 'category': 'planning',
                  'prompt': 'Deliver plan and check protocol once.', 'inputs': [], 'timeout': 2,
                  'goals': [{'id': 'protocol', 'objective': 'Check protocol', 'required': False,
                             'reason': 'Supplemental only', 'completion': 'Protocol check succeeds'}]}
        if tasks:
            result['id'] = 'renamed-check'
            result['prompt'] = 'Use a different command and wait longer.'
            result['timeout'] = 3
    elif scenario in ('tasks', 'reorganize', 'task-writes-knowledge', 'task-timeout') and len(tasks) < 2:
        inputs = ([str(Path(data['project']['linux_driver']) / 'driver.c')] if not tasks else
                  [p for p in data['handoff_index'] if '-task-' in p][-1:])
        result = {'action': 'task', 'prompt': 'Investigate driver dependencies; report evidence only.', 'inputs': inputs}
    elif scenario == 'knowledge-conflict' and data.get('knowledge_receipt') and not (ws / 'resolved').exists():
        (ws / 'resolved').write_text('owner chose the supported observation')
        result = {'action': 'record', 'report': 'Owner resolved conflicting findings using source evidence.'}
    elif data.get('acceptance') and all(v['status'] != 'needs-review' for v in data['acceptance'].values()):
        result = {'action': 'finish', 'knowledge_reviewed': True}
    else:
        target = Path(data['project']['target_os'])
        code = target / 'skeleton.c'
        code.write_text('int init(void) { return 0; }\n')
        evidence = ws / 'build-load.log'
        evidence.write_text('fixture build and load evidence; not a real OS experiment\n')
        plan = ws / 'suggested-plan.md'
        plan.write_text('Scope: driver. Module: core (driver.c). Dependencies: none. Order: core.\n')
        result = {'action': 'accept', 'report': 'Owner reviewed skeleton and proposed plan.',
                  'skeleton': {'status': 'pass', 'reason': 'build and load reviewed',
                               'evidence': [str(evidence)], 'inputs': [str(code)]},
                  'planning': {'status': 'pass', 'reason': 'scope, modules and order reviewed',
                               'evidence': [str(plan)], 'inputs': [str(Path(data['project']['linux_driver']) / 'driver.c')]}}
        if scenario == 'mixed-provider-failure':
            result['planning']['evidence'] = [str(ws / 'partial-plan.md')]
            result['completed_goals'] = {'main': 'planning'}
            result['report'] = 'Owner reviewed preserved partial-plan.md; main goal complete. Protocol check deferred.'
        if scenario == 'local-compile':
            code.write_text('#include <stdio.h>\n__attribute__((constructor)) static void init(void) { puts("loaded fixture"); fflush(stdout); }\n')
            build = subprocess.run(['cc', '-shared', '-fPIC', '-MMD', str(code), '-o', str(target / 'driver.so')], capture_output=True, text=True)
            if build.returncode:
                raise RuntimeError(build.stderr)
            loaded = subprocess.run([sys.executable, '-c', 'import ctypes; ctypes.CDLL(' + repr(str(target / 'driver.so')) + ')'], capture_output=True, text=True)
            evidence.write_text((target / 'driver.d').read_text() + loaded.stdout)
        if scenario == 'missing-evidence':
            evidence.unlink()
        if scenario in ('skeleton-only', 'planning-only'):
            key = 'planning' if scenario == 'skeleton-only' else 'skeleton'
            result[key] = {'status': 'blocked', 'reason': 'needs user input', 'evidence': [], 'inputs': []}
elif role == 'knowledge':
    if scenario == 'knowledge-conflict' and not (ws / 'resolved').exists():
        print(json.dumps({'type': 'text', 'sessionID': session, 'part': {'text': json.dumps(
            {'status': 'needs-owner', 'report': 'Conflicting observations; owner decision required.'})}}))
        sys.exit(0)
    if scenario == 'knowledge-failed':
        print(json.dumps({'type': 'text', 'sessionID': session, 'part': {'text': 'interrupted knowledge'}}))
        sys.exit(1)
    if scenario == 'rewrite-handoff':
        Path(data['handoffs'][0]).write_text('rewritten history')
    kb = ws / 'knowledgebase'
    kb.mkdir(exist_ok=True)
    for name in ('README.md', 'AUTO-DECISION.md', 'AUTO-TODO.md', 'AUTO-FIXME.md', 'verification.md'):
        (kb / name).write_text('Current knowledge; owner acceptance is in the supplied handoff.\n')
    for topic in ('build', 'boot', 'plan'):
        (kb / topic).mkdir(exist_ok=True)
        (kb / topic / 'README.md').write_text('[Details](details.md)\n')
        (kb / topic / 'details.md').write_text('Evidence reviewed by owner.\n')
    if scenario == 'reorganize' and len([c for c in history if c['role'] == 'knowledge']) > 1:
        (kb / 'build/new').mkdir(exist_ok=True)
        (kb / 'build/new/notes.md').write_text('Current build instructions.\n')
        (kb / 'build/details.md').write_text('[Moved](new/notes.md)\n')
        (kb / 'build/README.md').write_text('[Build](new/notes.md)\n')
    result = {'status': 'updated', 'report': 'Updated supplied handoffs.'}
else:
    result = {'status': 'delivered', 'report': 'Task findings and evidence. Build reference: ../../knowledgebase/build/details.md'}
    if scenario == 'optional-once':
        result = {'status': 'blocked', 'report': 'Protocol check failed; cause unknown. Evidence: protocol.log. '
                  'AUTO-TODO: retry when a controller is available; completion: protocol check succeeds.',
                  'goals': [{'id': 'protocol', 'status': 'blocked'}]}
    if scenario.startswith('required-retry') and len([c for c in history if c['role'] == 'task']) == 1:
        result = {'status': 'blocked', 'report': 'Build failed: compiler unavailable.'}
    if scenario == 'required-unresolved':
        result = {'status': 'blocked', 'report': 'Build failed: compiler unavailable.'}
    if scenario == 'selected-category':
        assert '\nTASK SKILL\n' in message, 'Selected task skill missing'
        result = {'status': 'delivered', 'report': 'Planning complete. Protocol unavailable; defer until controller exists.',
                  'goals': [{'id': 'main', 'status': 'delivered'}, {'id': 'protocol', 'status': 'blocked'}]}
    if scenario == 'malformed-goal':
        result['goals'] = [{'id': [], 'status': 'delivered'}, {'id': 'protocol', 'status': 'blocked'}]
if role == 'knowledge' and (scenario in ('optional-once', 'optional-resume', 'mixed-provider-failure') or scenario.startswith('optional-error')):
    (kb / 'AUTO-TODO.md').write_text('\n'.join(p.read_text() for p in (ws / 'prepare/handoffs').glob('*task*.md')))
if role == 'owner' and result.get('action') == 'task':
    result.setdefault('id', 'task-' + str(len(tasks)))
    result.setdefault('category', 'source')
    result.setdefault('timeout', 5)
    result.setdefault('goals', [{'id': result['id'], 'objective': 'Investigate source',
                               'required': True, 'reason': 'Needed for planning', 'completion': 'Evidence delivered'}])
if role == 'task':
    result.setdefault('goals', [{'id': g['id'], 'status': result['status']} for g in data['goals']])
if scenario == 'commentary':
    print(json.dumps({'type': 'text', 'sessionID': session, 'part': {'text': 'I will inspect the supplied source.'}}))
print(json.dumps({'type': 'text', 'sessionID': session, 'part': {'text': json.dumps(result)}}))
