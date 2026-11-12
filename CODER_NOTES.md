# Coder notes — `canary-rollback-engine` (revision 4)

**Assumptions:**
- `app-blue` (v1.0, `127.0.0.1:9001`) and `app-green` (v2.0, `127.0.0.1:9002`) are long-running service processes standing in for the two containers. They run as user `apps` inside `/run/netns/canary`, which already exists with loopback up. The agent must not modify, restart or reconfigure them.
- **The ingress must be the installed nginx (`/usr/sbin/nginx`).** It is the platform-standard proxy. No custom proxy may listen on `:8080`, and nothing may sit between nginx and the targets. The verifier checks the binary behind every `:8080` listener and every connection to a target.
- The `main` container has `NET_ADMIN`, `SYS_ADMIN`, `SYS_PTRACE` and `init: true`. The verifier needs `SYS_PTRACE` to inspect descriptors owned by canary processes. The agent works as root, but every process it leaves in the namespace (the engine, every nginx process, any helpers) must run as `canary` with zero capabilities, detached.
- A request to green has failed if green answered 5xx, refused or reset the connection, or did not send response headers within 5 s. The response body need not complete within that interval. Blue's failures never count against green.
- The error rate is summed failures divided by summed requests over the last five 2-second samples. It is evaluated only with a full window and at least 20 green requests. "Exceeds 5 %" means strictly greater than.
- Stages are 90/10 → 50/50 → 0/100, each held for the dwell (0/100 is a bake that can still roll back), then promoted. **Only the engine changes the split.** The split must match the stage even while green is failing.
- An abort is sticky until a new `start`.
- If the ingress dies while the engine runs, the engine restores 100 % blue within 10 s and aborts.
- **Crashes and reboots.** The engine may be SIGKILLed at any moment, and a reboot kills every `canary` process and loses `/run` and `/tmp` (back to their container-start contents). `/var/lib`, `/var/log` and `/opt` survive. The node agent then runs `canaryctl up`.
- **After `up`:**
  - A shifting rollout continues under the same ID, at the current stage's split, within 5 s.
  - An aborted rollout stays aborted at 100 % blue; a promoted one stays promoted.
  - A resumed engine logs `resumed` and starts with an empty window.
  - Dwell counts from stage entry, including downtime.
  - Every decision is logged exactly once.
- Reviewer's fix 4: `"pid": null` is valid whenever the state is not `shifting`.

**Edge Cases:**
- **Green crashes at 50/50.** nginx's default passive health check pulls green after one failure per worker, so the window looks healthy and the engine *advances the dead canary*. Must abort within 8 s, without advancing.
- **Green fails 8 % at 50/50.** The split must stay at 50/50 until the abort, which must come within 18 s.
- **Green hangs during the 0/100 bake.** Needs a ≤ 5 s upstream timeout; abort within 20 s.
- **A green request is never retried on blue.** If it were, the client would see 200 and nginx would log two upstreams (`127.0.0.1:9002, 127.0.0.1:9001`).
- **Weights of 0** must be expressed as `down` (`weight=0` is invalid in nginx). Split changes are graceful reloads with zero failed client requests.
- **nginx as `canary`:** the pid, temp and log paths must be writable by that user, including the start-up error log.
- **Engine killed right after a stage change:** the stage must be logged once, never re-applied or skipped.
- **Engine killed the moment green starts failing;** `up` 2 s later. The resumed engine must still abort within 25 s.
- **Reboot mid-rollout:** state in `/run` or `/tmp` is gone, orphaned nginx workers may hold `:8080`, and the status file is missing until `up`. The split must be back at the current stage within 5 s of `up`, never 100 % blue.
- **Abort, then reboot, then green looks healthy:** the rollout must stay aborted and all-blue.
- **A killed nginx master is a zombie** until reaped, so liveness checks must treat it as dead.
- **No traffic:** hold at stage 1. **Bursty traffic:** ratio of sums. **Blue failing 30 %:** not green's errors.
- **In-flight requests at an abort are exempt.** Requests sent from abort + 1 s must go to blue, and green's counter is frozen from abort + 2 s.

