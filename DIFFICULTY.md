# Difficulty explanation — `canary-rollback-engine`

## 1. Source of difficulty (one sentence)

The agent must run a canary rollout through a **mandated nginx ingress whose defaults are designed to hide a failing upstream**, using a controller whose **decisions survive SIGKILL and node reboots exactly once**. A plausible nginx config and an in-memory controller pass the happy path. They then silently never abort a crashed canary, or flip a live rollout back to blue after a reboot, or resume an aborted one.

## 2. Why does a frontier AI coding agent fail at this?

**What changed in revision 4.** In the difficulty-gate trials (revision 3), 5 of 6 frontier runs passed. They built their own proxy, so none of the proxy traps applied, and nothing ever killed their controller. Revision 4 removes both escape routes. nginx is required end to end: the verifier checks the binary behind every `:8080` listener and every connection to a target. The controller is killed and the node rebooted mid-rollout.

### Core step 1: making nginx tell the truth about a failing canary

nginx is built to *shield clients from a bad upstream*. A canary analysis needs the opposite: every green failure must reach green's client and green's log line. Three defaults each defeat a requirement, and nothing in a typical config mentions them:

- **Passive health checks.** The default is `max_fails=1` with `fail_timeout=10s`. After one connection error or timeout, each nginx worker stops sending to app-green for 10 s. When green crashes at 50/50, green gets a couple of requests per 10 s instead of about 300. Those few failures are under 1 % of the window, so the engine sees a *healthy* window, and when the dwell runs out it **advances the dead canary to 0/100**. In the mutation run, this abort came only after blue was marked `down` and nginx had nowhere else to send traffic, 8.9 s after the crash. *Mutant `nginx_max_fails`: missed deadline, and advanced while green was down.*
- **Retries to the other upstream.** The default `proxy_next_upstream error timeout` resends a refused green request to blue. The client sees 200, and the log line becomes `127.0.0.1:9002, 127.0.0.1:9001  502, 200`. Parsers that expect one address drop the line or count it for blue. *Mutant `nginx_next_upstream`.*
- **The 60 s upstream timeout.** A hung green produces nothing for a minute, far beyond the 20 s limit. *Mutant `nginx_read_timeout`.*

Beyond those, nginx itself adds work:
- `weight=0` is invalid, so the end stages need `down`.
- A split change is a graceful reload, which must not fail requests.
- Running nginx unprivileged means relocating its pid, temp and log paths away from root-owned defaults.
- The access-log format must be designed for attribution (`$upstream_addr`, `$upstream_status`).

Fairness: the instruction never names these directives. It states the properties they break. *"Only the engine changes the split ... including while app-green is failing"* and *"each client request goes to exactly one target"* are outcomes an expert can test in minutes, for example by killing green in their own setup.

### Core step 2: crash-consistent decisions

The verifier SIGKILLs the engine at arbitrary points, including right after a stage change and at the moment green starts failing. It also simulates a reboot: every process running as `canary` dies, and `/run` and `/tmp` revert to their container-start contents. Common designs fail in different ways:

- **State in memory or in `/run`/`/tmp`.** After a reboot, `up` finds nothing and starts at idle or 100 % blue. *Mutant `state_in_run`.*
- **No resume.** `up` restarts nginx but not the engine, so the rollout stalls forever. *Mutant `no_resume`.*
- **"Safe default" restart at 100 % blue.** This flips live traffic back mid-rollout. The verifier checks the split is back at the current stage within 5 s of `up`. *Mutant `up_always_blue`.*
- **Act first, persist later.** An abort applied to nginx but not persisted resumes the rollout after a reboot and sends traffic back to the bad canary. *Mutant `abort_not_durable`.*
- **Log-then-act or act-then-log without reconciliation.** A decision is lost from the log, or logged twice, or logged after the restart instead of right after its triggering sample. The verifier requires each decision exactly once, directly after the sample that caused it.
- **Judging a resumed rollout on stale or partial evidence.** A resumed engine must start a fresh five-sample window. Advancing because "the dwell has passed" on a restart is a decision on no evidence. The verifier recomputes every decision with a fresh window at each `resumed` event. *Mutant `no_resumed_event`* is caught too.

### Kept from earlier revisions (still mutation-checked)

- **Rolling-window maths:** a ratio of sums, not the mean of per-sample rates. *Mutant `mean_of_ratios`.*
- **Attribution:** blue's errors are not green's. *Mutant `all_5xx`.*
- **No decision without evidence.** *Mutant `empty_is_healthy`.*
- **Abort to 100/0, not drain.** *Mutant `drain_not_zero`.*
- **Ingress-death watchdog.** *Mutant `no_watchdog`.*
- **The isolation end state:** every process unprivileged, in the namespace, detached. *Mutant `root_engine`.*
- **The schema contract.** *Mutant `idle_schema`.*

All-or-nothing grading means any one of these zeros the reward, and most of them pass a demo.

**Empirical status:** The golden solution passes the full suite. All fifteen mutants fail their targeted tests. The difficulty gate itself (3 GPT-6.1 Sol and 3 Kimi 3.1 Pro trials) must be re-run by the reviewer, because those models aren't available here.

## 3. Why is this hard for a professional expert?

- **nginx upstream semantics.** You need to know that passive health checks are on by default and per worker, and that they count connect errors and timeouts (not 5xx responses once `proxy_next_upstream` is off). You need to know how `proxy_next_upstream` rewrites the log variables, and that zero weights need `down`. Most engineers have shipped an nginx reverse proxy without ever touching `max_fails`.
- **Running nginx as an unprivileged daemon in a namespace.** That means a relocated prefix, pid and temp paths, an error log usable from the very first line (`-e`), reload signalling as the same user, and orphaned workers after a SIGKILLed master.
- **Crash-consistent control loops.** Write-ahead persistence with atomic replace and fsync, recovery that re-applies desired state, and reconciliation of the decision log. This is the core of every Kubernetes-style controller, and it is easy to get subtly wrong.
- **Telemetry correctness.** Tail an access log across reloads and restarts, attribute each line to green, and keep counts within 5 % of ground truth.

Estimate: about 4 h for an expert, about 15 h for a junior engineer.

## 4. Real-world application

This is the core of progressive delivery (Argo Rollouts, Flagger, Spinnaker/Kayenta, internal deploy tools) on infrastructure where the ingress is fixed by the platform team. nginx is the most common such ingress. The failure modes are real outage patterns:
- a canary hidden by the ingress's own health checks or retries, then promoted and failing at 100 %;
- a deploy controller restarted mid-rollout (pod rescheduled, node rebooted) that loses its place, re-runs a stage, or forgets that it aborted;
- deploy agents running as root when they only need to drive a proxy.

## 5. Data origin

- **Ingress:** the real nginx 1.24 (`nginx-light`, Ubuntu 24.04). No mocks.
- **Targets:** a small threaded HTTP service run twice as `app-blue` 1.0 and `app-green` 2.0, as user `apps` inside the namespace. It has deterministic fault modes: ratio (every Nth request returns 500), crash (closes its listener and drops open connections) and hang (accepts and never responds). Control is through a root-only socket that also exposes ground-truth counters.
- **Traffic:** generated live by the verifier: a steady 60 rps, a bursty 10/200 rps pattern, and silence.
- **Crashes and reboots:** real SIGKILLs and a simulated reboot (all `canary` processes killed; `/run` and `/tmp` reset to the container-start baseline).
