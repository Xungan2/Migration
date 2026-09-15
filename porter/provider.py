"""opencode JSONL transport with durable logs and bounded process lifetime."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess

SKILLS = Path(__file__).resolve().parent / 'skills'
DEFAULT_MODEL = 'zhipu-ai/glm-5.3-flash'


def run(role: str, data: dict, target: Path, stem: Path, timeout: float,
        session: str | None = None) -> tuple[int, str, str | None]:
    prompt = (SKILLS / f'prepare-{role}.md').read_text(encoding='utf-8')
    if role == 'task':
        category = data.get('category')
        if category not in ('source', 'skeleton', 'planning'):
            raise ValueError('Unknown task skill')
        prompt += '\nTASK SKILL\n' + (SKILLS / f'prepare-task-{category}.md').read_text(encoding='utf-8')
    prompt += '\nTASK INPUT\n' + json.dumps(data, ensure_ascii=False)
    stem.parent.mkdir(parents=True, exist_ok=True)
    prompt_path = stem.with_suffix('.prompt.md')
    log_path = stem.with_suffix('.log')
    prompt_path.write_text(prompt, encoding='utf-8')
    # The unified log is the monitor's input.  Keep this low-level transport
    # observable even when callers do not bind the log context themselves.
    try:
        from .log import append_event
        append_event('agent_start', intent=str(stem), cmd=prompt,
                     summary=f'role={role}' + (f' session={session}' if session else ''),
                     ws=Path(data['workspace']), run_id=str(stem),
                     ref={'log': str(log_path), 'prompt': str(prompt_path)},
                     phase=role, task_id=data.get('task_id'),
                     session_id=session)
    except Exception:
        pass
    args = ['opencode', 'run', '--auto', '--format', 'json', '--model',
            os.environ.get('PORTER_MODEL', DEFAULT_MODEL), '--dir', str(target)]
    if session:
        args += ['--session', session]
    env = dict(os.environ, NO_COLOR='1', PORTER_WORKSPACE=data['workspace'],
               PORTER_TARGET_OS_ROOT=str(target), PORTER_ROLE=role)
    env.setdefault('OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX', '131072')
    stdout, stderr, rc = '', '', 127
    try:
        with subprocess.Popen(args, cwd=target, env=env, text=True,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, start_new_session=True) as proc:
            try:
                stdout, stderr = proc.communicate(prompt, timeout=timeout)
                rc = proc.returncode
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                rc = 130 if isinstance(exc, KeyboardInterrupt) else 124
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    stdout, stderr = proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    stdout, stderr = proc.communicate()
                stderr += '\nINTERRUPTED' if rc == 130 else '\nTIMEOUT'
            finally:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
    except OSError as exc:
        stderr = str(exc)
    output = stdout + '\n' + stderr
    log_path.write_text(output, encoding='utf-8')
    texts = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if isinstance(event.get('sessionID'), str):
            session = event['sessionID']
        if event.get('type') == 'error':
            rc = rc or 1
            stderr += '\n' + json.dumps(event, ensure_ascii=False)
        part = event.get('part')
        if event.get('type') == 'text' and isinstance(part, dict) and isinstance(part.get('text'), str):
            texts.append(part['text'])
    try:
        from .log import append_event
        append_event('agent_end', intent=str(stem), rc=rc,
                     summary=(output or '')[-300:].strip().replace('\n', ' ⏎ '),
                     ws=Path(data['workspace']), run_id=str(stem), phase=role,
                     task_id=data.get('task_id'), session_id=session)
    except Exception:
        pass
    text = texts[-1] if texts else ''
    if rc == 0:
        try:
            response(text)
        except ValueError:
            # Keep split deliveries and a delivery followed by commentary available
            # to the parser. Ambiguity is returned to the agent for correction.
            text = ''.join(texts)
    return rc, text if rc == 0 else output + stderr, session


def response(text: str) -> dict:
    # Decode the first structured block, never a nested object in a broken one.
    start = re.search(r'```|[\[{]', text)
    if start is None:
        raise ValueError('Provider response must contain a JSON object')
    text = text[start.start():].strip()
    fenced = text.startswith('```')
    if fenced:
        opening, separator, text = text.partition('\n')
        if opening.strip().lower() not in ('```', '```json') or not separator:
            raise ValueError('Provider response needs a plain or JSON code fence')
    value, end = json.JSONDecoder().raw_decode(text.lstrip())
    remainder = text.lstrip()[end:].strip()
    if fenced:
        if not remainder.startswith('```'):
            raise ValueError('Provider response has an unclosed code fence')
        remainder = remainder[3:]
    if re.search(r'[{}\[\]]|```', remainder):
        raise ValueError('Provider response contains ambiguous structured output')
    if not isinstance(value, dict):
        raise ValueError('Provider response must be an object')
    return value
