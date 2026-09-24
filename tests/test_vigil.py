"""Stdlib-only tests: python3 -m unittest discover tests"""

import json
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vigil.config import load_config
from vigil.runner import _claim_from_stdout, read_claim
from vigil.schedule import expected_windows, missed_windows, parse_duration
from vigil.store import Store
from vigil.supervisor import FAILED, UNVERIFIED, VERIFIED, Supervisor

NOW = datetime.fromisoformat("2026-09-23T12:00:00+00:00")


def write_config(directory: Path, body: str) -> Path:
    path = directory / "vigil.toml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class TestDuration(unittest.TestCase):
    def test_units(self):
        self.assertEqual(parse_duration("90s"), timedelta(seconds=90))
        self.assertEqual(parse_duration("2h30m"), timedelta(hours=2, minutes=30))
        self.assertEqual(parse_duration("1d"), timedelta(days=1))

    def test_rejects_garbage(self):
        with self.assertRaises(ValueError):
            parse_duration("soon")


class TestSchedule(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _job(self, body):
        return load_config(write_config(self.root, body)).job("j")

    def test_interval_windows(self):
        job = self._job('[jobs.j]\ncommand = "true"\nevery = "1h"\n')
        windows = expected_windows(job, NOW - timedelta(hours=3), NOW)
        self.assertEqual(len(windows), 3)

    def test_clock_windows_are_daily(self):
        job = self._job('[jobs.j]\ncommand = "true"\nat = ["09:15", "20:30"]\n')
        windows = expected_windows(job, NOW - timedelta(days=1), NOW)
        self.assertEqual([f"{w:%H:%M}" for w in windows], ["20:30", "09:15"])

    def test_grace_holds_a_window_open(self):
        job = self._job('[jobs.j]\ncommand = "true"\nevery = "1h"\ngrace = "2h"\n')
        last = NOW - timedelta(hours=2, minutes=30)
        self.assertEqual(missed_windows(job, last, last, NOW), [])

    def test_window_closes_after_grace(self):
        job = self._job('[jobs.j]\ncommand = "true"\nevery = "1h"\ngrace = "10m"\n')
        last = NOW - timedelta(hours=3)
        self.assertEqual(len(missed_windows(job, last, last, NOW)), 2)


class TestClaim(unittest.TestCase):
    def test_stdout_sentinel(self):
        self.assertEqual(_claim_from_stdout('noise\nVIGIL_CLAIM: {"sent": 2}\n'), {"sent": 2})

    def test_last_claim_wins(self):
        stdout = 'VIGIL_CLAIM {"sent": 1}\nVIGIL_CLAIM {"sent": 9}\n'
        self.assertEqual(_claim_from_stdout(stdout), {"sent": 9})

    def test_bad_json_is_not_a_claim(self):
        self.assertEqual(_claim_from_stdout("VIGIL_CLAIM: not json"), {})

    def test_claim_file_beats_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "claim.json"
            path.write_text(json.dumps({"sent": 5}), encoding="utf-8")
            self.assertEqual(read_claim(path, 'VIGIL_CLAIM {"sent": 1}'), {"sent": 5})


class TestSupervisor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _supervisor(self, body):
        config = load_config(write_config(self.root, body))
        return Supervisor(config, store=Store(config.resolved_state_path()))

    def test_passing_checks_verify(self):
        supervisor = self._supervisor('''
            [jobs.j]
            command = "echo hi > out.txt"
            every = "1h"
            [[jobs.j.checks]]
            type = "file_nonempty"
            path = "out.txt"
        ''')
        self.assertEqual(supervisor.run_job("j")["verdict"], VERIFIED)

    def test_exit_zero_with_no_work_is_unverified(self):
        """The whole point: success is not evidence."""
        supervisor = self._supervisor('''
            [jobs.j]
            command = "echo 'VIGIL_CLAIM: {\\"items\\": 0}'"
            every = "1h"
            [[jobs.j.checks]]
            type = "claim_field"
            field = "items"
            min = 1
        ''')
        record = supervisor.run_job("j")
        self.assertEqual(record["exit_code"], 0)
        self.assertEqual(record["verdict"], UNVERIFIED)

    def test_nonzero_exit_fails(self):
        supervisor = self._supervisor('[jobs.j]\ncommand = "exit 3"\nevery = "1h"\n')
        record = supervisor.run_job("j")
        self.assertEqual(record["verdict"], FAILED)
        self.assertEqual(record["exit_code"], 3)

    def test_checks_are_skipped_when_the_job_failed(self):
        supervisor = self._supervisor('''
            [jobs.j]
            command = "exit 1"
            every = "1h"
            [[jobs.j.checks]]
            type = "file_nonempty"
            path = "missing.txt"
        ''')
        self.assertEqual(supervisor.run_job("j")["checks"], [])

    def test_identical_output_degrades(self):
        supervisor = self._supervisor('''
            [jobs.j]
            command = "echo 'VIGIL_CLAIM: {\\"items\\": 1}'"
            every = "1h"
            degrade_after = 3
        ''')
        verdicts = [supervisor.run_job("j")["degraded"] for _ in range(3)]
        self.assertEqual(verdicts, [False, False, True])

    def test_changing_output_does_not_degrade(self):
        supervisor = self._supervisor('''
            [jobs.j]
            command = "echo \\"VIGIL_CLAIM: {\\\\\\"n\\\\\\": $RANDOM}\\""
            every = "1h"
            degrade_after = 2
        ''')
        supervisor.run_job("j")
        self.assertFalse(supervisor.run_job("j")["degraded"])

    def test_missing_run_is_reported_then_caught_up(self):
        supervisor = self._supervisor('''
            [jobs.j]
            command = "true"
            every = "1s"
            grace = "0s"
            catchup = true
        ''')
        supervisor.run_job("j")
        supervisor._now = lambda: datetime.now().astimezone() + timedelta(seconds=5)
        self.assertTrue(supervisor.status("j").missed)
        self.assertTrue(supervisor.catchup().runs)
        self.assertLess(len(supervisor.status("j").missed), 5)

    def test_catchup_marks_its_window_covered(self):
        supervisor = self._supervisor('''
            [jobs.j]
            command = "true"
            every = "1s"
            grace = "0s"
            catchup = true
            max_catchup = 1
        ''')
        supervisor.run_job("j")
        supervisor._now = lambda: datetime.now().astimezone() + timedelta(seconds=3)
        first = supervisor.status("j").missed
        supervisor.catchup()
        second = supervisor.status("j").missed
        self.assertNotIn(first[-1], second)

    def test_catchup_writes_off_windows_it_will_not_rerun(self):
        """A week of missed windows should not alert forever after one catch-up pass."""
        supervisor = self._supervisor("""
            [jobs.j]
            command = "true"
            every = "1s"
            grace = "0s"
            catchup = true
            max_catchup = 1
        """)
        supervisor.run_job("j")
        supervisor._now = lambda: datetime.now().astimezone() + timedelta(seconds=6)
        self.assertGreater(len(supervisor.status("j").missed), 2)
        result = supervisor.catchup()
        self.assertEqual(len(result.runs), 1)
        self.assertGreater(result.skipped["j"], 0)
        self.assertEqual(supervisor.status("j").missed, [])

    def test_missed_windows_collapse_into_one_problem(self):
        supervisor = self._supervisor("""
            [jobs.j]
            command = "true"
            every = "1s"
            grace = "0s"
        """)
        supervisor.run_job("j")
        supervisor._now = lambda: datetime.now().astimezone() + timedelta(seconds=6)
        missed = [p for p in supervisor.scan(notify=False) if p.kind == "missed"]
        self.assertEqual(len(missed), 1)
        self.assertIn("windows with no run", missed[0].detail)

    def test_unknown_check_type_fails_loudly(self):
        supervisor = self._supervisor('''
            [jobs.j]
            command = "true"
            every = "1h"
            [[jobs.j.checks]]
            type = "vibes"
        ''')
        record = supervisor.run_job("j")
        self.assertEqual(record["verdict"], UNVERIFIED)
        self.assertIn("unknown check type", record["checks"][0]["detail"])


class TestWatchMode(unittest.TestCase):
    """A job Vigil does not run: it checks the artifact the job should have left."""

    WATCH = """
        [jobs.w]
        at = ["08:00"]
        grace = "1h"
        [[jobs.w.checks]]
        type = "file_nonempty"
        path = "out/%Y-%m-%d.md"
        min_bytes = 10
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        config = load_config(write_config(self.root, self.WATCH))
        self.supervisor = Supervisor(config, store=Store(config.resolved_state_path()))
        # Vigil starts watching when it is installed, so seed that moment first.
        self.base = datetime.now().astimezone().replace(hour=0, minute=1, second=0, microsecond=0)
        self.supervisor.store.job_state("w", created_at=self.base)

    def tearDown(self):
        self.tmp.cleanup()

    def _at(self, **offset):
        self.supervisor._now = lambda: self.base + timedelta(**offset)

    def _write_artifact(self, when, text="plenty of bytes here"):
        (self.root / "out").mkdir(exist_ok=True)
        (self.root / "out" / f"{when:%Y-%m-%d}.md").write_text(text, encoding="utf-8")

    def test_watch_only_job_needs_no_command(self):
        self.assertTrue(self.supervisor.config.job("w").watch_only)

    def test_running_a_watched_job_is_refused(self):
        with self.assertRaises(ValueError):
            self.supervisor.run_job("w")

    def test_nothing_to_say_before_the_window_closes(self):
        self._at(hours=1)
        self.assertIsNone(self.supervisor.evaluate("w"))

    def test_missing_artifact_is_unverified(self):
        self._at(hours=10)
        record = self.supervisor.evaluate("w")
        self.assertEqual(record["verdict"], UNVERIFIED)
        self.assertIsNone(record["exit_code"])

    def test_dated_path_resolves_against_the_window(self):
        self._write_artifact(self.base)
        self._at(hours=10)
        self.assertEqual(self.supervisor.evaluate("w")["verdict"], VERIFIED)

    def test_yesterdays_artifact_does_not_verify_today(self):
        """The whole point of a dated path: a stale file is not today's run."""
        self._write_artifact(self.base - timedelta(days=1))
        self._at(hours=10)
        self.assertEqual(self.supervisor.evaluate("w")["verdict"], UNVERIFIED)

    def test_an_empty_artifact_does_not_verify(self):
        self._write_artifact(self.base, text="hi")
        self._at(hours=10)
        self.assertEqual(self.supervisor.evaluate("w")["verdict"], UNVERIFIED)

    def test_each_window_is_evaluated_once(self):
        self._at(hours=10)
        self.assertIsNotNone(self.supervisor.evaluate("w"))
        self.assertIsNone(self.supervisor.evaluate("w"))

    def test_a_new_watch_is_watching_not_missed(self):
        """Nothing has closed yet, so the job is new, not broken."""
        self._at(hours=1)
        status = self.supervisor.status("w")
        self.assertEqual(status.verdict, "watching")
        self.assertTrue(status.healthy)

    def test_watched_jobs_report_no_missed_windows(self):
        self._at(days=3)
        kinds = {p.kind for p in self.supervisor.scan(notify=False)}
        self.assertNotIn("missed", kinds)
        self.assertIn(UNVERIFIED, kinds)


class TestStore(unittest.TestCase):
    def test_survives_a_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            store = Store(path)
            store.record_run("j", {"verdict": VERIFIED})
            store.save()
            self.assertEqual(len(Store(path).runs("j")), 1)


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_schedule_is_required(self):
        with self.assertRaises(ValueError):
            load_config(write_config(self.root, '[jobs.j]\ncommand = "true"\n'))

    def test_command_is_required(self):
        with self.assertRaises(ValueError):
            load_config(write_config(self.root, '[jobs.j]\nevery = "1h"\n'))

    def test_bad_duration_fails_at_load(self):
        with self.assertRaises(ValueError):
            load_config(write_config(self.root, '[jobs.j]\ncommand = "true"\nevery = "whenever"\n'))


if __name__ == "__main__":
    unittest.main()
