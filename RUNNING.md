# Running `canary-rollback-engine`

## Layout

```
canary-rollback-engine/
├── instruction.md          # what the agent sees
├── task.toml               # metadata, timeouts (verifier 1800 s)
├── PROPOSAL.md  DIFFICULTY.md  GOLDEN_SOLUTION.md  RUNNING.md  CODER_NOTES.md
├── environment/
│   ├── Dockerfile          # ubuntu:24.04 + nginx-light, iproute2, util-linux, python3, python3-pytest, go
│   ├── docker-compose.yaml # service main: NET_ADMIN, SYS_ADMIN, SYS_PTRACE; init: true
│   ├── start-env.sh        # entrypoint: netns, apps as `apps`, /run+/tmp baseline for simulated reboots
│   └── apps/app.py         # the two targets + root-only admin socket (faults, counters)
├── solution/
│   ├── solve.sh            # installs to /opt/canary/bin, `canaryctl up`
│   └── canary/             # canaryctl, engine.py, common.py (drives /usr/sbin/nginx)
└── tests/                  # verifier suite (see tests/README.md)
    ├── test.sh             # entry point: pytest → /logs/verifier/reward.txt (+ junit.xml, pytest.log)
    ├── conftest.py  harness.py  scenarios.py
    ├── test_00_cli.py  test_10_promote.py  test_20_rollback.py  test_25_recovery.py
    ├── test_30_isolation.py  test_90_final.py
    ├── selftest/           # 51 unit tests of the verifier's own checkers (no environment)
    └── mutants/            # run_mutants.sh + mutate.py: fifteen broken engines that must fail
```

## Build and run

```bash
docker compose -f environment/docker-compose.yaml build
docker compose -f environment/docker-compose.yaml up -d
docker compose exec main bash                   # agent works here (as root)

# golden solution
docker compose cp solution main:/solution && docker compose exec main bash /solution/solve.sh

# verifier (~12 minutes); extra args go to pytest, e.g. -k "chaos or crash"
# scenario ids: healthy green3 blue30 bursty notraffic green8 crash hang proxykill killfault
#               chaos abortreboot ingress
docker compose cp tests main:/tests && docker compose exec main bash /tests/test.sh

# verifier self-tests (no environment) and mutation check (~45 minutes, reinstalls golden at the end)
docker compose exec main env CANARY_SELFTEST=1 python3 -m pytest -p no:cacheprovider /tests/selftest
docker compose exec main bash /tests/mutants/run_mutants.sh /solution
```

## Notes for the harness

- The **main container** needs `NET_ADMIN` and `SYS_ADMIN` so the entrypoint can create the named namespace and so root can enter it. `SYS_PTRACE` lets the root verifier inspect `/proc/<pid>/fd` for sockets owned by the unprivileged ingress. The agent's processes are checked to hold none.
- The container runs with **`init: true`**, so orphaned daemons (nginx masters, engines) are reaped. Without it, PID 1 is the entrypoint's `sleep infinity`, and a killed process stays a zombie forever.
- **The verifier simulates reboots.** It SIGKILLs every `canary` process and deletes every entry in `/run` and `/tmp` that isn't listed in `/etc/canary-env/baseline.txt` (written by the entrypoint at container start). Don't keep harness files under `/tmp` while the verifier runs; `/logs` is safe.
- **Always grade in a freshly built container.** Leftover files from development have hidden real golden bugs before.
- The verifier must run as root. It joins the namespace for its own traffic (`os.setns`, Python ≥ 3.12) and invokes `canaryctl` from the container's default namespace. pytest comes from the image; no verifier-time package installation is required.
- The agent and verifier share a root-capable container. A root agent can modify the verifier or system Python, so the verifier is not tamper-proof in this setup. A private virtual environment does not establish a trust boundary against root; use an externally isolated verifier if that protection is required.
- Before grading, restore `/opt/apps/app.py` and re-run `start-env --no-wait`, so the targets, their admin sockets and the reboot baseline are pristine. (`start-env` restarts the two apps.)
- The verifier records the size of `/var/log/canary/events.jsonl` at start and validates only lines written after that.
- **Review feedback applied in the current revision:** service name `main`, `SYS_PTRACE` for verifier socket inspection, explicit entrypoint executable mode, `ca-certificates`, strict idle status validation, rollout replacement/reset coverage, promoted-state recovery coverage, reward initialized before pytest, and updated timeouts.

## Validation log (authoring sandbox: Ubuntu 24.04, nginx 1.24 nginx-light, Python 3.13, real netns)

- Golden solution (Python controller + system nginx), pytest suite: **144 passed, reward 1**, 11 min 49 s. Installed from scratch, with `/opt/canary`, `/run/canary`, `/var/lib/canary` and `/var/log/canary` removed first.
- Fault-to-abort latency: 8 % 6.92 s, crash 0.92 s, hang 6.91 s, ingress killed 0.20 s, engine killed at fault onset 12.52 s (written to `/logs/verifier/abort_latency.json` on every run).
- Verifier self-tests: **57 passed** in the current revision. The full runtime suite remains to be rerun.
- Mutation check: **15/15 caught**. The first pass found two problems, both fixed and re-run: the crash scenario at 90/10 let `nginx_max_fails` through, so the crash moved to 50/50 and a no-advance test was added; and one target selected no tests, so the runner now requires an actual failing test.

| Mutant | Defect | Target | Tests failed |
|---|---|---|---|
| `mean_of_ratios` | mean of per-sample rates | bursty | 8: aborted a healthy rollout |
| `all_5xx` | counts blue's 5xx as green's | blue 30 % | 7: aborted at stage 1 |
| `empty_is_healthy` | 0/0 treated as healthy | no traffic | 2: advanced with no evidence |
| `drain_not_zero` | aborts to 99/1 | green 8 % | 2: green kept getting traffic |
| `no_watchdog` | no ingress liveness check | ingress killed | 5: never aborted |
| **`nginx_max_fails`** | nginx default passive health check | crash | 2: **advanced the dead canary to 0/100**; abort 8.9 s (> 8) |
| **`nginx_next_upstream`** | nginx default retry to the other upstream | crash | 2: failures hidden on blue; advanced; deadline missed |
| **`nginx_read_timeout`** | nginx default 60 s upstream timeout | hang | 5: never aborted in time |
| **`up_always_blue`** | `up` restarts nginx at "safe" 100 % blue | chaos | 8 |
| **`no_resume`** | `up` doesn't restart the engine | chaos | 8: rollout stalled |
| **`abort_not_durable`** | abort applied but not persisted | abort + reboot | 2: rollout resumed after the reboot |
| **`state_in_run`** | rollout state kept under `/run` | chaos | 8: reboot lost the rollout |
| **`no_resumed_event`** | no `resumed` event on restart | chaos | 1 |
| `root_engine` | enters the netns but never drops root | isolation | 1 |
| `idle_schema` | idle status missing keys | CLI | 1 |

- Ingress-identity check: verified to flag a non-nginx process holding a connection to a target.
- The Dockerfile itself was not built in the authoring sandbox (no Docker there). The same packages, users, entrypoint and namespace were set up directly on Ubuntu 24.04.
