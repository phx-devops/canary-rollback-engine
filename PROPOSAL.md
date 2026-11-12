# Task proposal: Automated canary deployment and rollback engine

**Author:** Andy (Phoenix DevOps)
**Domain / subdomain:** Systems / Systems. The task covers ingress traffic management with nginx, telemetry-driven control loops, crash-consistent controllers, and Linux process isolation (network namespaces, privilege dropping, daemonisation).
**Difficulty:** Hard. About 4 h for an expert and about 15 h for a junior engineer.
**Task ID:** `canary-rollback-engine`
**Revision:** 5. This revision incorporates reviewer feedback on the runtime image, Compose service, verifier coverage, and reward reporting. See [Revision 5](#revision-5-review-fixes).

## What the agent is asked for

Two versions of a service, `app-blue` (v1.0) and `app-green` (v2.0), are already running inside the network namespace `/run/netns/canary`. The agent builds a toolchain (Python, Go or Bash) with these parts:

- **nginx as the ingress.** The ingress is the platform's standard proxy: the installed **nginx**, run and configured by the agent's toolchain on `127.0.0.1:8080`. It connects to the targets directly. No custom proxy may listen on `:8080`, and nothing may sit between nginx and the targets.
- **Stages.** The engine shifts traffic **90/10 → 50/50 → 0/100**, holding each stage for a dwell time, including a final bake at 0/100.
- **Only the engine changes the split.** The split must match the stage at all times, including while green is failing.
- **Telemetry.** Every **2 seconds** the engine samples nginx's access log. It evaluates green's error rate over the last **10 seconds**, once there are at least 20 green requests in that window.
- **Rollback.** If that rate is **above 5 %**, the engine immediately moves 100 % of traffic back to blue, and the abort sticks.
- **Crash and reboot recovery.** The engine can be SIGKILLed at any moment, and the node can reboot. A reboot kills every `canary` process and loses `/run` and `/tmp`. After the node agent runs `canaryctl up`:
  - a rollout in progress continues under the same ID, at the current stage's split;
  - an aborted rollout stays aborted, and a promoted one stays promoted;
  - every decision appears in the log exactly once.
- **Isolation.** Everything runs **unprivileged and detached inside the namespace**, even though the operator starts it as root from the default namespace.

The instruction defines every quantity and every file the verifier reads: the error-rate formula, the failure definition, the decision procedure, the recovery outcomes, the status and event schemas (including a new `resumed` event), and the exact `/proc` fields. It does not name nginx directives, a state format or a privilege-dropping method.

## Why it is hard

**The core step the models get wrong: making nginx tell the truth about a failing canary.** Previously an agent could write its own proxy, and every passing trial did exactly that, so none of the proxy traps applied. With nginx required, a correct-looking configuration fails, and fails silently. nginx's defaults are built to *protect users from a bad upstream*, which is the opposite of what a canary analysis needs. Three defaults each defeat a requirement:

1. **Passive health checking (`max_fails=1`, `fail_timeout=10s`).** After one connection error or timeout, nginx pulls app-green out of rotation for 10 s. When green crashes, it gets one failed request every 10 s, so the window never holds 20 requests and the rate is never evaluated. The rollout never aborts, and clients are quietly served by blue. Nothing in the config mentions this behaviour; it is a default.
2. **Retry on the next upstream (`proxy_next_upstream error timeout`).** A refused green request is resent to blue. The client gets a 200, and the access log line names two upstreams (`127.0.0.1:9002, 127.0.0.1:9001`). Naive parsers drop or misattribute the line.
3. **60 s upstream timeout.** A hung green produces nothing for a minute.

Other nginx details the agent must handle:
- **Zero weights.** `weight=0` is invalid in nginx, so 100/0 and 0/100 need `down`.
- **Live split changes.** Changing the split means a graceful reload.
- **Running unprivileged.** nginx defaults to root-owned temp, log and pid paths, so it has to be relocated to run as `canary`.

**The second core step: crash consistency.** A controller that keeps its rollout state in memory, or writes state *after* acting, breaks under kill and reboot, and the verifier now does both:
- **Rollout lost or regressed.** State kept in `/run` or `/tmp` is lost on reboot, so `up` restarts from idle or 100 % blue.
- **Abort undone.** An abort applied to nginx but not persisted resumes the rollout after a reboot, sending traffic to the bad canary.
- **Decision logged twice or not at all.** A decision persisted but not logged is lost from the log, and a recovered decision logged in the wrong place breaks the decision order.
- **Stale window.** A resumed engine that judges with a stale or half-empty window makes a decision on no evidence.
- **"Safe default" restart.** An `up` that starts nginx at 100 % blue on reboot flips live traffic back to blue mid-rollout.

**Kept from earlier revisions:**
- rolling-window maths (ratio of sums, not the mean of per-sample rates);
- attributing failures to green rather than blue;
- no decision without evidence (0/0 is not healthy);
- the ingress-death watchdog;
- the isolation end state.

## How it is verified

The verifier is a pytest suite (`tests/`, entry point `tests/test.sh`) with **146 named tests** over thirteen scenarios. It runs as root **inside the namespace** as a load generator: 60 rps, a new connection per request, recording which target answered each one. Fault injection and ground-truth request counters go through the apps' **root-only admin sockets**. The status file and event log are checked against their schemas and against that ground truth. The verifier also recomputes every decision from the engine's own samples, starting a fresh window at each `resumed` event. The reward is 1 only if every test passes.

| Scenario | Must happen |
|---|---|
| Healthy rollout | Promoted. Stages in order, each held at least the dwell time. Green share within ±5 pp of 10/50/100 at every stage. **Zero** failed client requests. Sample totals match app-green's own counter within 5 %. |
| Green 3 % / blue 30 % errors / bursty traffic | Promoted, with the split checked at every stage. |
| No traffic, then traffic | Holds at stage 1, then promotes. |
| Green 8 % errors at 50/50 | Aborted within 18 s, without advancing. **The split must stay at 50/50 while green is failing**, before the abort. |
| Green crashes at 50/50 (window mostly stage-2 traffic) | Aborted within 8 s, **without advancing**. Under nginx's default `max_fails`, green is pulled from rotation and the window looks healthy, so the engine advances the dead canary to 0/100. Under its default retry, the failures land on blue. |
| Green hangs during the 0/100 bake | Aborted within 20 s. Fails under the default 60 s timeout. |
| Ingress SIGKILLed at 50/50 while the engine runs | Aborted within 10 s; serves 100 % blue again. |
| **Engine killed the moment green starts failing**; `up` after 2 s | Resumed (`resumed` logged), then aborted within 25 s of the fault, without advancing. |
| **Chaos:** healthy rollout, engine SIGKILLed 3 times (once right after a stage change) plus one reboot at 50/50, each followed by `up` | Promoted under the same `rollout_id`. Every `up` prints `shifting` with a live engine pid. The split is correct at every stage and back at 50/50 within 5 s of the post-reboot `up`. No client errors outside the reboot outage. Exactly 4 `resumed` events, each stage logged exactly once, and every decision consistent with its own window. |
| **Abort, then reboot** | `up` reports `aborted`, 100 % blue. Green gets nothing, even though it now looks healthy. The abort is logged exactly once. |
| **Ingress identity** (before and after a reboot) | With upstream connections held open, the `:8080` listeners and every process connected to a target are `/usr/sbin/nginx`. |
| After every abort | Requests sent from abort + 1 s are all 200s from blue. Green's counter is frozen from + 2 s, even after it recovers. |
| Every status poll, every event line | Schema-valid (status may be absent only during a reboot). Decisions directly follow their triggering sample. |
| Isolation (mid-rollout, after the ingress restart, after a reboot) | Every non-target process in the netns: all UIDs/GIDs `canary`, groups ⊆ {canary}, CapInh/Prm/Eff/Amb = 0, no TTY, its own session. No file capabilities; no setuid/setgid files under `/opt/canary`. |
| CLI | `up` is idempotent, `reset` prints an idle status, `start` returns within 5 s, `--dwell 5` is rejected, and the final state is 100 % blue and idle. |

**A reboot is simulated faithfully:**
- every `canary` process is SIGKILLed;
- every entry in `/run` and `/tmp` that was not there at container start is deleted (the entrypoint records a baseline);
- `/var/lib`, `/var/log` and `/opt` survive.

## Validation done

- The prior revision's golden solution passed **144 tests** (reward 1, 11 min 49 s) on Ubuntu 24.04 with nginx 1.24 (`nginx-light`, as in the image) and a real network namespace. The current revision adds integration and schema coverage that must be rerun before claiming a pass.
- The verifier's pure self-tests passed **57 tests** after adding strict idle-state validation and terminal `pid: null` cases. The full runtime suite still needs to be rerun for this revision.
- **Fifteen deliberately broken engines are each caught** by `tests/mutants/run_mutants.sh`:

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

- **Fault-to-abort latency** (limit in brackets): 8 % 6.92 s (18), crash 0.92 s (8), hang 6.91 s (20), ingress killed 0.20 s (10), engine killed at fault onset 12.52 s (25).


## Revision 5: review fixes

The current revision applies the feedback on Compose wiring and privilege inspection, explicit
entrypoint permissions, CA certificates, startup/runtime descriptions, timeout configuration,
reward initialization, idle schema validation, active-rollout replacement/reset, and promotion
recovery. The verifier still shares a root-capable container with the agent, so it is not a
tamper-proof trust boundary; a private virtual environment cannot protect it from a root agent.

## Revision 4: difficulty

**Reviewer's finding.** The gate needs a core step the models get wrong.

**Changes:**

1. **nginx is required as the ingress, end to end.** This turns the documented-but-avoidable proxy traps into the core of the task. The verifier checks the binary behind every `:8080` listener and behind every connection to a target, with connections held open so the check is deterministic. That rules out a custom proxy, or a relay hidden behind nginx.
2. **The split must hold while green fails, and a failing canary must never advance.** The split is now also checked during the 8 % fault. A new test fails any rollout that reaches a later stage after green starts failing. This is what nginx's passive health checking causes: a crashed green looks healthy and gets promoted.
3. **Crash and reboot recovery with durable decisions.** Three new scenarios: chaos (4 restarts during a healthy rollout), abort-then-reboot, and engine-killed-at-fault-onset. There is also a new `resumed` event, and the verifier's rule check starts a fresh window at each one. The verifier also requires that each decision directly follows its triggering sample, which is what makes "logged exactly once, in the right place" checkable after a crash.
4. **Terminal PID handling.** `"pid": null` is valid whenever the state is not `shifting`. Only a live pid while shifting is required.
5. **HAProxy removed from the image.** The instruction names nginx. The previous Go-proxy golden is retired, because a custom proxy is no longer allowed.
6. **Verifier robustness.** The ingress-ownership check holds upstream connections open (app-blue hangs briefly while idle) instead of racing millisecond-long connections. The verifier timeout is now 1800 s.

**Fairness.** Everything is still specified as an outcome, so the instruction never hints at `max_fails` or `proxy_next_upstream`. Instead it states the property they break: *"Only the engine changes the split ... including while app-green is failing"*, and *"each client request goes to exactly one target"*. An expert who knows nginx, or who tests a crash against their own setup, finds the defaults within minutes. An agent that writes a plausible nginx config and tests only the happy path does not.

## Interpretations and open points

| Point | Decision | Why / alternative |
|---|---|---|
| Ingress choice | nginx is required (the platform-standard proxy). | Realistic: organisations standardise their ingress. And it is the only way to make the proxy-default traps unavoidable. HAProxy would work equally well as the mandated proxy if the reviewer prefers it. |
| "Two container targets" | Long-running service processes standing in for the containers. | Nested container runtimes would make the verifier unrunnable. Compose sibling containers are possible with no verifier change. |
| "Provided network namespace" | Always `/run/netns/canary`. The container gets `NET_ADMIN` + `SYS_ADMIN` and `init: true`. The agent's processes must hold no capabilities. | A checkable meaning (a netns inode comparison). |
| Error rate and threshold | Summed failures over summed requests in the last five samples, at least 20 requests, strictly above 5 %. | Standard, and stated explicitly. |
| Green failure | A 5xx, a refused or reset connection, or no response headers within 5 s. | Covers crashes and hangs, the common bad-canary failures. |
| Reboot | Every `canary` process is killed. `/run` and `/tmp` revert to their container-start contents. `/var/lib`, `/var/log` and `/opt` survive. | Mirrors a real node restart (tmpfs `/run`). |
| Dwell across restarts | Measured from stage entry, including downtime. A resumed engine still needs five fresh samples before deciding. | Downtime can't stall a rollout forever, and no decision is ever made on stale evidence. |
| Proxy death with the engine alive | Abort and restore 100 % blue. After a full reboot, resume instead. | A running engine that loses its ingress can't trust its telemetry; a rebooted node is a clean restart. |
| "Immediately" | 8 s crash, 18 s 8 %, 20 s hang, 10 s ingress, 25 s engine-killed-at-fault. | Each is the worst case the rule allows plus margin. |
| Dwell | Default 30 s, minimum 10 s, tests use 15 s. | Full verifier run about 12 minutes (measured: 11 min 49 s). |
