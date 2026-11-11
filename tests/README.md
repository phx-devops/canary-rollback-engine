# Verifier suite — `canary-rollback-engine`

`test.sh` is the Terminal Bench entry point. It runs the pytest suite as root inside the task container and writes `/logs/verifier/reward.txt`: **1 only if every test passes**. It also writes `junit.xml` and `pytest.log`. Extra arguments go to pytest (`./test.sh -k crash`).

## Layout

| file | role |
|---|---|
| `conftest.py` | Joins the `canary` netns (all verifier traffic comes from inside it). Remembers the default netns for `canaryctl`. Records the event-log offset so that only lines written during grading are judged. Caches each scenario so it runs once per session. |
| `harness.py` | Load generator, status poller, admin-socket client, `/proc` snapshot. Also the **pure checkers** (`status_problem`, `parse_events`, `rule_problems`, `terminal_problem`, `cadence_problem`), which take plain data. |
| `scenarios.py` | The nine scenario runners. Each drives one rollout end to end and returns a dict of observations. |
| `test_00_cli.py` | `canaryctl` contract: `up` and its idempotency, `reset` schema, `status` matches the file, `--dwell 5` rejected, 100 % blue when idle. |
| `test_10_promote.py` | Must promote: `healthy`, `green3` (green 3 % errors), `blue30` (blue 30 % errors), `bursty`, `notraffic`. Checks stage order and dwell, the live split per stage, zero client errors, sample counts vs ground truth, and that the rollout holds without traffic. |
| `test_20_rollback.py` | Must abort: `green8` (≤ 18 s), `crash` (≤ 8 s, injected once the window is full), `hang` (≤ 20 s), `proxykill` (≤ 10 s). Checks traffic back on blue without errors, green frozen even after it heals, and the final status. |
| `test_30_isolation.py` | Every non-target process in the netns has UID/GID/groups `canary`, no capabilities, no TTY and its own session. Checked mid-rollout and after the proxy restart. No file caps or setuid helpers. |
| `test_90_final.py` | End state: ingress up, 100 % blue, idle. |
| `selftest/` | 42 unit tests of the pure checkers against hand-built good and bad statuses and event logs. No environment needed. |
| `mutants/` | `run_mutants.sh` installs nine deliberately broken engines, one at a time, and confirms each fails its targeted tests. |

Every scenario test in modules 10 and 20 is also checked for: status schema on every 0.2 s poll, a well-formed event log, a 2 s sample cadence, the expected stage and terminal events, and **every decision recomputed from the engine's own samples**.

## Ground truth and closed loopholes

- The load generator (60 rps, a new connection per request) records which target answered each request.
- Fault injection and request counters go through the targets' root-only unix sockets, which the `canary` user cannot reach.
- Faults are deterministic: 8 % means exactly 8 of every 100 requests.
- The status file and event log are validated against their schemas *and* against ground truth. A log that claims correct decisions has to match its own samples, and the samples have to match app-green's counter.

## Running

```bash
# full grading run (~8.5 min)
bash tests/test.sh

# one area or scenario
bash tests/test.sh -k "crash or hang"
bash tests/test.sh tests/test_30_isolation.py

# verifier self-tests (no environment, < 1 s)
CANARY_SELFTEST=1 python3 -m pytest tests/selftest

# mutation check (~12 min; reinstalls golden at the end)
bash tests/mutants/run_mutants.sh solution
```

## Results in the authoring sandbox (HAProxy 2.8.30, Ubuntu 24.04, real netns)

- Golden solution (Go proxy + Python controller): **110 passed**, reward 1, 8 min 25 s. Abort latencies go to `/logs/verifier/abort_latency.json`.
- Self-tests: **42 passed**.
- Mutants: **9/9 caught**. Table in `RUNNING.md`.
