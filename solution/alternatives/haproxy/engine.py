#!/usr/bin/env python3
"""Canary rollout engine: runs as `canary`, detached, inside the canary netns."""
import argparse, collections, os, select, signal, socket, subprocess, sys, time, uuid
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import RUN, HAP_SOCK, set_weights, write_status, event

STAGES = [(90, 10), (50, 50), (0, 100)]
PERIOD, WINDOW_SAMPLES, THRESHOLD, MIN_REQ = 2.0, 5, 0.05, 20

ap = argparse.ArgumentParser(); ap.add_argument("--dwell", type=float, default=30); ap.add_argument("--id", default=None)
a = ap.parse_args()
rid = a.id or uuid.uuid4().hex[:12]

path = f"{RUN}/log.sock"
if os.path.exists(path): os.unlink(path)
ls = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM); ls.bind(path)
ls.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20); ls.setblocking(False)
open(f"{RUN}/engine.pid", "w").write(str(os.getpid()))
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

def W(b, g): return {"app-blue": b, "app-green": g}

def proxy_alive():
    """Running and not a zombie: a killed proxy lingers as a zombie until PID 1 reaps it."""
    try:
        pid = int(open(f"{RUN}/haproxy.pid").read().split()[0])
        with open(f"/proc/{pid}/stat") as f: stat = f.read()
        return stat[stat.index("(") + 1:stat.rindex(")")].startswith("haproxy") and stat[stat.rindex(")") + 2] not in "ZX"
    except (OSError, ValueError, IndexError): return False

def restart_proxy():
    """Already canary + in the netns: start a fresh proxy from the stock config, which serves 100 % blue."""
    if os.path.exists(HAP_SOCK): os.unlink(HAP_SOCK)
    subprocess.run(["haproxy", "-D", "-f", f"{RUN}/haproxy.cfg", "-p", f"{RUN}/haproxy.pid"],
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)

req = err = 0
def drain():
    global req, err
    while True:
        try: data = ls.recv(4096)
        except BlockingIOError: return
        p = data.decode(errors="replace").split()
        if "CANARY" not in p: continue
        i = p.index("CANARY")
        try: srv, st, term = p[i + 1], int(p[i + 2]), p[i + 3]
        except (IndexError, ValueError): continue
        if srv != "app-green": continue
        req += 1
        # 5xx from green, or green never answered (connect failure / server timeout).
        if st >= 500 or (st < 0 and term[:1] == "S"): err += 1

window = collections.deque(maxlen=WINDOW_SAMPLES)
stage = 0; stage_since = time.monotonic()
status = {"rollout_id": rid, "pid": os.getpid(), "state": "shifting", "abort_reason": None}

def publish(n, e, weights=None):
    status.update(stage=stage + 1, weights=weights or W(*STAGES[stage]), last_sample_at=round(time.time(), 3),
                  window={"seconds": PERIOD * WINDOW_SAMPLES, "green_requests": n, "green_errors": e,
                          "error_rate": round(e / n, 5) if n else None})
    write_status(status)

def abort(reason, n, e):
    set_weights(100, 0)
    status.update(state="aborted", abort_reason=reason); publish(n, e, W(100, 0))
    event({"type": "aborted", "rollout_id": rid, "reason": reason, "green_requests": n, "green_errors": e,
           "error_rate": round(e / n, 5) if n else None})

set_weights(*STAGES[0]); event({"type": "stage", "rollout_id": rid, "stage": 1, "weights": W(*STAGES[0])}); publish(0, 0)
nxt = time.monotonic() + PERIOD
while True:
    timeout = nxt - time.monotonic()
    if timeout > 0:
        select.select([ls], [], [], min(timeout, 0.5)); drain()
        if proxy_alive(): continue
    drain(); n = sum(x[0] for x in window); e = sum(x[1] for x in window)
    if not proxy_alive():
        # Ingress died mid-rollout: bring it back at 100 % blue and abort (never resume blind).
        restart_proxy(); abort("ingress proxy exited during the rollout; restarted at 100% app-blue", n, e); break
    nxt += PERIOD
    window.append((req, err))
    event({"type": "sample", "rollout_id": rid, "green_requests": req, "green_errors": err}); req = err = 0
    n = sum(x[0] for x in window); e = sum(x[1] for x in window)
    evaluated = len(window) == WINDOW_SAMPLES and n >= MIN_REQ
    if evaluated and e / n > THRESHOLD:
        abort(f"app-green error rate {e/n:.2%} > 5% over 10s ({e}/{n})", n, e); break
    if evaluated and time.monotonic() - stage_since >= a.dwell:
        if stage == len(STAGES) - 1:
            status["state"] = "promoted"; publish(n, e); event({"type": "promoted", "rollout_id": rid}); break
        stage += 1; stage_since = time.monotonic(); set_weights(*STAGES[stage])
        event({"type": "stage", "rollout_id": rid, "stage": stage + 1, "weights": W(*STAGES[stage])})
    publish(n, e)
os.unlink(f"{RUN}/engine.pid")
