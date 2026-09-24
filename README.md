# Vigil

Your scheduled agent exited 0. That is not the same as it having done anything.

## The problem

You put an agent on a schedule. It runs at 9am every day. Six weeks later you find out it stopped
producing anything on day three, because the source it reads from started returning an empty list
and the code caught the exception and moved on. Every run since then has exited 0. Cron is green.
Your monitoring is green. Nothing ever paged you, because nothing ever failed.

Then there is the other half: your machine was asleep at 9am, so the run never happened at all.
There is no record of it. A job that didn't run leaves no log line saying it didn't run.

Schedulers answer "did the process exit cleanly." That was the right question when the process was
`rsync`. It is the wrong question when the process is an agent whose entire output is judgment.

## What Vigil does

Vigil sits around the job, not inside it, and separates two things that scheduling tools conflate:

- **The claim.** What the job says it did. `{"emails_sent": 4, "status": "ok"}`.
- **The check.** Independent evidence that it happened. The file's mtime moved. The row count went
  up. The endpoint returns 200. The claim's own number is above zero.

A claim is not evidence. A run is verified only when the checks agree with it.

On top of that, two failure modes schedulers cannot see at all:

- **Missed windows.** Vigil knows when the job was supposed to run, so it can tell you about a run
  that never happened, and re-run it.
- **Degradation.** Identical output N runs in a row means the job is alive and useless.

## The pillar cron leaves empty

Security teams split a posture into three pillars. Protection stops the bad thing. Detection tells
you it happened anyway. Response is what you do about it.

Cron looks like it covers detection, which is why the gap is easy to miss. You get one signal, the
exit code, and it reports rather than prevents, so it feels like detection. The catch is where it
comes from. The job produces it. Your agent grades its own homework and hands you the grade.

A health check on the box doesn't close that. It watches whether the process is alive. It never
reads what the job wrote. Both signals are about liveness, and a lying agent is perfectly alive.

Real detection has to look at the work. That's the pillar Vigil fills: checks that run outside the
job, against what it produced rather than how it exited. Give a job checks and a run has to agree
with them to come back `verified`. Declare a job with no checks and you're back to cron, because
there's nothing for the claim to disagree with.

Response is still mostly yours. If Vigil runs the job and you set `catchup = true`, it'll re-run a
window that nothing covered. Watched jobs are excluded, since Vigil can't re-run a command it was
never given. Everything past that is a decision only you can make.

## The 60-second version

```
$ python3 examples/silent_digest/simulate.py

day 1  the source is healthy
       exit 0, claim {'status': 'ok', 'items': 6, 'file': 'digest.md'}  ->  vigil: verified
       exit 0, claim {'status': 'ok', 'items': 0, 'file': 'digest.md'}  ->  vigil: unverified
         failed check file_nonempty: digest.md is 38 bytes, wanted >= 120
         failed check claim_field: items=0, wanted >= 1
       exit 0, claim {'status': 'ok', 'items': 0, 'file': 'digest.md'}  ->  vigil: unverified
         failed check file_nonempty: digest.md is 38 bytes, wanted >= 120
         failed check claim_field: items=0, wanted >= 1
       exit 0, claim {'status': 'ok', 'items': 0, 'file': 'digest.md'}  ->  vigil: unverified+degraded
         failed check file_nonempty: digest.md is 38 bytes, wanted >= 120
         failed check claim_field: items=0, wanted >= 1

day 5  the laptop was asleep, nothing ran
       vigil: missed - no run covering 2026-09-23 20:23
       vigil: unverified - file_nonempty: digest.md is 38 bytes, wanted >= 120; claim_field: items=0, wanted >= 1
       vigil: degraded - identical output 3 runs in a row

day 6  catch-up runs the window nothing covered
       exit 0, claim {'status': 'ok', 'items': 0, 'file': 'digest.md'}  ->  vigil: unverified+degraded
         failed check file_nonempty: digest.md is 38 bytes, wanted >= 120
         failed check claim_field: items=0, wanted >= 1

--------------------------------------------------------------
cron saw   : 5 runs, exit codes [0, 0, 0, 0, 0], zero alerts
vigil saw  : verified, unverified, unverified, unverified+degraded, unverified+degraded
verified   : 1 of 5
--------------------------------------------------------------
The agent never crashed. It stopped working on day 2 and said 'ok' four more times.
```

The agent in that demo has one bug, and it is the most common bug there is:

```python
def load_feed():
    try:
        return json.loads(FEED.read_text())["items"]
    except Exception:
        return []   # a dead source is indistinguishable from a quiet day
```

## Install

```bash
git clone https://github.com/ksolano220/vigil.git
cd vigil && pip install -e .
```

Python 3.11 or newer. No dependencies, by design: this is the thing that watches everything else,
so it should not be the thing that breaks.

## Use it

```bash
vigil init          # writes a starter vigil.toml
vigil run digest    # run the job under supervision
vigil status        # one line per job
vigil check         # report missed, unverified and degraded jobs, exit 1 if any
vigil catchup       # re-run the windows nothing covered
vigil log digest    # recent runs
```

`vigil.toml`:

```toml
notify = "./alert.sh"     # optional, receives a JSON problem report on stdin

[jobs.digest]
command = "python3 agent.py"
every = "24h"             # or: at = ["09:15", "20:30"]
grace = "2h"              # how late is still on time
catchup = true
max_catchup = 2
degrade_after = 3         # identical output this many runs in a row

[[jobs.digest.checks]]
type = "file_changed"
path = "out/digest.md"
within = "1h"

[[jobs.digest.checks]]
type = "claim_field"      # hold the job to its own numbers
field = "items"
min = 1
```