**Common Mistakes:**
- **Leaving nginx defaults in place:**
  - `max_fails` (green silently pulled from rotation; the crash never aborts);
  - `proxy_next_upstream` (green failures land on blue and vanish);
  - `proxy_read_timeout 60s` (a hang is invisible).
  *(mutants `nginx_max_fails`, `nginx_next_upstream`, `nginx_read_timeout`)*
- **Parsing the access log** by assuming one upstream per line, or keying on `$status` instead of the upstream address and status.
- **Keeping rollout state in memory, `/run` or `/tmp`.** *(`state_in_run`)*
- **`up` that restarts nginx but not the engine** *(`no_resume`)*, or **restarts nginx at a "safe" 100 % blue** mid-rollout *(`up_always_blue`)*.
- **Acting before persisting:** an abort applied but not saved resumes after a reboot. *(`abort_not_durable`)*
- **Missing `resumed` events, or deciding on a stale or partial window after a restart.** Decisions logged twice or out of place. *(`no_resumed_event`)*
- **Kept from earlier revisions:**
  - mean of per-sample rates *(`mean_of_ratios`)*;
  - counting blue's 5xx *(`all_5xx`)*;
  - 0/0 treated as healthy *(`empty_is_healthy`)*;
  - abort to 99/1 *(`drain_not_zero`)*;
  - no ingress watchdog *(`no_watchdog`)*;
  - `ip netns exec` leaving root *(`root_engine`)*;
  - status missing keys *(`idle_schema`)*.
- **Root-owned files** that the `canary` engine or nginx later needs to write.

**Alternative Approaches:**
- **Golden:** a Python controller that renders nginx configs from durable state in `/var/lib/canary` (atomic, fsynced). Each decision is persisted, then applied by graceful reload, then logged. The engine tails the access log and reconciles cut-off decisions on resume.
- **Telemetry:** a file tail (golden), nginx `syslog:server=unix:…` to a socket the engine reads, or a JSON `log_format`. Any approach that keys on `$upstream_addr` and `$upstream_status`.
- **State:** a JSON file, SQLite, or a write-ahead log, as long as it survives a reboot and decisions are persisted before they are applied.
- **Config changes:** templated full configs or an included upstream file, optionally validated with `nginx -t` before reload. (The nginx-plus API isn't available.)
- **Privilege drop:** `setpriv`, `capsh`, Python `os.setns` + `setgroups`/`setresgid`/`setresuid`, or Go `unix.Setns` on a locked thread. Any method reaching the specified `/proc` state passes.
- **Language:** Python, Go or Bash.

**Response to Feedback:**
- **Difficulty gate (1 of 6 trials failed; at least 2 needed).** The passing agents wrote their own proxy and never had to recover from a crash, so the documented traps didn't apply. Revision 4 adds two core steps:
  1. **nginx is mandatory end to end**, so its failure-hiding defaults (passive health checks, retry to the next upstream, 60 s timeout) must be found and defeated. The instruction states only the property they break: only the engine changes the split, including while green is failing.
  2. **Crash- and reboot-consistent decisions:** the chaos, abort-then-reboot and engine-killed-at-fault scenarios, the `resumed` event, and exactly-once decisions checked against the engine's own samples.
- **Crash scenario moved from 90/10 to 50/50,** injected 9 s into the stage. At 90/10, two stray failures already exceed 5 % of a thin window, so the `max_fails` trap didn't bite. The mutation run found this. It also showed the `max_fails` engine *advancing the dead canary*, so a "no advance while green is failing" test was added.
- **Mutant runner hardened:** a mutant counts as caught only if a targeted test actually fails (one target had selected zero tests).
- Review feedback carried into this revision: all required Compose capabilities on `main`, explicit entrypoint mode, strict idle status validation, active-rollout replacement/reset tests, promoted `up` recovery, and failure reward initialized before pytest.
- **HAProxy removed** from the image, and the Go-proxy golden retired.
- **Verifier:** an ingress-identity check with upstream connections held open (deterministic, and it caught a test relay); a reboot simulation against a container-start baseline; an 1800 s timeout; self-tests; 15 mutants.
- **Verifier trust boundary:** because the agent and verifier share a root-capable container, root can alter the tests or system Python. The setup does not claim verifier tamper resistance; isolating the verifier requires platform support.
- **Still to do:** rerun the 6-trial gate (3 GPT-6.1 Sol, 3 Kimi 3.1 Pro) on the built image. Those models aren't available here.
