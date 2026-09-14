"""Public CLI for unified migration preparation."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from porter import workspace


def _start_monitor(ws: Path, argv) -> object | None:
    """Start one detached monitor and persist enough context for recovery."""
    if os.environ.get("PORTER_NO_MONITOR") or os.environ.get("PORTER_MONITOR_CHILD"):
        return None
    from porter.monitor import start
    pid_file = ws / ".monitor.pid"
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            cmdline = Path(f"/proc/{pid}/cmdline")
            if b"porter.monitor" in cmdline.read_bytes():
                return None
        except (OSError, ValueError):
            try:
                pid_file.unlink()
            except OSError:
                pass
    # A marker belongs to the previous invocation; leaving it would make a
    # fresh monitor mistake a later owner crash for a clean exit.
    try:
        (ws / ".porter-exit.json").unlink()
    except FileNotFoundError:
        pass
    workspace.write_json(ws / ".porter-command.json", {
        "argv": list(argv), "cwd": str(Path.cwd()), "owner_pid": os.getpid(),
        "started_at": __import__("time").time()})
    try:
        return start(ws, owner_pid=os.getpid())
    except OSError:
        return None


def _finish_monitor(ws: Path, rc: int | None, *, stop: bool = False) -> None:
    """Publish a normal-exit marker; crash recovery relies on its absence."""
    if stop:
        try:
            workspace.write_json(ws / ".porter-exit.json", {
                "rc": rc, "finished_at": __import__("time").time()})
        except OSError:
            pass
    try:
        from porter.monitor import discover_tasks, write_taskboard
        write_taskboard(ws, discover_tasks(ws),
                        monitor_status="stopped" if stop else "idle")
    except (OSError, ValueError, UnicodeError):
        pass


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
    pre_mono = commands.add_parser('pre-mono', aliases=['prepare-mono'],
                                   help='agent-owned preparation of mono inputs')
    pre_mono.add_argument('--output-dir', required=True)
    mono = commands.add_parser('mono', help='migrate modules from pre-mono inputs')
    mono.add_argument('--output-dir', required=True)
    mono.add_argument('--module')
    mono.add_argument('--module-research')
    mono.add_argument('--session')
    mono.add_argument('--budget', type=int)
    mono.add_argument('--budget-research', type=int)
    mono.add_argument('--budget-translate', type=int)
    accept = commands.add_parser('accept', help='draft the migration acceptance standard (agent s1-s7 as JSON+check.py pairs, human gates, seven-section index); --execute runs the fix loop against the frozen ladder')
    accept.add_argument('--output-dir', required=True)
    accept.add_argument('--tier', choices=['t1', 'inject', 'e2e'],
                        help='force re-run one tier (default: auto-route)')
    accept.add_argument('--execute', action='store_true',
                        help='execute phase: agent fix loop against the '
                             'frozen acceptance ladder (s1..s7 in order, '
                             'fail-fast)')
    accept.add_argument('--budget', type=int)
    accept.add_argument('--session')
    monitor = commands.add_parser('monitor', help='run the background task monitor')
    monitor.add_argument('--output-dir', required=True)
    monitor.add_argument('--interval', type=float, default=5.0)
    monitor.add_argument('--budget', type=int, default=1)
    monitor.add_argument('--once', action='store_true')
    args = parser.parse_args(argv)
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    if args.command == 'monitor':
        from porter.monitor import Monitor
        return Monitor(Path(args.output_dir), interval=args.interval,
                       budget=args.budget).run(once=args.once)
    monitor_ws = None
    monitor_proc = None
    stop_monitor = False
    result = None
    try:
        if getattr(args, 'budget', None) is not None and args.budget <= 0:
            raise ValueError('budget must be positive')
        ws = Path(args.output_dir).resolve()
        with workspace.locked(ws):
            # prepare must validate the initially empty workspace before the
            # monitor's derived files are created.
            if args.command == 'prepare':
                project = workspace.prepare(args)
                monitor_ws = ws
                monitor_proc = _start_monitor(ws, raw_argv)
                if args.prepare_only:
                    stop_monitor = True
                    print(f'[porter] Inputs ready: {ws}; no acceptance performed.')
                    result = 0
                    return result
                from porter.workflow import run
                result = run(ws, project, args.budget)
                stop_monitor = result in (0, 2)
                return result
            monitor_ws = ws
            monitor_proc = _start_monitor(ws, raw_argv)
            if args.command in ('pre-mono', 'prepare-mono'):
                from porter.pre_mono import run
                rc = run(ws)
                result = rc
                stop_monitor = rc in (0, 2)
                return rc
            if args.command == 'mono':
                from porter.exp.mono import run_exp_mono
                result = run_exp_mono(ws, module=args.module,
                                      budget=args.budget,
                                      session=args.session,
                                      budget_research=args.budget_research,
                                      budget_translate=args.budget_translate,
                                      module_research=args.module_research)
                stop_monitor = result in (0, 2)
                return result
            if args.command == 'accept':
                from porter.exp.accept import run_accept
                rc = run_accept(ws, tier=args.tier,
                                budget=args.budget, session=args.session,
                                execute=args.execute)
                workspace.append_runner(
                    ws, 'accept', rc, sys.argv,
                    '- 产物：`exp-accept/acceptance/` 下七节文件对'
                    '（N-slug.json + N-slug.check.py；§5-§7 agent 设计，'
                    '§1-§4 由 Tier1 agent 从 mono 事实提取）'
                    '+ 关口放行后 ledger 七节索引；'
                    '--execute 时另有 `exp-accept/run-report.md`（终态'
                    '报告）/ `execute-panic.md`（标准争议人工介入）')
                result = rc
                stop_monitor = bool(args.execute)
                if not stop_monitor:
                    stop_monitor = rc in (0, 2)
                return rc
    except (OSError, ValueError, UnicodeError) as exc:
        print(f'[porter] {exc}', file=sys.stderr)
        result = 2
        return result
    except KeyboardInterrupt:
        result = 130
        return result
    finally:
        if monitor_ws is not None and not os.environ.get("PORTER_NO_MONITOR") \
                and not os.environ.get("PORTER_MONITOR_CHILD"):
            _finish_monitor(monitor_ws, result, stop=stop_monitor)
        if monitor_proc is not None and result in (0, 2) and stop_monitor:
            # Successful phases have no recovery work left.  Failed phases
            # deliberately leave the child alive so it can inspect logs after
            # the host releases the workspace lock.
            try:
                monitor_proc.terminate()
                monitor_proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired, TimeoutError):
                try:
                    monitor_proc.kill()
                except OSError:
                    pass
            try:
                (monitor_ws / ".monitor.pid").unlink()
            except OSError:
                pass


if __name__ == '__main__':
    sys.exit(main())