### How a job files a claim

Write JSON to the path in `$VIGIL_CLAIM`, or print one line to stdout:

```python
Path(os.environ["VIGIL_CLAIM"]).write_text(json.dumps({"items": len(items)}))
```

```bash
echo "VIGIL_CLAIM: {\"rows\": $count}"
```

A job that files no claim still gets checked. The claim just gives the checks something to test and
gives degradation something to fingerprint.

### Watching a job you don't want to wrap

Wrapping every job on day one is a bad trade: you rewrite a working crontab to install a tool you
have not seen work yet. So a job can be declared with **no command at all**. Vigil never runs it. It
learns when the job is supposed to finish and checks the artifact the job should have left behind.

```toml
[jobs.morning_briefing]
at = ["08:03"]
grace = "4h"

[[jobs.morning_briefing.checks]]
type = "file_nonempty"
path = "~/briefings/%Y-%m-%d.md"
min_bytes = 2000
```

Paths take `strftime` codes and resolve against the window being checked, not against today, so
`%Y-%m-%d` means "the file that run was supposed to write" and a stale file from yesterday does not
count as today's run.

This is how you adopt it without touching anything: point it at what your jobs already produce, let
it watch for a week, then wrap the ones worth wrapping. It also covers jobs you cannot wrap, like
something running on a box you do not control.

The trade is real and worth knowing: a watched job proves the artifact, not the run. If a job writes
its file and then fails, watching alone calls that verified. Wrapping catches it.

### Checks

| type | proves | options |
| --- | --- | --- |
| `file_changed` | something wrote to the file recently | `path`, `within` |
| `file_nonempty` | the file has real content in it | `path`, `min_bytes` |
| `http_ok` | a service reflects the work | `url`, `status`, `contains`, `timeout` |
| `command` | anything you can express in a shell | `run`, `expect_exit`, `stdout_contains` |
| `metric_increased` | a number went up since last run | `run` or `field`, `min_delta` |
| `claim_field` | the job's own number holds up | `field`, `min`, `max`, `equals` |

`file_changed` on its own is the trap the demo walks into: the file was written, with nothing in it.
Pair the cheap check with one that reads the content.

### Verdicts

| verdict | meaning |
| --- | --- |
| `verified` | it ran, it exited 0, and the checks agree |
| `unverified` | it ran, it exited 0, and the evidence does not support it |
| `failed` | it exited nonzero or hit its timeout |
| `missed` | the window closed and nothing even tried |
| `degraded` | running, but producing identical output N times in a row |

`vigil run` exits 0 on verified, 1 on failed, 2 on unverified or degraded. So the scheduler in front
of it sees a failure for a job that lied about succeeding.

## Wiring it in

Replace the command in your scheduler with the supervised one, and add a heartbeat that notices the
runs that never happened.

```cron
15 9 * * *   cd ~/agents && vigil run digest
*/30 * * * * cd ~/agents && vigil check && vigil catchup
```

launchd, on a laptop that sleeps through its own schedule:

```xml
<key>ProgramArguments</key>
<array>
  <string>/usr/local/bin/vigil</string>
  <string>-c</string><string>/Users/you/agents/vigil.toml</string>
  <string>run</string><string>digest</string>
</array>
<key>StartCalendarInterval</key>
<dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>15</integer></dict>
```

`catchup` is what makes the sleeping laptop survivable: the 9:15 window is still owed output at
11:40 when the lid opens.

## Alerts

`notify` is any command. Vigil pipes it a JSON report and does not care what happens next:

```json
{
  "job": "digest",
  "at": "2026-09-23T09:20:11-04:00",
  "problems": [
    {"job": "digest", "kind": "unverified",
     "detail": "claim_field: items=0, wanted >= 1", "at": null}
  ]
}
```

Alerts are deduplicated per job, so a job that has been broken for a week tells you once, not 168
times, and tells you again when the shape of the breakage changes.

## Design notes

**No daemon.** Vigil runs when your scheduler runs it. A watchdog that needs its own watchdog is not
a watchdog.

**No database.** State is one JSON file you can read, diff, commit or delete. Written atomically,
and a corrupt file degrades to an empty history instead of a crash.

**Schedules are declared, not discovered.** Vigil reads its own copy of the schedule, which is what
lets it notice a run that never happened. Cron knows the schedule too, but keeps no record of
whether it fired. Vigil keeps the schedule and the receipts in the same file.

**Checks run outside the job.** A job cannot verify itself for the same reason a witness cannot
alibi themselves. If the check imports the agent's own code, it is a claim wearing a lab coat.

**An attempt covers its window.** A run that failed is reported as `failed`, never also as `missed`.
Missed means nothing tried.

**A broken check is a failed check.** If a check raises, the run is unverified. The supervisor never
takes the process down with it.

## What this is not

Not a scheduler. Cron and launchd are fine at starting processes on time, which is the part they are
good at. Not an APM or a tracing system: there is nothing to instrument inside your code. Not an
evaluation framework for model output quality, which is a different and much harder problem. Vigil
answers one question, which nothing else currently answers: did the scheduled work actually happen.

## Tests

```bash
python3 -m unittest discover -s tests
```

## License

MIT
