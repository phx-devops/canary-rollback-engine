# Difficulty explanation — `canary-rollback-engine`

## 1. Source of difficulty (one sentence)

The agent must build a progressive-delivery controller whose correctness is all about behaviour under failure. That means windowed error-rate maths, attributing failures to the right target through a proxy that hides them by default, live weight changes, and a launch path that enters a namespace and sheds all privilege and its TTY. Every common shortcut produces a controller that promotes a healthy canary in a demo but misses a crash, misses a hang, or aborts on noise.

## 2. Why does a frontier AI coding agent fail at this?

The happy path is easy and the test that matters is the unhappy path. An agent that brings up HAProxy, flips weights on a timer and greps for ` 5[0-9][0-9] ` in a log will watch a healthy rollout succeed and declare victory. Each of the following is a separate, silent failure, and six of them are confirmed by mutants that fail the real verifier:

- **Wrong window maths.** Averaging the five per-sample percentages instead of dividing summed failures by summed requests. Under bursty traffic, a quiet 2-second slot with 1 failure in 2 requests counts as 50 %. *Mutant: aborted a healthy rollout.*
- **Wrong attribution.** Counting ingress-wide 5xx means app-blue's errors (30 % in the test) abort green's rollout. *Mutant: aborted at stage 1.*
- **Retries hide the crash.** HAProxy `retries` plus `option redispatch`, or nginx's default `proxy_next_upstream error timeout`, resend failed green requests to blue. Clients get 200s and the access log names blue as the server, so the crash never appears in telemetry. *Mutant: never aborted.*
- **Default timeouts.** A hung green (accepts, never answers) produces nothing until the proxy's upstream timeout fires, which defaults to 30–60 s. That is far past the 20 s limit. *Mutant (60 s): never aborted within the limit.*
- **Writing your own proxy doesn't avoid these traps; it moves them.** An agent may skip HAProxy and nginx and write its own proxy, as an external frontier run did with Go. It then has to make every one of these decisions itself. Go's `httputil.ReverseProxy` with the default transport has **no** response timeout, so a hung green blocks indefinitely. Its connection pooling can keep a dead backend looking alive on reused connections. Any retry or fallback logic hides green's failures. The golden solution is exactly this kind of proxy, and the `redispatch` and `default_timeout` mutants are patches to *its* code, both caught.
- **0/0 treated as healthy.** With no traffic, a naive `errors / max(n, 1)` or `n == 0 → healthy` promotes all the way to 0/100 on no evidence. *Mutant: promoted with zero traffic.*
- **Reload instead of runtime API.** Changing weights by rewriting the config and reloading (or restarting) the proxy drops requests mid-shift and resets cumulative counters. That gives negative deltas, lost samples or client errors, and the healthy scenario requires zero failed client requests.
- **A dead proxy goes unnoticed.** An engine that only reads telemetry sees an empty window when the proxy dies, so it waits for traffic that will never be counted while every client request fails. *Mutant (no liveness check): never aborted, 900 failed client requests.*
- **Privilege and namespace end state.** The instruction lists exactly which `/proc` values must hold, and any mechanism that produces them passes, so this is not a hunt for one syscall sequence. The common tools just don't produce the end state by default. `ip netns exec` leaves the process as root. `sudo -u canary` and `su` keep the caller's session and, often, a TTY. `runuser` keeps inheritable or ambient bits unless they are cleared. `setcap` on the proxy is checked and fails. A process started with `&` from `canaryctl` shares the caller's session. And because the check covers *every* non-target process in the namespace, a root helper or watchdog left behind fails too.
- **Sticky abort.** After rollback, the verifier heals green. Engines that keep evaluating re-promote. Engines that leave green's weight above 0 (draining instead of zeroing) send it traffic, and the verifier sees that on app-green's own counter.

All-or-nothing grading means any one of these zeros the reward. Most of them let the agent believe it has finished.

