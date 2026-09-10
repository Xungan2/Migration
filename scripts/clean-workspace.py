#!/usr/bin/env python3
"""Preview migration cleanup; --apply deletes artifacts, --reset-target discards target edits."""
import argparse
from contextlib import ExitStack
import fcntl
from pathlib import Path
import shlex
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def clean(root, *, apply=False, reset_target=False):
    target = root / 'asterinas'
    paths = [root / 'migrations', root / 'archive', target / 'target', target / 'osdk/target']
    paths = [p for p in paths if p.exists() or p.is_symlink()]
    commands = []
    if shutil.which('docker'):
        rows = subprocess.check_output(
            ['docker', 'ps', '-a', '--format', '{{.Names}}\t{{.Image}}'], text=True)
        containers = [name for name, image in (line.split('\t') for line in rows.splitlines())
                      if image.startswith('asterinas/dev:') and (
                          name.startswith(('porter-', 'spi-nor-clean-'))
                          or name == 'migration-asterinas-dev')]
        if containers:
            commands.append(['docker', 'rm', '-f', '-v', *containers])
    if reset_target:
        entry = subprocess.check_output(
            ['git', '-C', str(root), 'ls-tree', 'HEAD', 'asterinas'], text=True).split()
        if len(entry) != 4 or entry[:2] != ['160000', 'commit']:
            raise ValueError('HEAD does not pin an asterinas submodule')
        commands += [['git', '-C', str(target), 'checkout', '--detach', '--force', entry[2]],
                     ['git', '-C', str(target), 'clean', '-fdx']]
    for command in commands:
        print(shlex.join(command), flush=True)
    for path in paths:
        print('remove', shlex.quote(str(path)), flush=True)
    if not apply:
        print('Preview only. Add --apply to execute; --reset-target discards target edits and untracked files.')
        return
    with ExitStack() as stack:
        # Refuse to remove a workspace while its Porter process still owns it.
        for directory in (root / 'migrations', root / 'archive'):
            if directory.is_symlink():
                continue
            for lock in directory.rglob('.porter.lock'):
                stream = stack.enter_context(lock.open('a'))
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ValueError(f'Stop the running Porter first: {lock.parent}') from exc
        for command in commands:
            subprocess.run(command, check=True)
        for path in paths:
            if path.is_symlink():
                path.unlink()
            elif path.exists():
                shutil.rmtree(path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='execute the displayed cleanup')
    parser.add_argument('--reset-target', action='store_true',
                        help='also discard Asterinas edits and untracked files, using the pinned commit')
    args = parser.parse_args()
    try:
        clean(ROOT, apply=args.apply, reset_target=args.reset_target)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'Cleanup failed: {exc}\n')
