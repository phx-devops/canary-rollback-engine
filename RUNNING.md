# Running `canary-rollback-engine`

## Layout

```
canary-rollback-engine/
├── instruction.md          # what the agent sees
├── task.toml               # metadata, timeouts (verifier 900 s)
├── PROPOSAL.md  DIFFICULTY.md  GOLDEN_SOLUTION.md  RUNNING.md
├── environment/
│   ├── Dockerfile          # ubuntu:24.04 + haproxy, nginx-light, iproute2, util-linux, python3, go
│   ├── docker-compose.yaml # cap_add NET_ADMIN, SYS_ADMIN (to create the named netns); init: true
│   ├── start-env.sh        # entrypoint: create /run/netns/canary, start app-blue/app-green as `apps`
│   └── apps/app.py         # the two targets + root-only admin socket (faults, counters)
├── solution/
│   ├── solve.sh            # builds the Go proxy offline, installs to /opt/canary/bin, `canaryctl up`
│   ├── proxy/              # canary-proxy (Go, stdlib only) + unit tests
│   ├── canary/             # canaryctl, engine.py, common.py
│   └── alternatives/haproxy/  # previous HAProxy-based golden (also passes the suite)
└── tests/                  # verifier suite (see tests/README.md)
    ├── test.sh             # entry point: pytest → /logs/verifier/reward.txt (+ junit.xml, pytest.log)
    ├── conftest.py  harness.py  scenarios.py
    ├── test_00_cli.py  test_10_promote.py  test_20_rollback.py  test_30_isolation.py  test_90_final.py
    ├── selftest/           # 42 unit tests of the verifier's own checkers (no environment)
    └── mutants/            # run_mutants.sh + mutate.py: nine broken engines that must fail
```

## Build and run

```bash
docker compose -f environment/docker-compose.yaml build
docker compose -f environment/docker-compose.yaml up -d
docker compose exec client bash                 # agent works here (as root)

# golden solution
docker compose cp solution client:/solution && docker compose exec client bash /solution/solve.sh

# verifier (~8.5 minutes); extra args go to pytest, e.g. -k "crash or hang"
# scenario ids: healthy green3 blue30 bursty notraffic green8 crash hang proxykill
docker compose cp tests client:/tests && docker compose exec client bash /tests/test.sh

# verifier self-tests (no environment) and mutation check (~12 minutes, reinstalls golden at the end)
docker compose exec client env CANARY_SELFTEST=1 python3 -m pytest -p no:cacheprovider /tests/selftest
docker compose exec client bash /tests/mutants/run_mutants.sh /solution
```

## Notes for the harness

- The **container** needs `NET_ADMIN` and `SYS_ADMIN` so the entrypoint can create the named namespace and so root can enter it. The agent's processes are checked to hold none.
- The container runs with **`init: true`**, so orphaned daemons are reaped. Without it, PID 1 is the entrypoint's `sleep infinity`, and a killed proxy would stay a zombie forever.
- **Always grade in a freshly built container.** Leftover files from development (a canary-owned event log, for example) hid two real golden bugs in the authoring sandbox.
- The verifier must run as root. It joins the namespace for its own traffic (`os.setns`, Python ≥ 3.12) and invokes `canaryctl` from the container's default namespace. pytest comes from the image (`python3-pytest`), so no network is needed at grading time.
- Before grading, restore `/opt/apps/app.py` and re-run `start-env --no-wait` so the targets and their admin sockets are pristine. That way an agent cannot have edited the ground-truth source. (`start-env` kills and restarts the two apps.)
- The verifier records the size of `/var/log/canary/events.jsonl` when it starts and validates only lines written after that, so the agent's own development runs can't fail the log checks.
- No network access is needed at runtime for the agent or the verifier.

## Validation log (authoring sandbox, HAProxy 2.8.30, Python 3.13, real netns)

- Golden solution (Go proxy + Python controller), pytest suite: **110 passed, reward 1**, 8 min 25 s, installed from scratch (`/opt/canary`, `/run/canary` and `/var/log/canary` removed first).
- Go proxy: `go vet` clean, 7 unit tests passing with `-race`, static 6.4 MB binary.
- HAProxy alternative: passes the crash and proxy-killed scenarios after the revision-3 fixes. It passed the full suite before them.
- Fault-to-abort latency: 8 % 6.95 s, crash 0.97 s, hang 6.97 s, proxy killed 0.16 s (written to `/logs/verifier/abort_latency.json` on every run).
- Verifier self-tests: **42 passed**.
- Mutation check: **9/9 caught**.

| mutant | defect | target | tests that failed |
|---|---|---|---|
| `mean_of_ratios` | averages per-sample error rates | `bursty` | 8 (not promoted, stage order, splits, decisions) |
| `all_5xx` | counts every backend's 5xx | `blue30` | 7 (not promoted, stage order, splits, decisions) |
| `empty_is_healthy` | 0/0 treated as healthy | `notraffic` | 2 (holds at stage 1, decisions follow rule) |
| `drain_not_zero` | aborts to 99/1 instead of 100/0 | `green8` | 2 (traffic back on blue, green frozen after abort) |
| `no_watchdog` | no proxy liveness check | `proxykill` | 5 (deadline, blue restored, green frozen, final status, …) |
| `default_timeout` | proxy upstream timeout 60 s | `hang` | 5 (deadline, blue restored, green frozen, final status, …) |
| `redispatch` | Go proxy retries a failed green request on blue | `crash` | 5 (deadline, blue restored, green frozen, final status, …) |
| `root_engine` | enters the netns but never drops root | `mid-rollout` | 1 (every toolchain process unprivileged) |
| `idle_schema` | idle status missing schema keys | `test_00_cli` | 1 (reset reports schema-valid idle status) |

- Revision 2 also fixed a bug in `start-env`: `set -- $spec` overwrote the `--no-wait` flag, so re-running it for grading hung in `sleep infinity`.
- The Dockerfile itself was not built in the authoring sandbox (no Docker there). The same packages, users, entrypoint and namespace were set up directly on Ubuntu 24.04, so build the image once and re-run the golden solution inside it.
- To do before submission: one agentic frontier-model run in the built image.
