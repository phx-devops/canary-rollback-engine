#!/usr/bin/env python3
"""Canary rollout engine. Runs as `canary`, detached, inside the canary netns (started by canaryctl).

Every 2 s it scrapes the proxy's cumulative per-backend counters and turns them into one sample of
app-green requests and failures. The window is the last five samples; the rate is summed failures
over summed requests, evaluated only with a full window and at least 20 requests."""
import argparse, collections, os, signal, subprocess, sys, time, uuid
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import RUN, LOGDIR, GREEN, proxy_argv, proxy_pid, set_weights, stats, write_status, event

STAGES = [(90, 10), (50, 50), (0, 100)]
PERIOD, WINDOW_SAMPLES, THRESHOLD, MIN_REQ = 2.0, 5, 0.05, 20
WATCH = 0.25                     # proxy liveness is checked this often between samples

ap = argparse.ArgumentParser(); ap.add_argument("--dwell", type=float, default=30); ap.add_argument("--id", default=None)
a = ap.parse_args()
rid = a.id or uuid.uuid4().hex[:12]
with open(f"{RUN}/engine.pid", "w") as f: f.write(str(os.getpid()))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

def W(b, g): return {"app-blue": b, "app-green": g}

def restart_proxy():
    """Already canary inside the netns, so the new proxy inherits exactly this identity."""
    with open(f"{LOGDIR}/proxy.out", "ab") as out:
        subprocess.Popen(["setsid", "-f", *proxy_argv()], stdin=subprocess.DEVNULL, stdout=out, stderr=out).wait(5)
    for _ in range(50):
        if proxy_pid(): return
        time.sleep(0.1)

class Counter:
    """Cumulative proxy counters -> per-interval deltas. A new proxy instance re-baselines at zero."""
    def __init__(self): self.instance, self.req, self.fail = None, 0, 0
    def delta(self):
        s = stats(); g = s["backends"][GREEN]
        if s["instance"] != self.instance: self.instance, self.req, self.fail = s["instance"], 0, 0
        d = (g["requests"] - self.req, g["failures"] - self.fail)
        self.req, self.fail = g["requests"], g["failures"]
        return d

window = collections.deque(maxlen=WINDOW_SAMPLES)
stage = 0; stage_since = time.monotonic()
status = {"rollout_id": rid, "pid": os.getpid(), "state": "shifting", "abort_reason": None}

def totals(): return sum(x[0] for x in window), sum(x[1] for x in window)

def publish(n, e, weights=None):
    status.update(stage=stage + 1, weights=weights or W(*STAGES[stage]), last_sample_at=round(time.time(), 3),
                  window={"seconds": 10, "green_requests": n, "green_errors": e,
                          "error_rate": round(e / n, 5) if n else None})
    write_status(status)

def abort(reason, n, e):
    set_weights(100, 0)
    status.update(state="aborted", abort_reason=reason); publish(n, e, W(100, 0))
    event({"type": "aborted", "rollout_id": rid, "reason": reason, "green_requests": n, "green_errors": e,
           "error_rate": round(e / n, 5) if n else None})

counter = Counter(); counter.delta()                  # baseline before green gets any traffic
set_weights(*STAGES[0]); event({"type": "stage", "rollout_id": rid, "stage": 1, "weights": W(*STAGES[0])}); publish(0, 0)
nxt = time.monotonic() + PERIOD
while True:
    if not proxy_pid():
        # The ingress died: bring it back (it starts at 100 % blue) and abort; never resume blind.
        restart_proxy(); n, e = totals()
        abort("ingress proxy exited during the rollout; restarted at 100% app-blue", n, e); break
    if time.monotonic() < nxt:
        time.sleep(min(WATCH, max(0, nxt - time.monotonic()))); continue
    nxt += PERIOD
    try: req, err = counter.delta()
    except (OSError, RuntimeError):
        if not proxy_pid(): continue          # it died between the liveness check and the scrape
        raise
    window.append((req, err))
    event({"type": "sample", "rollout_id": rid, "green_requests": req, "green_errors": err})
    n, e = totals()
    evaluated = len(window) == WINDOW_SAMPLES and n >= MIN_REQ
    if evaluated and e / n > THRESHOLD:
        abort(f"app-green error rate {e / n:.2%} > 5% over 10s ({e}/{n})", n, e); break
    if evaluated and time.monotonic() - stage_since >= a.dwell:
        if stage == len(STAGES) - 1:
            status["state"] = "promoted"; publish(n, e); event({"type": "promoted", "rollout_id": rid}); break
        stage += 1; stage_since = time.monotonic(); set_weights(*STAGES[stage])
        event({"type": "stage", "rollout_id": rid, "stage": stage + 1, "weights": W(*STAGES[stage])})
    publish(n, e)
os.unlink(f"{RUN}/engine.pid")
