"""vigil: run, status, check, catchup, log, init."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from vigil import __version__
from vigil.config import DEFAULT_CONFIG_NAME, load_config
from vigil.supervisor import DEGRADED, FAILED, MISSED, UNVERIFIED, VERIFIED, WATCHING, Supervisor

MARKS = {VERIFIED: "ok", UNVERIFIED: "??", FAILED: "xx", MISSED: "--", DEGRADED: "~~", WATCHING: ".."}

STARTER = '''# vigil.toml
# notify = "./notify.sh"   # gets a JSON problem report on stdin

[jobs.example]
command = "python3 agent.py"
every = "24h"
grace = "2h"
catchup = true
degrade_after = 3

# The job says what it did. These prove it.
[[jobs.example.checks]]
type = "file_changed"
path = "out/report.md"
within = "1h"

[[jobs.example.checks]]
type = "claim_field"
field = "records_written"
min = 1
'''


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vigil", description="Prove your scheduled agents did the work.")
    parser.add_argument("-c", "--config", help=f"path to {DEFAULT_CONFIG_NAME}")
    parser.add_argument("-V", "--version", action="version", version=f"vigil {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="run a job under supervision")
    run.add_argument("job")
    run.add_argument("--no-notify", action="store_true", help="skip the notify command")
    run.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status", help="one line per job")
    status.add_argument("job", nargs="?")
    status.add_argument("--json", action="store_true")

    check = subparsers.add_parser("check", help="report missed, unverified and degraded jobs")
    check.add_argument("--no-notify", action="store_true")
    check.add_argument("--json", action="store_true")

    catchup = subparsers.add_parser("catchup", help="re-run windows nothing covered")
    catchup.add_argument("--max", type=int, default=None, help="windows per job")
    catchup.add_argument("--json", action="store_true")

    log = subparsers.add_parser("log", help="recent runs of a job")
    log.add_argument("job")
    log.add_argument("-n", type=int, default=10)
    log.add_argument("--json", action="store_true")

    subparsers.add_parser("init", help=f"write a starter {DEFAULT_CONFIG_NAME}")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    if args.command == "init":
        return _init()

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError, KeyError) as error:
        print(f"vigil: {error}", file=sys.stderr)
        return 3

    supervisor = Supervisor(config)
    try:
        handler = {"run": _run, "status": _status, "check": _check,
                   "catchup": _catchup, "log": _log}[args.command]
        return handler(supervisor, args)
    except KeyError as error:
        print(f"vigil: {error.args[0]}", file=sys.stderr)
        return 3


def _init() -> int:
    path = Path.cwd() / DEFAULT_CONFIG_NAME
    if path.exists():
        print(f"vigil: {path} already exists", file=sys.stderr)
        return 3
    path.write_text(STARTER, encoding="utf-8")
    print(f"wrote {path}")
    return 0


def _run(supervisor: Supervisor, args) -> int:
    record = supervisor.run_job(args.job, notify=not args.no_notify)
    if args.json:
        print(json.dumps(record, indent=2))
    else:
        print(_describe_run(args.job, record))
        for check in record["checks"]:
            print(f"  [{'ok' if check['ok'] else 'xx'}] {check['type']}: {check['detail']}")
    if record["verdict"] == FAILED:
        return 1
    if record["verdict"] == UNVERIFIED or record["degraded"]:
        return 2
    return 0


def _status(supervisor: Supervisor, args) -> int:
    names = [args.job] if args.job else list(supervisor.config.jobs)
    rows, unhealthy = [], False
    for name in names:
        status = supervisor.status(name)
        unhealthy = unhealthy or not status.healthy
        last = status.last_run
        rows.append({
            "job": name,
            "schedule": status.job.schedule_label,
            "verdict": DEGRADED if status.degraded else status.verdict,
            "last_run": last["started_at"] if last else None,
            "missed": [w.isoformat() for w in status.missed],
            "next_due": status.next_due.isoformat() if status.next_due else None,
            "detail": _detail(status),
        })
    if args.json:
        print(json.dumps(rows, indent=2))
        return 1 if unhealthy else 0

    width = max((len(r["job"]) for r in rows), default=3)
    for row in rows:
        stamp = _short(row["last_run"]) if row["last_run"] else "never"
        if row["verdict"] == WATCHING:
            stamp = "no window yet"
        mark = MARKS.get(row["verdict"], "??")
        line = f"[{mark}] {row['job']:<{width}}  {stamp:<12}  {row['verdict']}"
        if row["missed"]:
            line += f", {len(row['missed'])} window(s) missed"
        print(line)
        if row["detail"]:
            print(f"     {row['detail']}")
    return 1 if unhealthy else 0


def _check(supervisor: Supervisor, args) -> int:
    problems = supervisor.scan(notify=not args.no_notify)
    if args.json:
        print(json.dumps([p.as_dict() for p in problems], indent=2))
    elif not problems:
        print(f"[ok] {len(supervisor.config.jobs)} job(s), nothing outstanding")
    else:
        for problem in problems:
            print(f"[{MARKS.get(problem.kind, '??')}] {problem.job}: {problem.kind} - {problem.detail}")
    return 1 if problems else 0


def _catchup(supervisor: Supervisor, args) -> int:
    result = supervisor.catchup(limit=args.max)
    if args.json:
        print(json.dumps({"runs": result.runs, "skipped": result.skipped}, indent=2))
        return 0
    if not result:
        print("[ok] nothing to catch up")
        return 0
    for record in result.runs:
        window = _short(record["catchup_for"])
        print(f"[{MARKS.get(record['verdict'], '??')}] caught up window {window}: {record['verdict']}")
    for name, count in result.skipped.items():
        print(f"[--] {name}: wrote off {count} window(s) too old to re-run")
    return 0 if all(r["verdict"] == VERIFIED for r in result.runs) else 2


def _log(supervisor: Supervisor, args) -> int:
    supervisor.config.job(args.job)
    runs = supervisor.store.runs(args.job, limit=args.n)
    if args.json:
        print(json.dumps(runs, indent=2))
        return 0
    if not runs:
        print(f"no runs recorded for {args.job}")
        return 0
    for record in runs:
        print(_describe_run(args.job, record))
    return 0


def _describe_run(name: str, record: dict) -> str:
    mark = MARKS.get(DEGRADED if record.get("degraded") else record["verdict"], "??")
    stamp = _short(record["started_at"])
    claim = json.dumps(record["claim"]) if record["claim"] else "no claim"
    suffix = " (catch-up)" if record.get("catchup_for") else ""
    tail = f" degraded, {record['verdict']}" if record.get("degraded") else f" {record['verdict']}"
    return f"[{mark}] {stamp} {name}{suffix}:{tail}, claimed {claim}"


def _detail(status) -> str:
    if status.missed:
        return f"oldest missed window {status.missed[0]:%Y-%m-%d %H:%M}"
    if status.last_run and status.last_run["verdict"] != VERIFIED:
        from vigil.supervisor import _why
        return _why(status.last_run)
    if status.degraded:
        return "output has not changed"
    return ""


def _short(stamp: str | None) -> str:
    if not stamp:
        return "never"
    try:
        return f"{datetime.fromisoformat(stamp):%m-%d %H:%M}"
    except ValueError:
        return stamp


if __name__ == "__main__":
    raise SystemExit(main())
