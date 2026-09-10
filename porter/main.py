"""Public CLI for unified migration preparation."""
import argparse
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from porter import workspace


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='porter')
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare', aliases=['p0'], help='build/load skeleton and suggest migration plan')
    prepare.add_argument('--output-dir', required=True)
    prepare.add_argument('--linux-driver')
    prepare.add_argument('--target-os')
    prepare.add_argument('--materials', action='append')
    prepare.add_argument('--intent-file')
    prepare.add_argument('--hints-dir')
    prepare.add_argument('--category')
    prepare.add_argument('--budget', type=int, default=3600, help='total wall budget in seconds')
    prepare.add_argument('--prepare-only', '--t1-only', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.budget <= 0:
            raise ValueError('budget must be positive')
        ws = Path(args.output_dir).resolve()
        with workspace.locked(ws):
            project = workspace.prepare(args)
            if args.prepare_only:
                print(f'[porter] Inputs ready: {ws}; no acceptance performed.')
                return 0
            from porter.workflow import run
            return run(ws, project, args.budget)
    except (OSError, ValueError, UnicodeError) as exc:
        print(f'[porter] {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
