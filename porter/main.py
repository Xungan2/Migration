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
    prepare = commands.add_parser('prepare', help='build/load skeleton and suggest migration plan')
    prepare.add_argument('--output-dir', required=True)
    prepare.add_argument('--linux-driver')
    prepare.add_argument('--target-os')
    prepare.add_argument('--materials', action='append')
    prepare.add_argument('--intent-file')
    prepare.add_argument('--hints-dir')
    prepare.add_argument('--category')
    prepare.add_argument('--budget', type=int, default=3600, help='total wall budget in seconds')
    prepare.add_argument('--prepare-only', '--t1-only', action='store_true')
    pre_mono = commands.add_parser('pre-mono', help='prepare executable mono inputs')
    pre_mono.add_argument('--output-dir', required=True)
    args = parser.parse_args(argv)
    try:
        if args.budget <= 0:
            raise ValueError('budget must be positive')
        ws = Path(args.output_dir).resolve()
        with workspace.locked(ws):
            if args.command == 'pre-mono':
                from porter.pre_mono import run
                rc = run(ws)
                workspace.append_runbook(ws, 'pre-mono', rc, sys.argv,
                                         '- 产物：`mono-input-manifest.json`、`mono-input-report.md`')
                return rc
            project = workspace.prepare(args)
            if args.prepare_only:
                print(f'[porter] Inputs ready: {ws}; no acceptance performed.')
                workspace.append_runbook(ws, 'prepare', 0, sys.argv,
                                         f'- 产物：`{ws / "project.json"}`、`{ws / "goals.md"}`\n- 阶段结论：`PASS`')
                return 0
            from porter.workflow import run
            rc = run(ws, project, args.budget)
            state = ws / 'prepare' / 'state.json'
            workspace.append_runbook(ws, 'prepare', rc, sys.argv,
                                     f'- 产物：`{state}`\n- 阶段结论：`{"PASS" if rc == 0 else "BLOCKED"}`')
            return rc
    except (OSError, ValueError, UnicodeError) as exc:
        print(f'[porter] {exc}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
