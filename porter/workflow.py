"""One owner, on-demand tasks, continuing knowledge maintenance; Markdown evidence."""
import hashlib
import json
from pathlib import Path
import time
import uuid

from porter import provider
from porter.artifacts import PREPARE_ARTIFACTS, locate
from porter.workspace import read_json, read_text, write_json


class ProviderFailure(RuntimeError):
    """An execution failure, distinct from storage or budget failures."""


class DeliveryError(ValueError):
    """Agent-correctable delivery or acceptance error."""


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
            raise DeliveryError(f'Evidence/input must be an absolute regular file: {name}')
        if not path.stat().st_size:
            raise DeliveryError(f'Empty evidence/input: {path}')
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
            raise DeliveryError(f'{role} must deliver a nonempty report')
        path = self.handoffs / f'{time.time_ns()}-{role}-{uuid.uuid4().hex[:8]}.md'
        path.write_text(report + '\n', encoding='utf-8')
        return path

    def feedback(self, role: str, error: Exception, response=None):
        self.state['feedback'] = {
            'role': role, 'report': str(error), 'response': response,
            'call': self.state.get('last_call'),
        }
        self.save()

    def invoke(self, role: str, data: dict, timeout: float | None = None) -> dict:
        previous_feedback = self.state.get('feedback', {})
        deadline = min(self.deadline, time.monotonic() + timeout) if timeout is not None else self.deadline
        for attempt in range(1 if role == 'owner' else 2):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if time.monotonic() >= self.deadline:
                    raise RuntimeError('Total preparation budget exhausted')
                raise ProviderFailure(f'{role} delivery timeout')
            text = self._invoke(role, data, remaining)
            try:
                result = provider.response(text)
                if role in ('task', 'knowledge'):
                    statuses = ('delivered', 'blocked') if role == 'task' else ('updated', 'needs-owner')
                    if result.get('status') not in statuses:
                        raise DeliveryError(f'{role} status must be one of {statuses}')
                    if not isinstance(result.get('report'), str) or not result['report'].strip():
                        raise DeliveryError(f'{role} must deliver a nonempty report')
                if role == 'task':
                    outcomes = result.get('goals')
                    expected = {g['id'] for g in data['goals']}
                    if (not isinstance(outcomes, list) or len(outcomes) != len(expected)
                            or any(not isinstance(g, dict) or not isinstance(g.get('id'), str)
                                   or g.get('status') not in ('delivered', 'blocked') for g in outcomes)
                            or {g['id'] for g in outcomes} != expected):
                        raise DeliveryError('Task must deliver a result for every goal')
                if attempt:
                    self.state['feedback'] = previous_feedback
                    self.save()
                return result
            except ValueError as exc:
                self.feedback(role, exc, text)
                if role == 'owner' or attempt:
                    raise DeliveryError(str(exc)) from exc
                data = dict(data, feedback=self.state['feedback'], correction_only=True)

    def _invoke(self, role: str, data: dict, timeout: float) -> str:
        # A task's own checkpoint stays writable during its delivery correction.
        handoffs_before = {str(p): digest(p) for p in self.handoffs.glob('*.md')
                           if p.is_file() and (role != 'task' or str(p) != data.get('handoff'))}
        kb = self.ws / 'knowledgebase'
        knowledge_before = {str(p): digest(p) for p in kb.rglob('*') if p.is_file()}
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('Total preparation budget exhausted')
        if timeout is not None:
            remaining = min(remaining, timeout)
        stem = self.root / 'logs' / f'{time.time_ns()}-{role}'
        task_id = data.get('task_id') if role == 'task' else None
        session_key = f'task:{task_id}' if task_id else role
        session = self.state['sessions'].get(session_key)
        data = dict(data, role=role, workspace=str(self.ws))
        self.state['last_call'] = {'role': role, 'log': str(stem) + '.log'}
        self.save()
        rc, text, session = provider.run(role, data, self.target, stem, remaining, session)
        self.state['sessions'][session_key] = session
        if role == 'task' and task_id:
            for task in reversed(self.state.get('tasks', [])):
                if task.get('id') == task_id:
                    task['session_id'] = session
                    task['log'] = str(stem.with_suffix('.log'))
                    break
        self.save()
        if rc == 130:
            raise KeyboardInterrupt
        if rc:
            # Only an explicitly unavailable session gets one fresh attempt.
            lower = text.lower()
            if role != 'task' and session and 'session' in lower and ('not found' in lower or 'does not exist' in lower):
                self.state['sessions'][session_key] = None
                self.save()
                remaining = self.deadline - time.monotonic()
                if remaining > 0:
                    rc, text, session = provider.run(role, data, self.target,
                                                    Path(str(stem) + '-fresh'), remaining)
                    self.state['sessions'][session_key] = session
                    self.save()
            if rc == 130:
                raise KeyboardInterrupt
        handoffs_after = {str(p): digest(p) for p in self.handoffs.glob('*.md') if p.is_file()}
        changed = {name: {'before': value, 'after': handoffs_after.get(name)}
                   for name, value in handoffs_before.items() if handoffs_after.get(name) != value}
        if role != 'knowledge':
            knowledge_after = {str(p): digest(p) for p in kb.rglob('*') if p.is_file()}
            changed.update({name: {'before': knowledge_before.get(name), 'after': knowledge_after.get(name)}
                            for name in knowledge_before.keys() | knowledge_after.keys()
                            if knowledge_before.get(name) != knowledge_after.get(name)})
        if changed:
            self.handoff('workspace-change',
                         f'Files changed during {role}; review affected evidence and reconcile knowledge.\n'
                         f'Logs: {stem}.log\n' + json.dumps(changed, ensure_ascii=False, indent=2))
            for name in handoffs_before.keys() - handoffs_after.keys():
                self.state['incorporated'].pop(name, None)
            self.state['knowledge_current'] = False
            self.save()
        if time.monotonic() >= self.deadline:
            raise RuntimeError('Total preparation budget exhausted')
        if rc:
            raise ProviderFailure(f'{role} provider failed ({rc}); logs: {stem}.log')
        return text

    def sync_knowledge(self):
        removed = [name for name in self.state['incorporated'] if not Path(name).is_file()]
        if removed:
            self.handoff('workspace-change', 'Previously incorporated handoffs are missing; reconcile knowledge:\n'
                         + '\n'.join(removed))
            for name in removed:
                self.state['incorporated'].pop(name)
        pending = {str(p): digest(p) for p in sorted(self.handoffs.glob('*.md'))
                   if p.is_file() and self.state['incorporated'].get(str(p)) != digest(p)}
        if not pending:
            return
        self.state['knowledge_current'] = False
        self.save()
        try:
            result = self.invoke('knowledge', {
                'handoffs': list(pending), 'index': str(self.ws / 'knowledgebase/README.md'),
                'acceptance': self.state['acceptance'],
                'previous_receipt': self.state.get('knowledge_receipt'),
            })
        except (DeliveryError, ProviderFailure) as exc:
            if isinstance(exc, ProviderFailure):
                self.feedback('knowledge', exc)
            result = {'status': 'needs-owner', 'report': str(exc)}
        receipt = self.root / 'knowledge' / f'{time.time_ns()}.md'
        receipt.parent.mkdir(exist_ok=True)
        receipt.write_text(str(result.get('report', '')) + '\n', encoding='utf-8')
        self.state['knowledge_receipt'] = str(receipt)
        if result.get('status') == 'needs-owner' and str(result.get('report', '')).strip():
            self.save()
            return
        current = {str(p): digest(p) for p in self.handoffs.glob('*.md') if p.is_file()}
        # Changes made during maintenance belong to the next batch, including
        # the change notice. An updated receipt only covers unchanged inputs.
        self.state['incorporated'].update({name: value for name, value in pending.items()
                                           if current.get(name) == value})
        self.state['knowledge_current'] = current == self.state['incorporated']
        self.save()

    def accept(self, result: dict):
        acceptance = {}
        for name in ('skeleton', 'planning'):
            value = result.get(name)
            if not isinstance(value, dict) or value.get('status') not in ('pass', 'blocked'):
                raise DeliveryError(f'Owner must separately accept or block {name}')
            if not isinstance(value.get('reason'), str) or not value['reason'].strip():
                raise DeliveryError(f'{name} needs an acceptance reason')
            evidence, inputs = value.get('evidence', []), value.get('inputs', [])
            if not isinstance(evidence, list) or not isinstance(inputs, list):
                raise DeliveryError('Evidence and inputs must be lists')
            if not all(isinstance(p, str) for p in evidence + inputs):
                raise DeliveryError('Evidence and inputs must be file or directory paths')
            if value['status'] == 'pass' and (not evidence or not inputs):
                raise DeliveryError(f'{name} pass requires evidence and relevant source/config inputs')
            if name == 'planning' and value['status'] == 'pass':
                # Prepare planning is valid only when its markdown plan exists.
                try:
                    locate(self.ws, required=PREPARE_ARTIFACTS)
                except ValueError as exc:
                    raise DeliveryError(str(exc)) from exc
            acceptance[name] = dict(value, files=fingerprints(evidence + inputs, allow_directories=True),
                                    context=self.context())
        completed = result.get('completed_goals', {})
        latest = {g['id']: g for task in self.state['tasks'] for g in task['goals']}
        if (not isinstance(completed, dict) or any(
                goal_id not in latest
                or not isinstance(name, str) or name not in acceptance or acceptance[name]['status'] != 'pass'
                for goal_id, name in completed.items())):
            raise DeliveryError('Completed goals must reference known goals and a passing acceptance')
        report = self.handoff('owner-acceptance', result.get('report', ''))
        for goal_id, name in completed.items():
            latest[goal_id]['review'] = {'acceptance': name, 'report': str(report)}
        self.state.update(acceptance=acceptance, acceptance_report=str(report), knowledge_current=False)
        self.save()

    def finish(self, result: dict):
        try:
            artifact_paths = locate(self.ws, required=PREPARE_ARTIFACTS)
        except ValueError as exc:
            raise DeliveryError(str(exc)) from exc
        self.state['artifacts'] = {
            name: str(path.relative_to(self.ws))
            for name, path in artifact_paths.items()
        }
        self.save()
        self.refresh()
        kb = self.ws / 'knowledgebase'
        required = ['README.md', 'verification.md', 'AUTO-DECISION.md', 'AUTO-TODO.md',
                    'AUTO-FIXME.md', 'build/README.md', 'boot/README.md', 'plan/README.md']
        required += [str(p.relative_to(kb) / 'README.md') for p in kb.iterdir() if p.is_dir()] if kb.exists() else []
        for name in required:
            try:
                read_text(kb / name)
            except ValueError as exc:
                raise DeliveryError(str(exc)) from exc
        if (result.get('knowledge_reviewed') is not True or not self.state.get('knowledge_current')
                or set(self.state['acceptance']) != {'skeleton', 'planning'}
                or any(v['status'] != 'pass' for v in self.state['acceptance'].values())):
            raise DeliveryError('Both owner acceptances and current, owner-reviewed knowledge are required; '
                                + json.dumps({'acceptance': self.state['acceptance'],
                                              'knowledge_current': self.state.get('knowledge_current'),
                                              'knowledge_reviewed': result.get('knowledge_reviewed')}, ensure_ascii=False))
        self.state.update(status='complete', finished_at=time.time())
        self.save()
        # Publish the immutable prepare boundary only after owner acceptance
        # and knowledge synchronization have both passed.
        from porter.handoff import publish_handoff
        from porter.workspace import ensure_runner
        ensure_runner(self.ws)
        # Plans are mutable input for pre-mono, so keep them out of the
        # immutable prepare artifact fingerprint. Their locations live in the
        # workspace state index instead.
        artifacts = [self.ws / 'project.json', self.ws / 'goals.md',
                     self.state_path, self.ws / 'runner.md']
        publish_handoff(
            self.ws, 'prepare',
            summary=(self.state.get('acceptance_report') or
                     'prepare completed with owner acceptance'),
            artifacts=artifacts,
            verification=['skeleton and planning acceptance passed',
                          'knowledgebase synchronized and reviewed'],
            materials=(self.ws / 'project.json', artifact_paths['migration-plan.md']),
        )

    def dispatch(self, request: dict):
        inputs = request.get('inputs', [])
        try:
            if not isinstance(inputs, list) or not all(isinstance(p, str) for p in inputs):
                raise DeliveryError('Task inputs must be a list of absolute file or directory paths')
            for name in inputs:
                path = Path(name)
                if not path.is_absolute() or not (path.is_file() or path.is_dir()):
                    raise DeliveryError(f'Task input must be an existing absolute file or directory: {name}')
        except DeliveryError as exc:
            self.state['task_feedback'] = {'rejected': True, 'report': str(exc)}
            self.feedback('owner', exc, request)
            return
        for key in ('id', 'prompt'):
            if not isinstance(request.get(key), str) or not request[key].strip():
                raise DeliveryError(f'Task requires {key}')
        if request.get('category') not in ('source', 'skeleton', 'planning'):
            raise DeliveryError('Task category must be source, skeleton or planning')
        timeout = request.get('timeout')
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout < float('inf'):
            raise DeliveryError('Task requires a finite positive timeout in seconds')
        goals = request.get('goals')
        if not isinstance(goals, list) or not goals:
            raise DeliveryError('Task requires goals')
        seen = set()
        for goal in goals:
            if not isinstance(goal, dict):
                raise DeliveryError('Task goal must be an object')
            for key in ('id', 'objective', 'reason', 'completion'):
                if not isinstance(goal.get(key), str) or not goal[key].strip():
                    raise DeliveryError(f'Task goal requires {key}')
            if type(goal.get('required')) is not bool or goal['id'] in seen:
                raise DeliveryError('Goals require boolean necessity and unique IDs')
            seen.add(goal['id'])
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
            report = result['report']
            outcomes = result['goals']
            for goal in goals:
                status = next(g['status'] for g in outcomes if g['id'] == goal['id'])
                goal['status'] = 'deferred' if status == 'blocked' and not goal['required'] else status
            task['status'] = result['status']
            with delivery.open('a', encoding='utf-8') as stream:
                stream.write(f"\n\n# Task: {task['status']}\n\n{report}\n\nGoals: "
                             + json.dumps(goals, ensure_ascii=False) + '\n')
        except (ProviderFailure, DeliveryError) as exc:
            if isinstance(exc, ProviderFailure):
                self.feedback('task', exc)
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
                try:
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
                        'feedback': self.state.get('feedback', {}),
                        'knowledge_current': self.state.get('knowledge_current', False),
                    })
                except DeliveryError:
                    continue
                self.state.pop('feedback', None)
                self.refresh()
                try:
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
                        raise DeliveryError(f'Unknown owner action: {action}')
                except DeliveryError as exc:
                    self.feedback('owner', exc, result)
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
            # Successful state is already fingerprinted by the published handoff.
            if self.state['status'] != 'complete':
                self.state['finished_at'] = time.time()
                self.save()
            print(f"[porter] {self.state['status']}: {self.state.get('error') or self.ws}")


def run(ws: Path, project: dict, budget: int) -> int:
    return Preparation(ws, project, budget).run()
