# Verifier suite — `canary-rollback-engine`

`test.sh` is the Terminal Bench entry point. It runs the pytest suite as root inside the task container and writes `/logs/verifier/reward.txt` and `reward.json`: **1 only if every test passes**. It initializes both rewards to 0 before running and also writes `junit.xml` and `pytest.log`. Extra arguments go to pytest (`./test.sh -k crash`).

## Layout

| file | role |
|---|---|
| `conftest.py` | Joins the `canary` netns (all verifier traffic comes from inside it). Remembers the default netns for `canaryctl`. Records the event-log offset so that only lines written during grading are judged. Caches each scenario so it runs once per session. |
| `harness.py` | Load generator, status poller, admin-socket client, `/proc` snapshot. Also the **pure checkers** (`status_problem`, `parse_events`, `rule_problems`, `terminal_problem`, `cadence_problem`), which take plain data. |
| `scenarios.py` | The scenario runners (thirteen scenarios). Each drives one rollout end to end and returns a dict of observations. |
| `test_00_cli.py` | `canaryctl` contract: `up` and its idempotency, active rollout replacement, reset stopping a rollout, status schema and file matching, `--dwell 5` rejected, 100 % blue when idle. |
| `test_10_promote.py` | Must promote: `healthy`, `green3` (green 3 % errors), `blue30` (blue 30 % errors), `bursty`, `notraffic`. Checks stage order and dwell, the live split per stage, zero client errors, sample counts vs ground truth, and that the rollout holds without traffic. |
| `test_20_rollback.py` | Must abort: `green8` (≤ 18 s; the split must hold at 50/50 while green fails), `crash` at 50/50 (≤ 8 s, window full), `hang` (≤ 20 s), `proxykill` (≤ 10 s), `killfault` (engine killed as green starts failing, `up` after 2 s; ≤ 25 s). Checks traffic back on blue without errors, green frozen even after it heals, and the final status. |
| `test_25_recovery.py` | `chaos`: a healthy rollout with the engine SIGKILLed three times plus one simulated reboot, each followed by `canaryctl up`. It must be promoted under the same ID, with the split held (and restored after the reboot) and decisions logged exactly once. `abortreboot`: an aborted rollout stays aborted and all-blue across a reboot. |
| `test_30_isolation.py` | Every non-target process in the netns has UID/GID/groups `canary`, no capabilities, no TTY and its own session. Checked mid-rollout, after the ingress restart and after a reboot. The `:8080` listeners and every connection to a target belong to `/usr/sbin/nginx`; this is checked with upstream connections held open, before and after a reboot. No file caps or setuid helpers. |
| `test_90_final.py` | Checks the promoted status and green traffic left by a completed rollout before cleanup, including recovery through `up`. |
| `selftest/` | Unit tests of pure checkers against hand-built good and bad statuses and event logs, including strict idle fields, `resumed` window resets and decision ordering. No environment needed. |
| `mutants/` | `run_mutants.sh` installs fifteen deliberately broken engines, one at a time, and confirms each fails its targeted tests. |

Every rollout scenario is also checked for: status schema on every 0.2 s poll (the file may be absent only during a simulated reboot), a well-formed event log, a 2 s sample cadence within each engine lifetime, each stage and terminal event logged exactly once, and **every decision recomputed from the engine's own samples**, with a fresh window at each `resumed` event and each decision directly after its triggering sample.

## Ground truth and closed loopholes

- The load generator (60 rps, a new connection per request) records which target answered each request.
- Fault injection and request counters go through the targets' root-only unix sockets, which the `canary` user cannot reach.
- Faults are deterministic: 8 % means exactly 8 of every 100 requests.
- The status file and event log are validated against their schemas *and* against ground truth. A log that claims correct decisions has to match its own samples, and the samples have to match app-green's counter.

## Running

```bash
# full grading run (~12 min)
bash tests/test.sh

# one area or scenario
bash tests/test.sh -k "crash or hang"
bash tests/test.sh test_30_isolation.py

# verifier self-tests (no environment, < 1 s)
CANARY_SELFTEST=1 python3 -m pytest tests/selftest

# mutation check (~45 min; reinstalls golden at the end)
bash tests/mutants/run_mutants.sh solution
```

## Results in the authoring sandbox (nginx 1.24, Ubuntu 24.04, real netns)

- Golden solution (Python controller + system nginx): **144 passed**, reward 1, 11 min 49 s. Abort latencies are written to `/logs/verifier/abort_latency.json`.
- Self-tests: **51 passed**.
- Mutants: **15/15 caught**. Table in `RUNNING.md`.
