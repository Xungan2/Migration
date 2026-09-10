"""Validated workspace inputs, atomic state and one active workflow per workspace."""
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import tempfile

MODE = 'unified-p01'


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        except BaseException:
            temporary.unlink()
            raise
    temporary.replace(path)


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError(f'Expected an object: {path}')
    return value


def read_text(path: Path) -> str:
    if not path.is_file():
        raise ValueError(f'Not a regular file: {path}')
    text = path.read_text(encoding='utf-8')
    if not text.strip():
        raise ValueError(f'Empty file: {path}')
    return text


@contextmanager
def locked(ws: Path):
    ws.mkdir(parents=True, exist_ok=True)
    with (ws / '.porter.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(f'Workspace already running: {ws}') from exc
        yield


def prepare(args) -> dict:
    ws = Path(args.output_dir).resolve()
    path = ws / 'project.json'
    project = read_json(path) if path.exists() else {}
    if project and project.get('mode') != MODE:
        raise ValueError('Use a new workspace for unified preparation; legacy workspaces remain untouched.')
    if not project and any(p.name != '.porter.lock' for p in ws.iterdir()):
        raise ValueError(f'Workspace is not empty: {ws}')
    identity = {}
    for key in ('linux_driver', 'target_os'):
        supplied = getattr(args, key)
        value = str(Path(supplied).resolve()) if supplied else project.get(key)
        if not value or not Path(value).is_dir():
            raise ValueError(f'{key} must name an existing directory')
        if project and value != project[key]:
            raise ValueError(f'Workspace identity differs: {key}')
        identity[key] = value
    source, target = Path(identity['linux_driver']), Path(identity['target_os'])
    if not any(source.rglob('*.[ch]')):
        raise ValueError('Source driver contains no C source or headers')
    # A unique temporary file never overwrites an existing target-tree file.
    with tempfile.TemporaryFile(dir=target):
        pass
    materials = ([str(Path(p).resolve()) for p in args.materials]
                 if args.materials is not None else project.get('materials', []))
    if any(not Path(p).exists() for p in materials):
        raise ValueError('A material path does not exist')
    intent = read_text(Path(args.intent_file)) if args.intent_file else None
    category = args.category if args.category is not None else project.get('category')
    if category is not None and not category.strip():
        raise ValueError('category must not be empty')
    hints = {}
    if args.hints_dir:
        directory = Path(args.hints_dir)
        if not directory.is_dir():
            raise ValueError('hints-dir must be a directory')
        hints = {p.name: read_text(p) for p in directory.glob('*.md')}
    project.update(mode=MODE, **identity, materials=materials, category=category)
    if intent is not None:
        (ws / 'goals.md').write_text(intent, encoding='utf-8')
        project['intent_source'] = str(Path(args.intent_file).resolve())
    for name, text in hints.items():
        (ws / 'inputs/hints').mkdir(parents=True, exist_ok=True)
        (ws / 'inputs/hints' / name).write_text(text, encoding='utf-8')
    write_json(path, project)
    return project
