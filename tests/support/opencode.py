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
    stream.write(json.dumps({'role': role, 'session': session, 'data': data}) + '\n')
history = [json.loads(line) for line in (ws / 'provider-calls.jsonl').read_text().splitlines()]
if scenario == 'timeout':
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
if role == 'owner':
    tasks = [c for c in history if c['role'] == 'task']
    if scenario in ('tasks', 'reorganize', 'task-writes-knowledge') and len(tasks) < 2:
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
if scenario == 'commentary':
    print(json.dumps({'type': 'text', 'sessionID': session, 'part': {'text': 'I will inspect the supplied source.'}}))
print(json.dumps({'type': 'text', 'sessionID': session, 'part': {'text': json.dumps(result)}}))
