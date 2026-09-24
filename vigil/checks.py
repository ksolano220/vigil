"""Independent checks. A job's claim is not evidence; these are."""

from __future__ import annotations

import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from vigil.schedule import parse_duration


@dataclass
class CheckResult:
    type: str
    ok: bool
    detail: str

    def as_dict(self) -> dict:
        return {"type": self.type, "ok": self.ok, "detail": self.detail}


@dataclass
class CheckContext:
    job: object
    claim: dict
    stdout: str
    store: object
    root: Path
    started_at: datetime
    now: datetime
    key: str


def _resolve(root: Path, raw: str) -> Path:
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else (root / path)


def check_file_changed(options: dict, ctx: CheckContext) -> CheckResult:
    """The file's mtime moved recently, so something actually wrote to it."""
    path = _resolve(ctx.root, options["path"])
    within = parse_duration(options.get("within", "1h"))
    if not path.exists():
        return CheckResult("file_changed", False, f"{path} does not exist")
    mtime = datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    age = ctx.now - mtime
    if age <= within:
        return CheckResult("file_changed", True, f"{path.name} written {_ago(age)} ago")
    return CheckResult("file_changed", False, f"{path.name} last written {_ago(age)} ago, wanted within {options.get('within', '1h')}")


def check_file_nonempty(options: dict, ctx: CheckContext) -> CheckResult:
    path = _resolve(ctx.root, options["path"])
    minimum = int(options.get("min_bytes", 1))
    if not path.exists():
        return CheckResult("file_nonempty", False, f"{path} does not exist")
    size = path.stat().st_size
    ok = size >= minimum
    return CheckResult("file_nonempty", ok, f"{path.name} is {size} bytes, wanted >= {minimum}")


def check_http_ok(options: dict, ctx: CheckContext) -> CheckResult:
    url = options["url"]
    expected = int(options.get("status", 200))
    contains = options.get("contains")
    timeout = float(parse_duration(options.get("timeout", "10s")).total_seconds())
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            status = response.status
            body = response.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        status, body = error.code, ""
    except Exception as error:
        return CheckResult("http_ok", False, f"{url} unreachable: {error}")
    if status != expected:
        return CheckResult("http_ok", False, f"{url} returned {status}, wanted {expected}")
    if contains and contains not in body:
        return CheckResult("http_ok", False, f"{url} returned {status} but did not contain {contains!r}")
    return CheckResult("http_ok", True, f"{url} returned {status}")


def check_command(options: dict, ctx: CheckContext) -> CheckResult:
    """Escape hatch: any shell command that knows how to prove the work happened."""
    command = options["run"]
    expected = int(options.get("expect_exit", 0))
    contains = options.get("stdout_contains")
    try:
        completed = subprocess.run(
            command, shell=True, cwd=ctx.root, capture_output=True, text=True,
            timeout=parse_duration(options.get("timeout", "2m")).total_seconds(),
        )
    except subprocess.TimeoutExpired:
        return CheckResult("command", False, f"check command timed out: {command}")
    if completed.returncode != expected:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()
        hint = tail[-1] if tail else ""
        return CheckResult("command", False, f"exit {completed.returncode}, wanted {expected}. {hint}".strip())
    if contains and contains not in completed.stdout:
        return CheckResult("command", False, f"stdout did not contain {contains!r}")
    return CheckResult("command", True, f"exit {completed.returncode}")


def check_metric_increased(options: dict, ctx: CheckContext) -> CheckResult:
    """A number that must go up between runs: rows written, messages sent."""
    minimum = float(options.get("min_delta", 1))
    if "run" in options:
        completed = subprocess.run(
            options["run"], shell=True, cwd=ctx.root, capture_output=True, text=True,
            timeout=parse_duration(options.get("timeout", "2m")).total_seconds(),
        )
        if completed.returncode != 0:
            return CheckResult("metric_increased", False, f"metric command failed: exit {completed.returncode}")
        source = completed.stdout.strip().splitlines()
        raw = source[-1] if source else ""
    else:
        raw = ctx.claim.get(options["field"])
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return CheckResult("metric_increased", False, f"not a number: {raw!r}")

    previous = ctx.store.metric(ctx.job.name, ctx.key)
    ctx.store.set_metric(ctx.job.name, ctx.key, value)
    if previous is None:
        return CheckResult("metric_increased", True, f"baseline {value:g} recorded")
    delta = value - float(previous)
    ok = delta >= minimum
    return CheckResult("metric_increased", ok, f"{previous:g} -> {value:g} (delta {delta:+g}, wanted >= {minimum:g})")


def check_claim_field(options: dict, ctx: CheckContext) -> CheckResult:
    """Hold the job to its own numbers: a digest that sent 0 emails did not send."""
    field = options["field"]
    if field not in ctx.claim:
        return CheckResult("claim_field", False, f"claim has no field {field!r}")
    value = ctx.claim[field]
    if "equals" in options:
        ok = value == options["equals"]
        return CheckResult("claim_field", ok, f"{field}={value!r}, wanted {options['equals']!r}")
    if "min" in options or "max" in options:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return CheckResult("claim_field", False, f"{field}={value!r} is not a number")
        if "min" in options and number < float(options["min"]):
            return CheckResult("claim_field", False, f"{field}={number:g}, wanted >= {options['min']}")
        if "max" in options and number > float(options["max"]):
            return CheckResult("claim_field", False, f"{field}={number:g}, wanted <= {options['max']}")
        return CheckResult("claim_field", True, f"{field}={number:g}")
    ok = bool(value) and value != ""
    return CheckResult("claim_field", ok, f"{field}={value!r}")


CHECKS = {
    "file_changed": check_file_changed,
    "file_nonempty": check_file_nonempty,
    "http_ok": check_http_ok,
    "command": check_command,
    "metric_increased": check_metric_increased,
    "claim_field": check_claim_field,
}


def run_check(check, ctx: CheckContext) -> CheckResult:
    handler = CHECKS.get(check.type)
    if handler is None:
        known = ", ".join(sorted(CHECKS))
        return CheckResult(check.type, False, f"unknown check type (known: {known})")
    try:
        return handler(check.options, ctx)
    except KeyError as error:
        return CheckResult(check.type, False, f"check is missing option {error}")
    except Exception as error:  # a broken check is a failed check, never a crashed supervisor
        return CheckResult(check.type, False, f"check raised {type(error).__name__}: {error}")


def _ago(delta) -> str:
    seconds = int(abs(delta.total_seconds()))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"
