"""Execute a job and capture what it claims to have done."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

CLAIM_SENTINEL = "VIGIL_CLAIM"


@dataclass
class Execution:
    started_at: datetime
    finished_at: datetime
    exit_code: int
    stdout: str
    stderr: str
    claim: dict
    timed_out: bool = False

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


def execute(job, root: Path, catchup_for: datetime | None = None) -> Execution:
    claim_file = Path(tempfile.mkdtemp(prefix="vigil-claim-")) / "claim.json"
    env = os.environ.copy()
    env.update(job.env)
    env["VIGIL_JOB"] = job.name
    env["VIGIL_CLAIM"] = str(claim_file)
    if catchup_for:
        env["VIGIL_CATCHUP_FOR"] = catchup_for.isoformat()

    cwd = Path(job.cwd).expanduser() if job.cwd else root
    if not cwd.is_absolute():
        cwd = root / cwd

    started_at = datetime.now().astimezone()
    timed_out = False
    try:
        completed = subprocess.run(
            job.command, shell=True, cwd=str(cwd), env=env,
            capture_output=True, text=True, timeout=job.timeout_seconds,
        )
        exit_code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as expired:
        timed_out = True
        exit_code = 124
        stdout = _decode(expired.stdout)
        stderr = _decode(expired.stderr) + f"\nvigil: killed after {job.timeout}"
    finished_at = datetime.now().astimezone()

    claim = read_claim(claim_file, stdout)
    _cleanup(claim_file)
    return Execution(started_at, finished_at, exit_code, stdout, stderr, claim, timed_out)


def read_claim(claim_file: Path, stdout: str) -> dict:
    """The claim file wins; a VIGIL_CLAIM line on stdout is the fallback."""
    if claim_file.is_file():
        try:
            text = claim_file.read_text(encoding="utf-8").strip()
            if text:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
        except (json.JSONDecodeError, OSError):
            pass
    return _claim_from_stdout(stdout)


def _claim_from_stdout(stdout: str) -> dict:
    claim: dict = {}
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith(CLAIM_SENTINEL):
            continue
        payload = stripped[len(CLAIM_SENTINEL):].lstrip(": ").strip()
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            claim = parsed
    return claim


def _decode(raw) -> str:
    if raw is None:
        return ""
    return raw if isinstance(raw, str) else raw.decode("utf-8", "replace")


def _cleanup(claim_file: Path) -> None:
    try:
        claim_file.unlink(missing_ok=True)
        claim_file.parent.rmdir()
    except OSError:
        pass
