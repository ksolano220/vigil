"""Six days of a job that never fails and stops working on day two."""

import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vigil.config import load_config
from vigil.store import Store
from vigil.supervisor import VERIFIED, Supervisor

HERE = Path(__file__).parent
TICK = 1.3


def reset():
    for path in [HERE / "out", HERE / ".vigil"]:
        shutil.rmtree(path, ignore_errors=True)
    shutil.copy(HERE / "feed.json.seed", HERE / "feed.json")


def main():
    reset()
    config = load_config(HERE / "vigil.toml")
    supervisor = Supervisor(config, store=Store(config.resolved_state_path()))
    cron_view, vigil_view = [], []

    print("day 1  the source is healthy")
    record = supervisor.run_job("digest")
    cron_view.append(record["exit_code"])
    vigil_view.append(_verdict(record))
    _show(1, record)

    (HERE / "feed.json").unlink()  # the API token expires overnight
    for day in (2, 3, 4):
        time.sleep(TICK)
        record = supervisor.run_job("digest")
        cron_view.append(record["exit_code"])
        vigil_view.append(_verdict(record))
        _show(day, record)

    print("\nday 5  the laptop was asleep, nothing ran")
    time.sleep(TICK * 2)
    problems = supervisor.scan()
    for problem in problems:
        print(f"       vigil: {problem.kind} - {problem.detail}")

    print("\nday 6  catch-up runs the window nothing covered")
    for record in supervisor.catchup().runs:
        cron_view.append(record["exit_code"])
        vigil_view.append(_verdict(record))
        _show(6, record)

    print("\n" + "-" * 62)
    print(f"cron saw   : {len(cron_view)} runs, exit codes {cron_view}, zero alerts")
    print(f"vigil saw  : {', '.join(vigil_view)}")
    print(f"verified   : {vigil_view.count(VERIFIED)} of {len(vigil_view)}")
    print("-" * 62)
    print("The agent never crashed. It stopped working on day 2 and said 'ok' four more times.")


def _verdict(record):
    return f"{record['verdict']}+degraded" if record.get("degraded") else record["verdict"]


def _show(day, record):
    failed = [c for c in record["checks"] if not c["ok"]]
    print(f"       exit {record['exit_code']}, claim {record['claim']}  ->  vigil: {_verdict(record)}")
    for check in failed:
        print(f"         failed check {check['type']}: {check['detail']}")


if __name__ == "__main__":
    main()
