# Golden solution — `canary-rollback-engine`

The golden solution is a **Python controller driving the system nginx** (`/usr/sbin/nginx`, nginx-light 1.24). `solution/solve.sh` installs three files into `/opt/canary/bin/` and runs `canaryctl up`. It passes the full verifier suite (`tests/test.sh`, reward 1).

```
solution/
├── solve.sh              # install → canaryctl up (nginx at 100 % blue)
└── canary/
    ├── canaryctl         # CLI: up | start | status | reset; privileged launcher; recovery
    ├── engine.py         # rollout loop: tail access log, window, decisions, nginx watchdog
    └── common.py         # durable state, nginx config + control, status, events, reconciliation
```

## 1. nginx, configured to tell the truth

`common.render_conf()` writes `/var/lib/canary/nginx.conf` from the durable state:

```nginx
upstream apps {
    server 127.0.0.1:9001 weight=50 max_fails=0;   # or "down" when its weight is 0
    server 127.0.0.1:9002 weight=50 max_fails=0;
}
location / {
    proxy_pass http://apps;
    proxy_next_upstream off;
    proxy_connect_timeout 1s;  proxy_send_timeout 5s;  proxy_read_timeout 5s;
}
log_format canary '$msec $upstream_addr $upstream_status $status';
```

- **`max_fails=0`** turns off passive health checking. Otherwise each worker pulls green for `fail_timeout` (10 s) after one connection error or timeout, which changes the split behind the engine's back and starves the window of green requests.
- **`proxy_next_upstream off`**: a failed green request is never retried on blue, so the client sees the failure and the log line has exactly one upstream.
- **`proxy_read_timeout 5s`** bounds waiting for response headers from a hung upstream. The default 60 s would hide a hang.
- **Zero weights become `down`**, because `weight=0` is invalid in nginx.
- **Split changes are graceful reloads** (SIGHUP to the master): new workers take the new split while old workers finish their requests. No client request fails.
- **Unprivileged paths.** pid in `/run/canary`; temp dirs in `/var/lib/canary/nginx/*`; access and error logs in `/var/log/canary`; and `-e` so even the start-up error log is writable by `canary`.

## 2. Durable state and recovery

- **State.** `/var/lib/canary/state.json` survives a reboot. It holds the rollout ID, state, stage, dwell, stage entry time (wall clock), abort reason and the last window. It is written atomically, with an fsync of both the file and the directory.
- **Write-ahead ordering.** Every decision is *persisted first, then applied to nginx, then reported* (status and event). A crash at any point leaves state that recovery can finish.
- **`canaryctl up`** (the node agent's call after a crash or reboot):
  1. Recreates `/run/canary` and the canary-owned files.
  2. Renders nginx from state (the current stage's split while shifting, 100/0 for aborted or idle, 0/100 for promoted), kills any orphaned workers and starts nginx. It leaves nginx alone if a live engine owns it.
  3. If the rollout is shifting and no engine runs, starts the engine with `--resume`. Otherwise it logs any decision the crash cut off and rewrites the status.
- **A resumed engine** first reconciles the event log against state, logging any persisted-but-unlogged stage, abort or promotion right after the sample that triggered it. It then logs `resumed`, re-applies the stage's split, and starts with an **empty window**. Dwell is measured from the persisted stage entry time, so downtime counts, but a decision still needs five fresh samples.
- **An abort is durable.** After a reboot, `up` brings nginx back at 100 % blue and reports `aborted`. Green gets nothing.

## 3. Engine loop (`engine.py`)

```
reconcile events with state; if resumed: log "resumed"; apply the stage's split; publish status
loop (wakes every 0.25 s):
    if nginx's master is gone (zombie-aware): persist abort → render 100/0 → kill stray workers →
        start nginx → report → exit
    every 2.0 s on a monotonic schedule:
        read new access-log lines; count lines whose $upstream_addr is app-green
            (failure: $upstream_status ≥ 500 or missing; 499 = client gave up, ignored)
        append to the 5-slot window; log "sample"
        evaluated = window full and ≥ 20 green requests
        if evaluated and rate > 5 %: persist abort → nginx 100/0 → report → exit
        if evaluated and now − stage_entered_at ≥ dwell: persist → apply → log (advance or promote)
```

The access log is tailed by inode, so it is reopened if replaced. The engine starts reading at the end of the file, so a restarted engine never counts traffic from before its own window.

## 4. Launcher and isolation

`canaryctl` runs as root in the default namespace. Every long-running process starts as:

```
nsenter --net=/run/netns/canary -- setpriv --reuid=canary --regid=canary --clear-groups \
        --inh-caps=-all --bounding-set=-all --no-new-privs -- …
```

followed by either `/usr/sbin/nginx -e … -c …` or `setsid -f python3 -I engine.py [--resume]`.

nginx daemonises itself (its own session, no TTY). The engine gets `setsid -f`. Port 8080 needs no capability. When the engine restarts nginx after the ingress dies, nginx inherits the engine's identity.

## 5. Measured behaviour

Final full run: **144 passed, reward 1, 11 min 49 s**, installed from scratch.

| Scenario | Outcome |
|---|---|
| Healthy, green 3 %, blue 30 %, bursty, no traffic → traffic | Promoted. Stage splits within ±5 pp. Zero client errors in the healthy run. Sample totals match app-green's counter. |
| 8 % errors at 50/50 | Aborted after **6.92 s** (limit 18), with the split held at 50/50 until then. This is the minimum the rule allows (about 6.25 s plus one sample). |
| Crash at 50/50 | Aborted after **0.92 s** (limit 8), without advancing. |
| Hang during the 0/100 bake | Aborted after **6.91 s** (limit 20): the 5 s upstream timeout plus one sample. |
| Ingress SIGKILLed at 50/50 | nginx restarted at 100 % blue (as `canary`, no capabilities), aborted after **0.20 s** (limit 10). |
| Engine killed as green starts failing; `up` after 2 s | Resumed, then aborted after **12.52 s** (limit 25): the 2 s restart delay plus a fresh 10 s window. |
| Chaos (3 engine kills + 1 reboot) | Promoted under the same ID. Split restored at 50/50 within 5 s of the post-reboot `up`. 4 `resumed` events, each stage logged once. No client errors outside the reboot outage. |
| Abort, then reboot | Comes back `aborted` at 100 % blue. Green gets nothing. The abort is logged once. |
| Ingress identity, before and after a reboot | Only `/usr/sbin/nginx` listens on `:8080` and holds connections to the targets. |
| Isolation (mid-rollout, after the ingress restart, after a reboot) | Engine and every nginx process: UID/GID `canary`, no groups, all capability sets 0, no TTY, their own session. |

## 6. What an agent may do differently

- **Telemetry:** syslog-to-unix-socket instead of tailing a file; JSON log format; any attribution scheme that keys on the upstream address.
- **Split changes:** the nginx-plus API isn't available. Reloading is the only route, but how the config is generated and validated is free (`nginx -t` first, templating, includes).
- **State:** SQLite or another file layout, as long as it lives somewhere a reboot doesn't wipe and decisions are persisted before they are applied.
- **Privilege drop:** `setpriv`, `capsh`, Python `os.setns` + `setgroups`/`setresgid`/`setresuid`, or Go `unix.Setns` on a locked thread. Any method that reaches the specified `/proc` end state passes.
