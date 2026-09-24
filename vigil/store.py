"""Run history on disk. Plain JSON, no database, safe to read by hand."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

MAX_RUNS_PER_JOB = 200


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data = self._read()

    def _read(self) -> dict:
        if not self.path.is_file():
            return {"jobs": {}}
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (json.JSONDecodeError, OSError):
            return {"jobs": {}}
        data.setdefault("jobs", {})
        return data

    def save(self) -> None:
        """Atomic write so a killed process can't truncate the history."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=self.path.parent, prefix=".vigil-", delete=False
        )
        try:
            json.dump(self._data, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, self.path)

    def job_state(self, name: str, created_at: datetime | None = None) -> dict:
        jobs = self._data["jobs"]
        if name not in jobs:
            stamp = (created_at or datetime.now().astimezone()).isoformat()
            jobs[name] = {"created_at": stamp, "runs": [], "metrics": {}, "last_alert": None,
                          "covered_through": None, "covered": []}
        state = jobs[name]
        state.setdefault("runs", [])
        state.setdefault("metrics", {})
        state.setdefault("last_alert", None)
        state.setdefault("covered_through", None)
        state.setdefault("covered", [])
        return state

    def record_run(self, name: str, run: dict) -> None:
        state = self.job_state(name)
        state["runs"].append(run)
        del state["runs"][:-MAX_RUNS_PER_JOB]

    def runs(self, name: str, limit: int | None = None) -> list[dict]:
        runs = self.job_state(name)["runs"]
        return runs[-limit:] if limit else list(runs)

    def last_run(self, name: str) -> dict | None:
        runs = self.job_state(name)["runs"]
        return runs[-1] if runs else None

    def covered_through(self, name: str) -> datetime | None:
        """Start of the last on-schedule run. Windows before it are accounted for."""
        raw = self.job_state(name)["covered_through"]
        return datetime.fromisoformat(raw) if raw else None

    def cover_through(self, name: str, moment: datetime) -> None:
        state = self.job_state(name)
        state["covered_through"] = moment.isoformat()
        state["covered"] = [c for c in state["covered"] if datetime.fromisoformat(c) > moment]

    def covered(self, name: str) -> set[str]:
        return set(self.job_state(name)["covered"])

    def cover_window(self, name: str, window: datetime) -> None:
        """Mark one specific missed window as caught up."""
        state = self.job_state(name)
        stamp = window.isoformat()
        if stamp not in state["covered"]:
            state["covered"].append(stamp)

    def metric(self, name: str, key: str):
        return self.job_state(name)["metrics"].get(key)

    def set_metric(self, name: str, key: str, value) -> None:
        self.job_state(name)["metrics"][key] = value

    def last_alert(self, name: str) -> str | None:
        return self.job_state(name)["last_alert"]

    def set_last_alert(self, name: str, key: str | None) -> None:
        self.job_state(name)["last_alert"] = key

    def known_jobs(self) -> list[str]:
        return sorted(self._data["jobs"])
