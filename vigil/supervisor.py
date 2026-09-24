"""Verdicts: did the run happen, did it do anything, and is it still doing it."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from vigil import runner
from vigil.checks import CheckContext, run_check
from vigil.schedule import expected_windows, missed_windows, next_due
from vigil.store import Store

VERIFIED = "verified"
UNVERIFIED = "unverified"
FAILED = "failed"
MISSED = "missed"
DEGRADED = "degraded"
WATCHING = "watching"

BAD_VERDICTS = {UNVERIFIED, FAILED}


@dataclass
class Problem:
    job: str
    kind: str
    detail: str
    at: datetime | None = None

    def as_dict(self) -> dict:
        return {"job": self.job, "kind": self.kind, "detail": self.detail,
                "at": self.at.isoformat() if self.at else None}


@dataclass
class CatchupResult:
    runs: list = field(default_factory=list)
    skipped: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.runs or self.skipped)


@dataclass
class JobStatus:
    job: object
    last_run: dict | None
    verdict: str
    degraded: bool
    missed: list = field(default_factory=list)
    next_due: datetime | None = None

    @property
    def healthy(self) -> bool:
        if self.verdict == WATCHING:
            return True
        return self.verdict == VERIFIED and not self.degraded and not self.missed


class Supervisor:
    def __init__(self, config, store: Store | None = None, now=None):
        self.config = config
        self.store = store or Store(config.resolved_state_path())
        self._now = now or (lambda: datetime.now().astimezone())

    # --- running -------------------------------------------------------

    def run_job(self, name: str, catchup_for: datetime | None = None, notify: bool = True) -> dict:
        job = self.config.job(name)
        if job.watch_only:
            raise ValueError(f"job {name!r} has no command; it is watched, not run")
        self.store.job_state(name, created_at=self._now())
        execution = runner.execute(job, self.config.root, catchup_for)

        results = []
        if execution.exit_code == 0:
            results = self._verify(job, execution)

        if execution.exit_code != 0:
            verdict = FAILED
        elif all(result.ok for result in results):
            verdict = VERIFIED
        else:
            verdict = UNVERIFIED

        fingerprint = _fingerprint(execution.claim, execution.stdout)
        record = {
            "started_at": execution.started_at.isoformat(),
            "finished_at": execution.finished_at.isoformat(),
            "duration_seconds": round(execution.duration_seconds, 3),
            "exit_code": execution.exit_code,
            "timed_out": execution.timed_out,
            "verdict": verdict,
            "claim": execution.claim,
            "fingerprint": fingerprint,
            "checks": [result.as_dict() for result in results],
            "catchup_for": catchup_for.isoformat() if catchup_for else None,
            "stderr_tail": _tail(execution.stderr),
        }
        record["degraded"] = self._is_degraded(job, fingerprint, verdict)
        self.store.record_run(name, record)
        # An attempt covers its window even when it fails. Missed means nothing tried.
        if catchup_for:
            self.store.cover_window(name, catchup_for)
        else:
            self.store.cover_through(name, execution.started_at)

        if notify:
            self._maybe_alert(job, record)
        self.store.save()
        return record

    def evaluate(self, name: str, notify: bool = True) -> dict | None:
        """Watch mode: check the artifact of a job Vigil does not run itself."""
        job = self.config.job(name)
        state = self.store.job_state(name, created_at=self._now())
        anchor = self.store.covered_through(name) or datetime.fromisoformat(state["created_at"])
        closed = [w for w in expected_windows(job, anchor, self._now())
                  if w + job.grace_delta <= self._now()]
        if not closed:
            return None

        window = closed[-1]
        results = []
        for index, check in enumerate(job.checks):
            ctx = CheckContext(
                job=job, claim={}, stdout="", store=self.store, root=self.config.root,
                started_at=window, now=self._now(), key=f"{index}:{check.type}", reference=window,
            )
            results.append(run_check(check, ctx))

        verdict = VERIFIED if all(r.ok for r in results) else UNVERIFIED
        record = {
            "started_at": window.isoformat(),
            "finished_at": self._now().isoformat(),
            "duration_seconds": 0.0,
            "exit_code": None,
            "timed_out": False,
            "verdict": verdict,
            "claim": {},
            "fingerprint": "",
            "checks": [r.as_dict() for r in results],
            "catchup_for": None,
            "watched": True,
            "stderr_tail": "",
            "degraded": False,
        }
        self.store.record_run(name, record)
        self.store.cover_through(name, window)
        if notify:
            self._maybe_alert(job, record)
        self.store.save()
        return record

    def _verify(self, job, execution) -> list:
        results = []
        for index, check in enumerate(job.checks):
            ctx = CheckContext(
                job=job, claim=execution.claim, stdout=execution.stdout, store=self.store,
                root=self.config.root, started_at=execution.started_at, now=self._now(),
                key=f"{index}:{check.type}",
            )
            results.append(run_check(check, ctx))
        return results

    def _is_degraded(self, job, fingerprint: str, verdict: str) -> bool:
        """Same output N runs in a row means the job is alive and useless."""
        if job.degrade_after <= 0 or verdict == FAILED:
            return False
        previous = [r for r in self.store.runs(job.name) if r.get("verdict") != FAILED]
        recent = previous[-(job.degrade_after - 1):] if job.degrade_after > 1 else []
        if len(recent) < job.degrade_after - 1:
            return False
        return all(r.get("fingerprint") == fingerprint for r in recent)

    # --- scanning ------------------------------------------------------

    def status(self, name: str) -> JobStatus:
        job = self.config.job(name)
        state = self.store.job_state(name, created_at=self._now())
        created_at = datetime.fromisoformat(state["created_at"])
        last = self.store.last_run(name)
        anchor = self.store.covered_through(name)
        covered = self.store.covered(name)
        open_windows = [] if job.watch_only else [
            w for w in missed_windows(job, anchor, created_at, self._now())
            if w.isoformat() not in covered
        ]
        return JobStatus(
            job=job,
            last_run=last,
            verdict=last["verdict"] if last else (WATCHING if job.watch_only else MISSED),
            degraded=bool(last and last.get("degraded")),
            missed=open_windows,
            next_due=next_due(job, anchor, self._now()),
        )

    def scan(self, notify: bool = True) -> list[Problem]:
        problems = []
        for name, job in self.config.jobs.items():
            if job.watch_only:
                self.evaluate(name, notify=False)
        for name in self.config.jobs:
            status = self.status(name)
            if status.missed:
                oldest, newest = status.missed[0], status.missed[-1]
                count = len(status.missed)
                detail = f"no run covering {oldest:%Y-%m-%d %H:%M}"
                if count > 1:
                    detail = f"{count} windows with no run, {oldest:%Y-%m-%d %H:%M} through {newest:%Y-%m-%d %H:%M}"
                problems.append(Problem(name, MISSED, detail, oldest))
            if status.verdict in BAD_VERDICTS:
                problems.append(Problem(name, status.verdict, _why(status.last_run)))
            if status.degraded:
                problems.append(Problem(name, DEGRADED, f"identical output {self.config.job(name).degrade_after} runs in a row"))
        if notify:
            self._alert_problems(problems)
        self.store.save()
        return problems

    def catchup(self, limit: int | None = None) -> CatchupResult:
        """Re-run the most recent windows a sleeping machine ate, and write off the rest."""
        result = CatchupResult()
        for name in self.config.jobs:
            job = self.config.job(name)
            if not job.catchup or job.watch_only:
                continue
            windows = self.status(name).missed
            if not windows:
                continue
            allowed = limit if limit is not None else job.max_catchup
            chosen = windows[-allowed:] if allowed else []
            stale = [w for w in windows if w not in chosen]
            for window in stale:
                # Too old to be worth re-running. Close it so it stops alerting.
                self.store.cover_window(name, window)
            if stale:
                result.skipped[name] = len(stale)
            for window in chosen:
                result.runs.append(self.run_job(name, catchup_for=window))
        self.store.save()
        return result

    # --- alerts --------------------------------------------------------

    def _maybe_alert(self, job, record: dict) -> None:
        if record["verdict"] == VERIFIED and not record["degraded"]:
            self.store.set_last_alert(job.name, None)
            return
        kind = DEGRADED if record["degraded"] and record["verdict"] != FAILED else record["verdict"]
        self._alert_problems([Problem(job.name, kind, _why(record))])

    def _alert_problems(self, problems: list[Problem]) -> None:
        if not self.config.notify or not problems:
            return
        by_job: dict[str, list[Problem]] = {}
        for problem in problems:
            by_job.setdefault(problem.job, []).append(problem)
        for name, items in by_job.items():
            key = "|".join(sorted(f"{p.kind}:{p.detail}" for p in items))
            if self.store.last_alert(name) == key:
                continue
            payload = {"job": name, "at": self._now().isoformat(),
                       "problems": [p.as_dict() for p in items]}
            self._send(payload)
            self.store.set_last_alert(name, key)

    def _send(self, payload: dict) -> None:
        try:
            subprocess.run(self.config.notify, shell=True, cwd=str(self.config.root),
                           input=json.dumps(payload), text=True, timeout=60)
        except Exception:
            pass  # an alert that cannot send must not take the supervisor down


def _fingerprint(claim: dict, stdout: str) -> str:
    material = json.dumps(claim, sort_keys=True) if claim else (stdout or "").strip()
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _why(record: dict | None) -> str:
    if not record:
        return "never ran"
    if record["verdict"] == FAILED:
        reason = "timed out" if record.get("timed_out") else f"exit {record['exit_code']}"
        return f"{reason}. {record.get('stderr_tail', '')}".strip()
    failed = [c for c in record.get("checks", []) if not c["ok"]]
    if failed:
        return "; ".join(f"{c['type']}: {c['detail']}" for c in failed)
    return "no checks declared" if not record.get("checks") else "ok"


def _tail(text: str, lines: int = 3) -> str:
    rows = [row for row in (text or "").strip().splitlines() if row.strip()]
    return " / ".join(rows[-lines:])[:500]
