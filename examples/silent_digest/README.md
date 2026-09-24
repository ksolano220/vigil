# Silent digest

A digest agent that stops working on day two and reports success four more times.

```bash
python3 simulate.py
```

The agent has one bug, in `load_feed()`: it catches the exception from a dead source and returns an
empty list. From the scheduler's point of view nothing is wrong. Exit 0, every run, forever.

The simulation runs a compressed schedule (`every = "1s"`) so six days take about eight seconds, and
covers the three things a scheduler cannot see:

1. **Day 2 to 4** the job runs and produces nothing. Vigil marks it `unverified`, because the claim
   says `items: 0` and the digest file is a header with no body.
2. **Day 4** the output has been byte-identical three runs in a row, so it is also `degraded`.
3. **Day 5** nothing runs at all. Vigil knows the window existed, reports it `missed`, and `catchup`
   re-runs it.

Note which check catches it. `file_changed` passes on every single run, because the agent really
does write the file. It writes an empty one. A check that only asks "was the file touched" is a
check the bug walks straight through.
