# Task proposal: Automated canary deployment and rollback engine

**Author:** Andy (Phoenix DevOps)
**Domain / subdomain:** Systems / Systems. The task covers ingress traffic management, telemetry-driven control loops, and Linux process isolation (network namespaces, privilege and capability dropping, daemonisation).
**Difficulty:** Hard. About 2.5 h for an expert and about 10 h for a junior engineer.
**Task ID:** `canary-rollback-engine`
**Revision:** 3. Revision 2 responded to the TB3 review (decision: Accept; "well-specified" marked Uncertain); see [Response to the review](#response-to-the-review). Revision 3 replaces the golden solution with a Go proxy plus Python controller, following an external frontier run, and fixes three deployment bugs and one test-timing weakness that the rebuild exposed (see [Revision 3](#revision-3-go-golden-solution)).

## What the agent is asked for

Two versions of a service, `app-blue` (v1.0) and `app-green` (v2.0), are already running inside the network namespace `/run/netns/canary`. The agent builds a toolchain in Python, Go or Bash with these parts:

- **An ingress proxy** on `127.0.0.1:8080` inside the namespace. The agent manages it, and it splits traffic between the two targets.
- **Stages.** The engine shifts traffic **90/10 → 50/50 → 0/100**. Each stage is held for a dwell time, including 0/100, before the rollout is promoted.
- **Telemetry.** Every **2 seconds** the engine takes a sample from the proxy's access logs or metrics. It evaluates app-green's error rate over the last **10 seconds** (five samples), once there are at least 20 green requests in that window.
- **Rollback.** If that rate is **above 5 %**, the engine immediately moves 100 % of traffic back to `app-blue`, and the abort sticks. It also aborts to blue if the proxy itself dies.
- **Isolation.** Everything runs **unprivileged and detached inside the namespace**, even though the operator starts it as root from the default namespace.

The instruction defines every quantity and every file the verifier reads:

- the error-rate formula, the 20-request minimum and the failure definition;
- the decision order after each sample;
- the full status-file schema, keyed by state;
- the event-log schema, with the required fields per event type;
- the exact `/proc` fields used for the isolation checks.

It does not name a proxy, proxy directives, telemetry plumbing or a privilege-dropping mechanism.

## Why it is hard

1. **The rolling window has to be computed correctly.** The error rate is summed failures divided by summed requests. Averaging each 2-second sample's percentage is the natural shortcut, and under bursty traffic it aborts healthy rollouts.
2. **Errors must be attributed to app-green.** Counting every 5xx at the ingress makes app-blue's errors abort the rollout. Counting only 5xx responses misses green failures that the proxy generates (connection refused, upstream timeout), which is how crashes and hangs show up.
3. **Proxy defaults hide failures.** HAProxy's retries and redispatch, or nginx's `proxy_next_upstream`, quietly resend failed green requests to blue. Clients see 200s and the telemetry never sees the crash. A default upstream timeout of 30–60 s makes a hung green invisible past the deadline.
4. **Weights must change live.** Reloading or restarting the proxy to change weights drops requests and resets counters, and the healthy run allows zero client errors.
5. **No decision without evidence.** With no traffic, 0/0 must not count as healthy.
6. **The proxy is a failure point too.** If the proxy dies mid-rollout, an engine that only reads telemetry sees an empty window and waits forever while clients get errors.
7. **Root starts it, but it must end up unprivileged in the namespace.** The end state is fully specified, so this is not a hunt for a specific syscall sequence: any mechanism that produces the state passes. Getting there still means entering the namespace while privileged and dropping identity and capabilities in a valid order, which the common tools (`ip netns exec`, `sudo -u`, `su`, `runuser`) don't do by default.
8. **The time limits are tight.** A crash must be rolled back within 8 s, 8 % errors within 18 s, a hang within 20 s, and a dead proxy within 10 s.

## How it is verified

The verifier is a pytest suite (`tests/`, entry point `tests/test.sh`) with **110 named tests** over nine scenarios. It runs as root **inside the namespace** as a load generator. It sends 60 rps with a new connection per request and records which target answered each one. It drives fault injection through the apps' **root-only admin sockets** and reads the apps' own request counters as ground truth. The status file and event log are checked against their schemas and against that ground truth, never trusted on their own. The reward is 1 only if every test passes. The suite's own checkers are covered by 42 self-tests, and a mutation runner proves that nine broken engines each fail (see Validation done).

| Scenario | Must happen |
|---|---|
| Healthy rollout | Promoted. Stages in order, each held at least the dwell time. Measured green share within ±5 pp of 10/50/100 at every stage. **Zero** failed client requests. The engine's sample total matches app-green's own counter within 5 %. |
| app-green fails 3 % of requests | Promoted (no false abort). |
| app-blue fails 30 % of requests, green healthy | Promoted (attribution). |
| Bursty traffic: 4 s at 10 rps with green failing 40 %, then 6 s at 200 rps clean | Promoted. The correct ratio is about 1–2 %, while averaging per-sample percentages gives more than 5 % at every sampling alignment. |
| No traffic for 2 × dwell, then traffic | Holds at stage 1, then promotes. |
| app-green 8 % errors at 50/50 | Aborted within 18 s. |
| app-green crashes at 90/10, once the 10 s window is full | Aborted within 8 s. |
| app-green hangs (accepts, never answers) during the 0/100 bake | Aborted within 20 s. |
| Ingress proxy SIGKILLed at 50/50 | Aborted within 10 s. From kill + 10 s, every request is a 200 from app-blue. |
| After every abort | Requests sent from abort time + 1 s are all 200s from blue. app-green's counter is frozen from + 2 s, including after green recovers. In-flight requests may finish either way. |
| Every status poll (every 0.2 s) | The full schema holds, and weights match the state and stage. |
| Event log of every rollout | Well-formed per schema. 2-second sample cadence. One terminal event of the expected kind. **Each stage change, promotion and abort recomputed from the engine's own samples follows the rule**, and the engine never ignores a breach. |
| Isolation, checked on every process in the namespace except the two targets | All four `Uid`/`Gid` values are `canary`. `Groups` ⊆ {canary}. CapInh/Prm/Eff/Amb = 0. `tty_nr = 0`. Session differs from the verifier's. The set must include the `:8080` listener and, while shifting, the engine pid. Checked during a healthy rollout and again after the proxy has been restarted. No file capabilities; no setuid/setgid files under `/opt/canary`. |
| CLI | `up` is idempotent. `reset` prints a schema-valid idle status. `start` prints the new rollout (state `shifting`, new `rollout_id`) within 5 s. `--dwell 5` is rejected. The final state is the ingress up at 100 % blue. |

**Ways to game the checks are closed off:**

- Fault control and ground-truth counters sit behind root-only sockets.
- Client-side routing is observed directly.
- The engine's reported counts must match app-green's own counter.
- Its decisions must be consistent with its own samples, so a fabricated log fails.
- Only event lines written during grading are judged, so leftovers from the agent's development runs don't count against it.

**Fault injection is deterministic.** "8 %" means exactly 8 of every 100 requests, evenly spaced. In simulation, random 3 % faults crossed the 5 % threshold through binomial noise in 7 of 60 runs; deterministic faults crossed it in 0 of 160. The latency limits are the simulated worst case (4.0 s for a crash, 10.7 s for 8 %) plus margin.

## Validation done

- **The golden solution passes all 110 tests** (reward 1), including the five isolation tests, on Ubuntu 24.04 with a real named network namespace. It is a static Go proxy plus a Python controller (GOLDEN_SOLUTION.md). The full run takes about 8.5 minutes. The previous HAProxy-based golden is kept in `solution/alternatives/haproxy/` and passes too, which shows the verifier doesn't depend on a particular proxy.
- **The verifier's own checkers pass 42 self-tests.** These are hand-built good and bad statuses and event logs: each status-schema violation, each malformed event, a decision on an unevaluated window, an ignored breach, a mislogged abort, and a mean-of-ratios abort.
- **Nine deliberately broken engines are each caught** by `tests/mutants/run_mutants.sh`. It installs each one (rebuilding the Go proxy from patched source where needed) and runs only its targeted tests:

| Mutant | Scenario | Result |
|---|---|---|
| Mean of per-sample percentages | bursty | Aborted a healthy rollout whose true window ratio was 1.29 % |
| Counts every backend's 5xx | blue 30 % | Aborted at stage 1 |
| Treats an empty window as healthy | no traffic | Promoted to 0/100 with zero traffic |
| Proxy upstream timeout of 60 s (Go proxy flag) | hang | Never aborted |
| Proxy retries a failed green request on blue (Go source patch) | crash | Never aborted; clients saw 200s |
| No proxy liveness check | proxy killed | Never aborted; clients got errors |
| Aborts to 99/1 instead of 100/0 | green 8 % | app-green kept receiving traffic after the abort |
| Enters the netns but never drops root | mid-rollout isolation | Toolchain processes ran as root with capabilities |
| Idle status missing schema keys | CLI | `reset` status failed the schema |

- **Measured fault-to-abort latency** (limit in brackets): 8 % errors 6.95 s (18), crash 0.97 s (8), hang 6.97 s (20), proxy killed 0.16 s (10).
- Not yet done: a **blind** frontier-agent run (instruction and environment only) inside the built image. The one external run so far doesn't count; see below.

## Revision 3: Go golden solution

**The external frontier run.** A frontier agent built a Go proxy plus a Python controller and passed 105 of 110 tests in a non-root sandbox. It couldn't run the 5 isolation tests there. Its abort latencies were 6.92 s for 8 % errors, 6.95 s for a crash, 6.96 s for a hang and 0.15 s for proxy death. **This is not a difficulty measurement**, because its input included the verifier suite and our previous golden solution. It does show that writing your own proxy is a natural route, so the golden now takes that route and the suite has been checked against it.

**What rebuilding the golden that way exposed.** These problems were hidden in the authoring sandbox but would have appeared in a freshly built image:

1. **Event-log ownership (affected both goldens).** On a fresh container, root's `canaryctl reset` created `/var/log/canary/events.jsonl` owned by root, so the engine (running as `canary`) couldn't write to it and died on start. The old HAProxy runs had only passed because a canary-owned file was left over from development. `canaryctl` now creates the file as canary's before anything appends to it, and repairs it if it's root-owned.
2. **Launcher log ownership.** Log files that root's launcher opened stayed root-owned, so the engine couldn't reopen `proxy.out` when it restarted a dead proxy. The launcher now hands them to `canary`.
3. **Zombie proxies.** A SIGKILLed proxy lingers as a zombie until PID 1 reaps it. The entrypoint's `sleep infinity` never reaps, so a liveness check based on `/proc/<pid>/comm` saw it as alive forever. Liveness now treats zombies as dead, and the compose file sets `init: true` so orphans are reaped.
4. **Crash test timing.** Both implementations aborted all three app faults at almost exactly 7 s. For the crash, that was the rule rather than the engine: the fault was injected 3 s into stage 1, before the first five-sample window existed (at 10 s), so every correct engine had to wait. The crash is now injected 11 s into stage 1, with the window full and the rollout still at 90/10. The test now measures detection speed, and the golden aborts in about 1 s.
5. **Stand-in app fidelity.** A simulated crash closed only the listener, so a proxy reusing pooled connections could keep getting answers from a "crashed" green. The app now drops every open connection on crash, as a real crash would. This possibly contributed to the external run's slow crash abort (6.95 s against an 8 s limit).

## Response to the review

| Review point | Change in this revision |
|---|---|
| *Well-specified:* event-log format not given | `instruction.md` now has an event table: required types (`sample`, `stage`, `aborted`, `promoted`), required fields and meanings, ordering (a decision follows the sample that triggered it), and how the tests use it. Unknown types are allowed. |
| *Well-specified:* status-file format not given | Full schema table: every key always present, types, null rules, and `weights` pinned per state (100/0 idle or aborted, the stage's split while shifting, 0/100 promoted). It is validated on every 0.2 s poll. |
| *Well-specified:* exact fields the verifier checks are not stated | The instruction states which file fields and which `/proc/<pid>/status`, `ns/net` and `stat` fields are read. It also says what is *not* checked (bounding set, `NoNewPrivs`). "How it will be checked" spells out abort-time measurement, the in-flight grace and the counter-freeze window. |
| *Well-specified:* namespace setup described conditionally | The fallback is removed. The environment always creates `/run/netns/canary` with loopback up, and the task container is always granted `NET_ADMIN` + `SYS_ADMIN` (in `docker-compose.yaml`). The instruction states this as a fact. |
| *Well-specified:* unclear whether the agent knows the 20-request minimum | It was always in `instruction.md`. "Not in the brief" in v1 meant *my original one-paragraph brief*, not the agent's instruction, and that wording is gone. The rule is now part of a numbered decision procedure that runs after each sample. |
| *Well-specified:* privilege requirements stated at a high level | They are now a precise list of the `/proc` values the tests read, applied to *every* non-target process in the namespace. Any mechanism is accepted. |
| *Verifiable:* how "no traffic after abort" is told apart from in-flight requests | Defined in the instruction and implemented: the abort time is the first poll showing `aborted`. Client requests sent at abort time + 1 s or later must all be served by blue, and green's own counter must not grow from abort time + 2 s. Requests in flight at the abort are exempt. |
| *Verifiable:* how the event log is validated | Schema validation on every line written during grading, plus a recomputation check: the verifier applies the rollback rule to the engine's own `sample` events and confirms each decision and the logged abort totals. |
| *Verifiable:* proxy crashing mid-stage not addressed | New requirement and scenario: the proxy is SIGKILLed at 50/50. The engine must abort and have it serving 100 % blue within 10 s, still unprivileged. A mutant without a liveness check fails. |
| *Solvable:* "no reference implementation is provided" | There is one (`solution/`, documented in GOLDEN_SOLUTION.md), and it passes the full verifier. That is now stated at the top of the validation section. |
| *Difficult:* privilege dropping is "trick-question-adjacent" | The checks describe an end state, not a recipe. `setpriv`, `capsh`, Python's `os.setns` + `os.setresgid`/`setgroups`/`setresuid`, or Go with `unix.Setns` + `syscall.Setresuid` on a locked thread all pass. The hard part of the task is elsewhere: telemetry correctness and attribution, which six mutants show are what break plausible implementations. |
| *Outcome-verified:* process-property checks | The brief's own requirement is "run unprivileged and detached inside the namespace", and the state of the running processes *is* that outcome. The checks inspect only that end state, never how it was reached. The 2 s cadence check verifies the brief's "every 2 seconds" claim, and the event log is checked for consistency with ground truth rather than for style. |

## Interpretations and open points

| Point | Decision | Why / alternative |
|---|---|---|
| "Two container targets" | app-blue and app-green are **long-running service processes standing in for the containers**. Each has its own address, user (`apps`), version and response header. | A nested container runtime is what kept the Bazzite task from being validated end to end. With plain processes, every check here runs today. Real sibling containers are possible via Compose `network_mode: service:client` and a shared volume for the admin sockets, with no verifier change. |
| "Provided network namespace" | Always `/run/netns/canary`, created by the entrypoint. The task container gets `cap_add: [NET_ADMIN, SYS_ADMIN]`. The agent's processes must hold no capabilities. | Gives "inside the namespace" a checkable meaning (a netns inode comparison). |
| "Exceeds 5 %" | Strictly greater than, on summed failures over summed requests in the last five samples. | The standard reading, stated explicitly. |
| Minimum sample | At least 20 green requests in the window. | Matches production canary analysers (Flagger, Argo Rollouts, Kayenta). Without it, one early 500 is a 100 % error rate. |
| What counts as a green failure | A 5xx from green, a refused or reset connection, or no response within 5 s. | Otherwise crashes and hangs, the most common bad-canary failures, produce no green 5xx and can't be tested. |
| "Reach 0/100 if healthy" | 0/100 is held for a dwell (a bake) and can still roll back. Then the rollout is promoted. | Matches Argo Rollouts and Flagger. |
| Proxy death | Abort and restore the proxy at 100 % blue rather than resume. | Resuming a rollout without telemetry is unsafe. Aborting is the conservative, testable choice. |
| "Immediately" | Limits from fault onset: 8 s for a crash, 18 s for 8 %, 20 s for a hang, 10 s for a dead proxy. The crash is injected once the window is full. | An 8 % fault can't push a 10 s window above 5 % before about 6.25 s, and a hang can't show until the 5 s upstream timeout. The limits are the worst case plus margin. |
| Dwell | Default 30 s, minimum 10 s, tests use 15 s. | Keeps the full verifier run to about 8.5 minutes (measured: 8 min 25 s). |
