# Golden solution — `canary-rollback-engine`

The golden solution is a **static Go ingress proxy** plus a **Python controller**. `solution/solve.sh` builds the proxy from source (Go standard library only, so it builds offline with the image's `golang-go`), installs everything into `/opt/canary/bin/`, and runs `canaryctl up`. It passes all 110 tests of the verifier suite (`tests/test.sh`, reward 1), including the five isolation tests.

```
solution/
├── solve.sh                    # build proxy → install → bring ingress up at 100 % blue
├── proxy/                      # Go, stdlib only
│   ├── main.go                 # canary-proxy: weighted routing, live weights, per-backend counters
│   ├── main_test.go            # 7 unit tests (run with -race)
│   └── go.mod                  # go 1.22 (Ubuntu 24.04's golang-go)
├── canary/
│   ├── canaryctl               # CLI: up | start | status | reset; privileged launcher
│   ├── engine.py               # rollout loop: scrape, window, stage/abort decisions, proxy watchdog
│   └── common.py               # proxy admin client, proxy command line, status + event writers
└── alternatives/haproxy/       # the previous HAProxy-based golden; also passes the suite
```

This design follows the approach an external frontier-model run took. That run (Go proxy plus Python controller) passed 105 of 110 tests in a non-root sandbox but couldn't execute the five isolation tests, and its code wasn't available to us. This version was written from scratch along the same lines and validated end to end in a real network namespace.

## 1. Ingress: `canary-proxy` (Go)

- **Routing.** Smooth weighted round robin, as in nginx, gives exact, evenly interleaved proportions: at 90/10, green gets exactly 10 of every 100 requests and never two in a row.
- **Live weights.** Weights change through a unix admin socket (`/run/canary/proxy.sock`, mode 0600) with a line-JSON protocol: `{"cmd":"set","weights":{"app-blue":90,"app-green":10}}` and `{"cmd":"stats"}`. Both weights are swapped under one lock, so there is never an instant with no eligible backend. The proxy is never restarted to change weights, and in-flight requests are unaffected.
- **Failure attribution.** Each backend has its own `requests` and `failures` counters. A request counts once it completes, against the backend it was sent to. A failure is a 5xx answer, a refused or reset connection (the client gets 502), or no response headers within `-upstream-timeout 5s` (the client gets 504). A client that disconnects is not counted against the backend.
- **No retries.** A failed request is never resent to the other backend. That is exactly the behaviour that hides a crashed canary behind blue (the `redispatch` mutant shows it).
- **No upstream connection reuse** (`DisableKeepAlives`). Each request opens its own connection, so a backend that dies can't keep answering on a pooled connection.
- **Restart-safe.** The proxy always starts from the weights on its command line (100/0 blue), and every process start gets a new random `instance` ID in `stats`, so a scraper can tell that the counters restarted.
- **Readiness.** The pidfile is written only after both listeners are bound. SIGTERM shuts down gracefully.

Seven unit tests cover these behaviours (`go test -race`), along with `go vet`: exact SWRR split and interleaving, weight validation, per-backend attribution with no retry, timeout giving 504 plus a failure, response pass-through, zero failed requests across live weight changes under concurrent load, and the admin protocol.

## 2. Launcher: enter the namespace, drop everything, detach

`canaryctl` is run by root from the default namespace. Every long-running process (proxy and engine) is started like this:

```
nsenter --net=/run/netns/canary -- \
  setpriv --reuid=canary --regid=canary --clear-groups \
          --inh-caps=-all --bounding-set=-all --no-new-privs -- \
  setsid -f <argv>
```

with stdin from `/dev/null`, output to `/var/log/canary/*.out`, and a scrubbed environment. The namespace is entered while still privileged (`setns` needs `CAP_SYS_ADMIN`), and only then are UID, GID, groups and all capability sets dropped. `setsid -f` forks into a new session with no controlling terminal, reparented to PID 1. Port 8080 is unprivileged, so the proxy needs no capability at all.

## 3. Engine loop (`engine.py`)

```
write pid; baseline the proxy's counters; set 90/10; status(stage 1)
loop (wakes every 0.25 s):
    if the proxy's pid is gone: restart it (100/0 blue), abort, exit
    every 2.0 s on a monotonic schedule:
        scrape stats → delta of app-green (requests, failures) since the last scrape
            (a new proxy instance re-baselines at zero, never a negative delta)
        append to a 5-slot window; emit a "sample" event
        n, e = window sums; evaluated = window full and n >= 20
        if evaluated and e/n > 5 %: set 100/0, status aborted with reason, "aborted" event, exit
        if evaluated and stage held >= dwell: advance (or promote after 0/100), event
        write status (atomic tmp + rename)
```

- **Ratio of sums** over the five samples, never the mean of per-sample rates.
- **Scraping, not log tailing.** The cumulative counters are differenced every 2 s, which satisfies "access logs or metrics". The engine's sample total matches app-green's own counter within the 5 % check.
- **Abort is terminal.** The engine exits after setting 100/0, so a recovered green is never reconsidered. `canaryctl start` launches a fresh engine with a new `rollout_id` and returns once the status file shows it.
- **Proxy watchdog.** The engine checks the proxy's pid four times a second. When it is gone, the engine is already `canary` inside the namespace, so it starts a new proxy with the same identity. That proxy serves 100 % blue, and the engine then aborts. If the proxy dies between the liveness check and a scrape, the next loop iteration handles it the same way, so the engine never crashes.
- **Events** follow the instruction's schema. Every `sample`, `stage`, `aborted` and `promoted` line carries `rollout_id`, and a decision is always logged after the sample that triggered it.

## 4. Measured behaviour

Final full run: **110 passed, reward 1, 8 min 25 s**, installed from scratch.

| Scenario | Outcome |
|---|---|
| Healthy, 3 % green, 30 % blue, bursty, no-traffic→traffic | Promoted. Stage splits within ±5 pp. Zero client errors in the healthy run. Sample totals match app-green's counter. |
| 8 % errors at 50/50 | Aborted after **6.95 s** (limit 18). This is the minimum the rule allows: about 6.25 s for the window to cross 5 %, plus up to one sample. |
| Crash at 90/10, window full | Aborted after **0.97 s** (limit 8). Refused connections count as failures immediately. |
| Hang during the 0/100 bake | Aborted after **6.97 s** (limit 20): the 5 s upstream timeout plus up to one sample. |
| Proxy SIGKILLed at 50/50 | Restarted at 100 % blue and aborted after **0.16 s** (limit 10). The new proxy runs as `canary` with no capabilities. |
| After each abort | All traffic is on blue with no errors, and green gets nothing even after it recovers. |
| Status and event logs | Schema-valid on every poll. Every decision follows from the logged samples. |
| Process checks | Engine and proxy: UID/GID `canary`, no supplementary groups, all capability sets 0, `tty_nr = 0`, own session. Also checked after the proxy restart. |

Bugs found while building this version (fixed in both this and the HAProxy alternative): root-owned `events.jsonl` and launcher logs on a fresh container, and a killed proxy lingering as a zombie that the liveness check took for alive. See PROPOSAL.md, Revision 3.

## 5. Valid alternatives the verifier accepts

- **HAProxy** (kept in `solution/alternatives/haproxy/`): live weights via the runtime API, `retries 0` with no redispatch, `timeout server 5s`, and the access log sent to a unix datagram socket the engine reads.
- **nginx:** weighted `upstream` with reloads, `proxy_next_upstream off`, `proxy_read_timeout 5s`, and an access log with `$upstream_addr $upstream_status`.
- **Any home-grown proxy** in Python, Go or Bash, provided it attributes failures per backend, enforces the 5 s limit and doesn't retry. Go's `httputil.ReverseProxy` has *no* response timeout by default, so a naive Go proxy waits indefinitely on a hung green and fails the hang scenario.
