"""opencode JSONL transport with durable logs and bounded process lifetime."""
import json
import os
from pathlib import Path
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
    stem.with_suffix('.prompt.md').write_text(prompt, encoding='utf-8')
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
    stem.with_suffix('.log').write_text(output, encoding='utf-8')
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
    return rc, (texts[-1] if texts else '') if rc == 0 else output + stderr, session


def response(text: str) -> dict:
    text = text.strip()
    # Allow introductory prose, but decode the entire first structured block.
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if line.lstrip().startswith(('{', '[', '```')):
            text = ''.join(lines[index:]).strip()
            break
    if text.startswith('```json\n') and text.endswith('```'):
        text = text[8:-3].strip()
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError('Provider response must be an object')
    return value
