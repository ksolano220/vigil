"""Job declarations, read from vigil.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from vigil.schedule import parse_clock, parse_duration

DEFAULT_CONFIG_NAME = "vigil.toml"


@dataclass
class Check:
    type: str
    options: dict


@dataclass
class Job:
    name: str
    command: str
    every: str | None = None
    at: list[str] = field(default_factory=list)
    grace: str = "1h"
    cwd: str | None = None
    env: dict = field(default_factory=dict)
    timeout: str = "30m"
    catchup: bool = False
    max_catchup: int = 1
    degrade_after: int = 0
    checks: list[Check] = field(default_factory=list)

    @property
    def every_delta(self) -> timedelta:
        return parse_duration(self.every) if self.every else timedelta(days=1)

    @property
    def grace_delta(self) -> timedelta:
        return parse_duration(self.grace)

    @property
    def timeout_seconds(self) -> float:
        return parse_duration(self.timeout).total_seconds()

    @property
    def schedule_label(self) -> str:
        return f"at {', '.join(self.at)}" if self.at else f"every {self.every}"


@dataclass
class Config:
    path: Path
    jobs: dict[str, Job]
    notify: str | None = None
    state_path: Path | None = None

    @property
    def root(self) -> Path:
        return self.path.parent

    def resolved_state_path(self) -> Path:
        return self.state_path or self.root / ".vigil" / "state.json"

    def job(self, name: str) -> Job:
        if name not in self.jobs:
            known = ", ".join(sorted(self.jobs)) or "none"
            raise KeyError(f"unknown job {name!r} (declared: {known})")
        return self.jobs[name]


def find_config(start: Path | None = None) -> Path:
    """Walk up from start looking for vigil.toml."""
    current = (start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        candidate = directory / DEFAULT_CONFIG_NAME
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"no {DEFAULT_CONFIG_NAME} found in {current} or any parent")


def load_config(path: str | Path | None = None) -> Config:
    config_path = Path(path).resolve() if path else find_config()
    with open(config_path, "rb") as handle:
        raw = tomllib.load(handle)

    jobs = {}
    for name, body in (raw.get("jobs") or {}).items():
        jobs[name] = _build_job(name, body)

    state = raw.get("state_path")
    return Config(
        path=config_path,
        jobs=jobs,
        notify=raw.get("notify"),
        state_path=(config_path.parent / state).resolve() if state else None,
    )


def _build_job(name: str, body: dict) -> Job:
    if "command" not in body:
        raise ValueError(f"job {name!r} has no command")
    at = [str(t) for t in body.get("at", [])]
    for text in at:
        parse_clock(text)
    if not at and not body.get("every"):
        raise ValueError(f"job {name!r} needs either every = or at =")

    job = Job(
        name=name,
        command=body["command"],
        every=body.get("every"),
        at=at,
        grace=body.get("grace", "1h"),
        cwd=body.get("cwd"),
        env={str(k): str(v) for k, v in (body.get("env") or {}).items()},
        timeout=body.get("timeout", "30m"),
        catchup=bool(body.get("catchup", False)),
        max_catchup=int(body.get("max_catchup", 1)),
        degrade_after=int(body.get("degrade_after", 0)),
        checks=[_build_check(name, c) for c in body.get("checks", [])],
    )
    # Touch the derived properties so a bad duration fails at load, not at 3am.
    _ = (job.every_delta, job.grace_delta, job.timeout_seconds)
    return job


def _build_check(job_name: str, body: dict) -> Check:
    if "type" not in body:
        raise ValueError(f"job {job_name!r} has a check with no type")
    options = {k: v for k, v in body.items() if k != "type"}
    return Check(type=str(body["type"]), options=options)
