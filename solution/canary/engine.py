#!/usr/bin/env python3
"""Canary rollout engine. Runs as `canary`, detached, inside the canary netns (started by canaryctl).

Every 2 s it reads the access-log lines nginx appended since the last sample and counts app-green's
requests and failures. The window is the last five samples; the rate is summed failures over
summed requests, evaluated only with a full window and at least 20 requests. A restarted engine
(--resume) starts with an empty window."""
import argparse, collections, os, signal, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (RUN, ENGINE_PID, ACCESS_LOG, GREEN_ADDR, STAGES, W, load_state, save_state, apply,
                    nginx_pid, kill_stray_nginx, start_nginx, write_conf, write_status, event, reconcile_events)

PERIOD, WINDOW_SAMPLES, THRESHOLD, MIN_REQ, WATCH = 2.0, 5, 0.05, 20, 0.25

ap = argparse.ArgumentParser(); ap.add_argument("--resume", action="store_true")
a = ap.parse_args()
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

st = load_state()
if st["state"] != "shifting": sys.exit(0)                 # nothing to run (finished or reset meanwhile)
rid = st["rollout_id"]
with open(ENGINE_PID, "w") as f: f.write(str(os.getpid()))

class Tail:
    """New complete lines of nginx's access log since the last read (reopens if the file is replaced)."""
    def __init__(self, path):
        self.path, self.f, self.ino, self.buf = path, None, None, b""
        self.open(at_end=True)
    def open(self, at_end):
        try:
            self.f = open(self.path, "rb"); self.ino = os.fstat(self.f.fileno()).st_ino
            if at_end: self.f.seek(0, os.SEEK_END)
        except OSError: self.f = None
    def lines(self):
        try:
            if self.f is None or os.stat(self.path).st_ino != self.ino:
                if self.f: self.buf += self.f.read()
                self.open(at_end=False)
        except OSError: pass
        if self.f: self.buf += self.f.read()
        *done, self.buf = self.buf.split(b"\n")
        return [l.decode(errors="replace") for l in done]

def count(lines):
    """`$msec $upstream_addr $upstream_status $status`, one line per request (no retries are configured)."""
    req = err = 0
    for l in lines:
        p = l.split()
        if len(p) != 4 or p[1] != GREEN_ADDR or p[3] == "499": continue      # 499: the client gave up
        req += 1
        if not p[2].isdigit() or int(p[2]) >= 500: err += 1                  # 5xx, 502 refused, 504 timeout
    return req, err

tail = Tail(ACCESS_LOG)
window = collections.deque(maxlen=WINDOW_SAMPLES)
status = {"rollout_id": rid, "pid": os.getpid(), "state": "shifting", "abort_reason": None}

def totals(): return sum(x[0] for x in window), sum(x[1] for x in window)

def publish(n, e):
    w = {"seconds": 10, "green_requests": n, "green_errors": e, "error_rate": round(e / n, 5) if n else None}
    status.update(state=st["state"], stage=st["stage"], abort_reason=st["abort_reason"], window=w,
                  weights=W(*STAGES[st["stage"]]) if st["state"] == "shifting" else (W(100, 0) if st["state"] == "aborted" else W(0, 100)))
    write_status(status); return w

def abort(reason, n, e):
    w = {"seconds": 10, "green_requests": n, "green_errors": e, "error_rate": round(e / n, 5) if n else None}
    st.update(state="aborted", abort_reason=reason, window=w); save_state(st)     # 1. durable decision
    apply(st)                                                                     # 2. nginx -> 100/0
    publish(n, e)                                                                 # 3. report
    event({"type": "aborted", "rollout_id": rid, "reason": reason, "green_requests": n, "green_errors": e,
           "error_rate": w["error_rate"]})

# Start-up: finish any decision a crash cut off, then announce the (re)start.
reconcile_events(st)
if a.resume: event({"type": "resumed", "rollout_id": rid, "stage": st["stage"]})
apply(st); publish(0, 0)

nxt = time.monotonic() + PERIOD
while True:
    if not nginx_pid():
        # The ingress died under a running engine: abort, then bring nginx back at 100 % blue.
        n, e = totals()
        st.update(state="aborted", abort_reason="ingress exited during the rollout; restarted at 100% app-blue",
                  window={"seconds": 10, "green_requests": n, "green_errors": e, "error_rate": round(e / n, 5) if n else None})
        save_state(st); write_conf(100, 0); kill_stray_nginx(os.getuid()); start_nginx()
        publish(n, e)
        event({"type": "aborted", "rollout_id": rid, "reason": st["abort_reason"], "green_requests": n,
               "green_errors": e, "error_rate": st["window"]["error_rate"]})
        break
    if time.monotonic() < nxt:
        time.sleep(min(WATCH, max(0, nxt - time.monotonic()))); continue
    nxt += PERIOD
    req, err = count(tail.lines()); window.append((req, err))
    event({"type": "sample", "rollout_id": rid, "green_requests": req, "green_errors": err})
    n, e = totals()
    evaluated = len(window) == WINDOW_SAMPLES and n >= MIN_REQ
    if evaluated and e / n > THRESHOLD:
        abort(f"app-green error rate {e / n:.2%} > 5% over 10s ({e}/{n})", n, e); break
    if evaluated and time.time() - st["stage_entered_at"] >= st["dwell"]:
        w = {"seconds": 10, "green_requests": n, "green_errors": e, "error_rate": round(e / n, 5)}
        if st["stage"] == 3:
            st.update(state="promoted", window=w); save_state(st)
            publish(n, e); event({"type": "promoted", "rollout_id": rid}); break
        st.update(stage=st["stage"] + 1, stage_entered_at=time.time(), window=w); save_state(st)
        apply(st)
        event({"type": "stage", "rollout_id": rid, "stage": st["stage"], "weights": W(*STAGES[st["stage"]])})
    st["window"] = publish(n, e)
try: os.unlink(ENGINE_PID)
except OSError: pass
