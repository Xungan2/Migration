"""One owner, on-demand tasks, one continuing knowledge writer; Markdown evidence."""
import hashlib
import json
from pathlib import Path
import time
import uuid

from porter import provider
from porter.workspace import read_json, read_text, write_json


class ProviderFailure(RuntimeError):
    """An execution failure, distinct from workspace integrity or budget failures."""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fingerprints(paths: list[str], *, allow_directories: bool = False) -> dict:
    result = {}
    for name in paths:
        path = Path(name)
        if allow_directories and path.is_absolute() and path.is_dir():
            contents = [(str(p.relative_to(path)), digest(p))
                        for p in sorted(path.rglob('*')) if p.is_file()]
            result[str(path)] = hashlib.sha256(json.dumps(contents).encode()).hexdigest()
            continue
        if not path.is_absolute() or not path.is_file():
            raise ValueError(f'Evidence/input must be an absolute regular file: {name}')
        if not path.stat().st_size:
            raise ValueError(f'Empty evidence/input: {path}')
        result[str(path)] = digest(path)
    return result


class Preparation:
    def __init__(self, ws: Path, project: dict, budget: int):
        self.ws, self.project = ws, project
        self.root = ws / 'prepare'
        self.root.mkdir(exist_ok=True)
        self.state_path = self.root / 'state.json'
        self.state = read_json(self.state_path) if self.state_path.exists() else {}
        self.state.setdefault('sessions', {})
        self.state.setdefault('acceptance', {})
        self.state.setdefault('incorporated', {})
        self.state.setdefault('tasks', [])
        self.state.update(status='running', run_id=uuid.uuid4().hex, error=None)
        self.deadline = time.monotonic() + budget
        self.target = Path(project['target_os'])
        self.handoffs = self.root / 'handoffs'
        self.handoffs.mkdir(exist_ok=True)
        self.save()

    def save(self):
        write_json(self.state_path, self.state)

    def context(self) -> str:
        source = Path(self.project['linux_driver'])
        files = [p for p in source.rglob('*')
                 if '.git' not in p.relative_to(source).parts and not p.is_relative_to(self.ws)]
        files += [self.ws / 'project.json', self.ws / 'goals.md', self.ws / 'answers.md']
        files += list((self.ws / 'inputs').rglob('*.md'))
        for name in self.project.get('materials', []):
            path = Path(name)
            files += [p for p in path.rglob('*') if not p.is_relative_to(self.ws)
                      and '.git' not in p.relative_to(path).parts] if path.is_dir() else [path]
        values = [(str(p), digest(p)) for p in sorted(set(files)) if p.is_file()]
        return hashlib.sha256(json.dumps(values).encode()).hexdigest()

    def refresh(self):
        context = self.context()
        for decision in self.state['acceptance'].values():
            if decision['status'] != 'pass':
                continue
            try:
                unchanged = fingerprints(list(decision['files']), allow_directories=True) == decision['files']
            except (OSError, ValueError):
                unchanged = False
            if decision['context'] != context or not unchanged:
                decision['status'] = 'needs-review'
                decision['reason'] = 'Inputs or cited evidence changed; owner must review affected conclusions.'
        self.save()

    def handoff(self, role: str, report: str) -> Path:
        if not isinstance(report, str) or not report.strip():
            raise ValueError(f'{role} must deliver a nonempty report')
        path = self.handoffs / f'{time.time_ns()}-{role}-{uuid.uuid4().hex[:8]}.md'
        path.write_text(report + '\n', encoding='utf-8')
        return path

    def invoke(self, role: str, data: dict, timeout: float | None = None) -> dict:
        protected = {str(p): digest(p) for p in self.handoffs.glob('*.md')}
        kb = self.ws / 'knowledgebase'
        knowledge_before = {str(p): digest(p) for p in kb.rglob('*') if p.is_file()}
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('Total preparation budget exhausted')
        if timeout is not None:
            remaining = min(remaining, timeout)
        stem = self.root / 'logs' / f'{time.time_ns()}-{role}'
        session = self.state['sessions'].get(role) if role != 'task' else None
        data = dict(data, role=role, workspace=str(self.ws))
        self.state['last_call'] = {'role': role, 'log': str(stem) + '.log'}
        self.save()
        rc, text, session = provider.run(role, data, self.target, stem, remaining, session)
        if role != 'task':
            self.state['sessions'][role] = session
        self.save()
        if rc == 130:
            raise KeyboardInterrupt
        if rc:
            # Only an explicitly unavailable session gets one fresh attempt.
            lower = text.lower()
            if role != 'task' and session and 'session' in lower and ('not found' in lower or 'does not exist' in lower):
                self.state['sessions'][role] = None
                self.save()
                remaining = self.deadline - time.monotonic()
                if remaining > 0:
                    rc, text, session = provider.run(role, data, self.target,
                                                    Path(str(stem) + '-fresh'), remaining)
                    if role != 'task':
                        self.state['sessions'][role] = session
                    self.save()
            if rc == 130:
                raise KeyboardInterrupt
        if fingerprints(list(protected)) != protected:
            raise RuntimeError('Historical handoff changed during provider execution')
        if role != 'knowledge':
            knowledge_after = {str(p): digest(p) for p in kb.rglob('*') if p.is_file()}
            if knowledge_before != knowledge_after:
                raise RuntimeError('Only the knowledge agent may write shared knowledge')
        if time.monotonic() >= self.deadline:
            raise RuntimeError('Total preparation budget exhausted')
        if rc:
            raise ProviderFailure(f'{role} provider failed ({rc}); logs: {stem}.log')
        try:
            return provider.response(text)
        except ValueError as exc:
            raise ProviderFailure(f'{role} invalid provider response; logs: {stem}.log') from exc

    def sync_knowledge(self):
        pending = {str(p): digest(p) for p in sorted(self.handoffs.glob('*.md'))
                   if self.state['incorporated'].get(str(p)) != digest(p)}
        if not pending:
            return
        self.state['knowledge_current'] = False
        self.save()
        result = self.invoke('knowledge', {
            'handoffs': list(pending), 'index': str(self.ws / 'knowledgebase/README.md'),
            'acceptance': self.state['acceptance'],
            'previous_receipt': self.state.get('knowledge_receipt'),
        })
        receipt = self.root / 'knowledge' / f'{time.time_ns()}.md'
        receipt.parent.mkdir(exist_ok=True)
        receipt.write_text(str(result.get('report', '')) + '\n', encoding='utf-8')
        self.state['knowledge_receipt'] = str(receipt)
        if result.get('status') == 'needs-owner' and str(result.get('report', '')).strip():
            self.save()
            return
        if result.get('status') != 'updated' or not str(result.get('report', '')).strip():
            self.save()
            raise RuntimeError(f'Knowledge needs owner attention: {receipt}')
        if fingerprints(list(pending)) != pending:
            raise RuntimeError('A historical handoff changed during knowledge maintenance')
        self.state['incorporated'].update(pending)
        self.state['knowledge_current'] = True
        self.save()

    def accept(self, result: dict):
        acceptance = {}
        for name in ('skeleton', 'planning'):
            value = result.get(name)
            if not isinstance(value, dict) or value.get('status') not in ('pass', 'blocked'):
                raise ValueError(f'Owner must separately accept or block {name}')
            if not isinstance(value.get('reason'), str) or not value['reason'].strip():
                raise ValueError(f'{name} needs an acceptance reason')
            evidence, inputs = value.get('evidence', []), value.get('inputs', [])
            if not isinstance(evidence, list) or not isinstance(inputs, list):
                raise ValueError('Evidence and inputs must be lists')
            if not all(isinstance(p, str) for p in evidence + inputs):
                raise ValueError('Evidence and inputs must be file or directory paths')
            if value['status'] == 'pass' and (not evidence or not inputs):
                raise ValueError(f'{name} pass requires evidence and relevant source/config inputs')
            acceptance[name] = dict(value, files=fingerprints(evidence + inputs, allow_directories=True),
                                    context=self.context())
        completed = result.get('completed_goals', {})
        latest = {g['id']: g for task in self.state['tasks'] for g in task['goals']}
        if (not isinstance(completed, dict) or any(
                goal_id not in latest or not latest[goal_id]['required']
                or not isinstance(name, str) or name not in acceptance or acceptance[name]['status'] != 'pass'
                for goal_id, name in completed.items())):
            raise ValueError('Completed goals must reference required goals and a passing acceptance')
        report = self.handoff('owner-acceptance', result.get('report', ''))
        for goal_id, name in completed.items():
            latest[goal_id]['review'] = {'acceptance': name, 'report': str(report)}
        self.state.update(acceptance=acceptance, acceptance_report=str(report), knowledge_current=False)
        self.save()

    def finish(self, result: dict):
        self.refresh()
        latest = {g['id']: g for task in self.state['tasks'] for g in task['goals']}
        if any(g['required'] and g.get('status') != 'delivered' and not g.get('review') for g in latest.values()):
            raise RuntimeError('Required goals remain unfinished; deliver a correction or blocker')
        kb = self.ws / 'knowledgebase'
        required = ['README.md', 'verification.md', 'AUTO-DECISION.md', 'AUTO-TODO.md',
                    'AUTO-FIXME.md', 'build/README.md', 'boot/README.md', 'plan/README.md']
        required += [str(p.relative_to(kb) / 'README.md') for p in kb.iterdir() if p.is_dir()] if kb.exists() else []
        for name in required:
            read_text(kb / name)
        if (result.get('knowledge_reviewed') is not True or not self.state.get('knowledge_current')
                or set(self.state['acceptance']) != {'skeleton', 'planning'}
                or any(v['status'] != 'pass' for v in self.state['acceptance'].values())):
            raise RuntimeError('Both owner acceptances and current, owner-reviewed knowledge are required')
        self.state['status'] = 'complete'
        self.save()
        # Publish the immutable prepare boundary only after owner acceptance
        # and knowledge synchronization have both passed.
        from porter.handoff import publish_handoff
        from porter.workspace import ensure_runner
        ensure_runner(self.ws)
        artifacts = [self.ws / 'project.json', self.ws / 'goals.md',
                     self.state_path, self.ws / 'runner.md']
        plan = self.ws / 'plan' / 'migration-plan.md'
        if plan.exists():
            artifacts.append(plan)
        publish_handoff(
            self.ws, 'prepare',
            summary=(self.state.get('acceptance_report') or
                     'prepare completed with owner acceptance'),
            artifacts=artifacts,
            verification=['skeleton and planning acceptance passed',
                          'knowledgebase synchronized and reviewed'],
            materials=(self.ws / 'project.json',),
        )

    def dispatch(self, request: dict):
        inputs = request.get('inputs', [])
        try:
            if not isinstance(inputs, list) or not all(isinstance(p, str) for p in inputs):
                raise ValueError('Task inputs must be a list of absolute file or directory paths')
            for name in inputs:
                path = Path(name)
                if not path.is_absolute() or not (path.is_file() or path.is_dir()):
                    raise ValueError(f'Task input must be an existing absolute file or directory: {name}')
        except (OSError, ValueError) as exc:
            self.state['task_feedback'] = {'rejected': True, 'report': str(exc)}
            self.save()
            return
        for key in ('id', 'prompt'):
            if not isinstance(request.get(key), str) or not request[key].strip():
                raise ValueError(f'Task requires {key}')
        if request.get('category') not in ('source', 'skeleton', 'planning'):
            raise ValueError('Task category must be source, skeleton or planning')
        timeout = request.get('timeout')
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout < float('inf'):
            raise ValueError('Task requires a finite positive timeout in seconds')
        goals = request.get('goals')
        if not isinstance(goals, list) or not goals:
            raise ValueError('Task requires goals')
        seen = set()
        for goal in goals:
            if not isinstance(goal, dict):
                raise ValueError('Task goal must be an object')
            for key in ('id', 'objective', 'reason', 'completion'):
                if not isinstance(goal.get(key), str) or not goal[key].strip():
                    raise ValueError(f'Task goal requires {key}')
            if type(goal.get('required')) is not bool or goal['id'] in seen:
                raise ValueError('Goals require boolean necessity and unique IDs')
            seen.add(goal['id'])
            previous = [g for task in self.state['tasks'] for g in task['goals'] if g['id'] == goal['id']]
            if previous and any(not g['required'] for g in previous):
                rejection = f"Rejected repeated supplemental goal {goal['id']}; retain its handoff and AUTO-TODO."
                path = self.handoff('task-rejected', rejection)
                self.state['task_feedback'] = {'rejected': True, 'report': rejection, 'handoff': str(path)}
                self.save()
                return
            if previous:
                if goal['required'] != previous[-1]['required']:
                    raise ValueError('An attempted goal cannot change necessity')
                retry = goal.get('retry')
                if (not isinstance(retry, dict) or not isinstance(retry.get('correction'), str)
                        or not retry['correction'].strip() or not isinstance(retry.get('evidence'), list)
                        or not retry['evidence'] or not all(isinstance(p, str) for p in retry['evidence'])):
                    raise ValueError(f"Required goal {goal['id']} retry needs failure evidence and a correction")
                fingerprints(retry['evidence'])
        goals = [{key: goal[key] for key in ('id', 'objective', 'required', 'reason', 'completion', 'retry')
                  if key in goal} | {'status': 'running'} for goal in goals]
        delivery = self.handoffs / f'{time.time_ns()}-task-{uuid.uuid4().hex[:8]}.md'
        task = {key: request[key] for key in ('id', 'category')}
        task['goals'] = goals
        task.update(status='running', handoff=str(delivery), run_id=self.state['run_id'], timeout=timeout)
        self.state['tasks'].append(task)
        self.state['task_feedback'] = {'rejected': False, 'handoff': str(delivery)}
        self.save()
        try:
            result = self.invoke('task', {'prompt': request['prompt'], 'inputs': inputs,
                                         'task_id': task['id'], 'timeout': timeout,
                                         'category': task['category'], 'goals': goals,
                                         'handoff': str(delivery)}, timeout=timeout)
            if result.get('status') not in ('delivered', 'blocked'):
                raise ValueError('Task status must be delivered or blocked')
            report = result.get('report')
            if not isinstance(report, str) or not report.strip():
                raise ValueError('Task must deliver a nonempty report')
            outcomes = result.get('goals', [])
            if (not isinstance(outcomes, list) or len(outcomes) != len(goals)
                    or any(not isinstance(g, dict) or not isinstance(g.get('id'), str)
                           or g.get('status') not in ('delivered', 'blocked')
                           for g in outcomes) or {g.get('id') for g in outcomes} != seen):
                raise ValueError('Task must deliver a result for every goal')
            for goal in goals:
                status = next(g['status'] for g in outcomes if g['id'] == goal['id'])
                goal['status'] = 'deferred' if status == 'blocked' and not goal['required'] else status
            task['status'] = result['status']
            delivery.write_text(f"# Task: {task['status']}\n\n{report}\n\nGoals: "
                                + json.dumps(goals, ensure_ascii=False) + '\n', encoding='utf-8')
        except ProviderFailure as exc:
            task['status'] = 'blocked' if any(g['required'] for g in goals) else 'deferred'
            for goal in goals:
                goal['status'] = 'blocked' if goal['required'] else 'deferred'
            with delivery.open('a', encoding='utf-8') as stream:
                stream.write(f'\n\nExecution failed: {exc}\nCause beyond the recorded evidence: unknown.\n'
                             'AUTO-TODO: defer supplemental goals until their execution prerequisites are available; '
                             'use the goal completion conditions below. Preserve partial findings.\n'
                             + json.dumps(goals, ensure_ascii=False) + '\n')
        except (OSError, ValueError, RuntimeError, KeyboardInterrupt):
            task['status'] = 'interrupted'
            for goal in goals:
                goal['status'] = 'blocked' if goal['required'] else 'deferred'
            with delivery.open('a', encoding='utf-8') as stream:
                stream.write(f"\n\nTask did not finish. Objective: {request['prompt']}\n"
                             f'Inputs: {inputs}\nProvider: {self.state.get("last_call")}\n'
                             'Any findings not recorded above remain unknown.\n'
                             'AUTO-TODO: resume supplemental work only when prerequisites permit; '
                             'use these completion conditions.\n' + json.dumps(goals, ensure_ascii=False) + '\n')
            raise
        finally:
            self.save()

    def run(self) -> int:
        try:
            for task in self.state['tasks']:
                if task['status'] == 'running':
                    task['status'] = 'interrupted'
                    for goal in task['goals']:
                        goal['status'] = 'blocked' if goal['required'] else 'deferred'
                    self.handoff('task-recovery', f"Interrupted task; partial evidence: {task['handoff']}\n"
                                 'Cause unknown. AUTO-TODO: defer supplemental goals until prerequisites permit; '
                                 'completion conditions: ' + json.dumps(task['goals'], ensure_ascii=False))
            self.save()
            self.refresh()
            self.sync_knowledge()
            while True:
                result = self.invoke('owner', {
                    'project': self.project, 'acceptance': self.state['acceptance'],
                    'previous_call': self.state.get('last_call'),
                    'acceptance_report': self.state.get('acceptance_report'),
                    'knowledge_receipt': self.state.get('knowledge_receipt'),
                    'knowledge_index': str(self.ws / 'knowledgebase/README.md'),
                    'handoff_index': [str(p) for p in sorted(self.handoffs.glob('*.md'))],
                    'goals': str(self.ws / 'goals.md'), 'answers': str(self.ws / 'answers.md'),
                    'hints': str(self.ws / 'inputs/hints'),
                    'tasks': self.state['tasks'], 'task_feedback': self.state.get('task_feedback', {}),
                })
                self.refresh()
                action = result.get('action')
                if action == 'finish':
                    self.finish(result)
                    return 0
                if action == 'accept':
                    self.accept(result)
                elif action == 'record':
                    self.handoff('owner', result.get('report', ''))
                elif action == 'task':
                    self.dispatch(result)
                elif action == 'blocked':
                    self.handoff('owner-blocked', result.get('report', ''))
                    self.sync_knowledge()
                    raise RuntimeError('Owner reported a blocker; see latest handoff')
                else:
                    raise ValueError(f'Unknown owner action: {action}')
                self.refresh()
                self.sync_knowledge()
        except KeyboardInterrupt:
            self.state.update(status='blocked', error='Interrupted')
            self.handoff('interrupted', 'Execution interrupted; inspect provider logs before resuming.')
            return 130
        except (OSError, ValueError, RuntimeError) as exc:
            self.state.update(status='blocked', error=str(exc))
            self.handoff('failure', str(exc))
            return 3
        finally:
            self.state['finished_at'] = time.time()
            self.save()
            print(f"[porter] {self.state['status']}: {self.state.get('error') or self.ws}")


def run(ws: Path, project: dict, budget: int) -> int:
    return Preparation(ws, project, budget).run()