**Empirical status:** The golden solution (Go proxy plus Python controller) passes all 110 tests of the pytest suite on a live build. The six mutants above, and three more (abort to 99/1, engine left as root, incomplete idle status), were run against the suite, and each failed its targeted tests.

**External frontier run:** one run reached 105 of 110 tests (the 5 isolation tests couldn't run in its non-root sandbox). It is **not** a valid difficulty data point, because its input included the verifier and the previous golden solution. A blind run, given only `instruction.md` and the environment, is still needed. If a blind run also passes, the task needs to be made harder.

**Depth versus volume.** One review asked whether the difficulty is volume rather than a single insight. The checks do touch several areas, but one insight runs through most of them: *a canary engine is only as good as its view of the canary's failures, and the proxy's defaults are built to hide them.* Retries move failures to blue. Long timeouts delay them. 5xx-only counting misses failures the proxy itself generates. A dead proxy produces no telemetry at all. An agent that understands this passes the crash, hang, attribution and proxy-death scenarios together. One that doesn't fails most of them.

## 3. Why is this hard for a professional expert?

A senior SRE knows what a canary is. The time goes on the details:

- **Proxy internals.** You have to know that HAProxy's per-server weights can be changed live over the admin socket, that `retries`/`redispatch` move failures onto another server, which termination flags mean the server failed rather than the client, and that `log` can target a unix datagram socket when no syslog daemon exists. The nginx route is harder: it has no live weights without the commercial API, so you need upstream reloads without dropping requests, and per-upstream status in the access log.
- **Telemetry correctness.** Turning a streamed log or cumulative counters into exact 2-second deltas and a 10-second ratio, without double counting across reloads and without letting the sampler drift, and confirming the counts agree with ground truth to within 5 %.
- **Linux process isolation.** Entering the namespace while still privileged and only then dropping identity, clearing supplementary groups, the four capability sets, and session leadership versus process groups. The target state is spelled out, but most engineers have used `ip netns exec` without ever producing a capability-free, detached daemon from a root caller, and they find the gaps by checking `/proc`.
- **Timing.** Designing an engine whose worst-case detection fits 8/18/20-second limits means reasoning about sample alignment, window fill and proxy timeouts together.

Estimate: about 2.5 h for an expert who knows HAProxy's runtime API, and about 10 h for a junior who has to discover the retry, timeout and privilege traps by failing.

## 4. Real-world application

This is the core loop of every progressive-delivery system: Argo Rollouts, Flagger, Spinnaker/Kayenta, AWS CodeDeploy canaries, and internal deploy tools at most large companies. Teams build exactly this when they run on bare VMs, edge nodes or appliances without Kubernetes or a service mesh. The failure modes tested are the ones that cause real outages:

- A canary that **hangs** rather than crashes and goes unnoticed because the proxy's timeout is longer than the analysis window.
- **Retries hiding a broken canary.** It looks healthy, gets promoted, and fails at 100 %.
- **False rollbacks** caused by low-traffic noise or by another service's errors, which teach teams to switch automation off.
- Deploy agents running **as root** on shared hosts when they only need to touch a proxy socket.

## 5. Data origin

- **Proxy:** the real HAProxy 2.8 (Ubuntu 24.04 package); nginx is also installed as an alternative. No mocks.
- **Targets:** a small threaded HTTP service (`/opt/apps/app.py`, about 75 lines) run twice as `app-blue` 1.0 and `app-green` 2.0, as the unprivileged `apps` user inside the namespace. It supports deterministic fault modes: ratio (every Nth request returns 500), crash (closes its listener) and hang (accepts and never responds). Control is through a root-only unix socket that also exposes ground-truth counters.
- **Traffic:** generated live by the verifier: steady 60 rps, a bursty 10/200 rps pattern, and silence.
- **Thresholds and limits:** taken from the brief (2 s, 10 s, 5 %, 90/10 → 50/50 → 0/100). The minimum sample (20) and failure definition follow production canary analysers. The latency limits are derived from a simulation of the sampling maths.

Realism is high. The only concession is that the two "containers" are processes in a shared namespace (see the PROPOSAL open points).
