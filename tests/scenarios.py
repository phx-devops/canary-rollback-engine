"""Scenario runners. Each drives one rollout end to end and returns a plain dict of observations;
the test modules only assert on these. conftest caches each scenario so it runs once per session."""
import threading, time
import harness as H
from harness import DWELL

# fault-onset -> abort deadline (seconds), from instruction.md
ABORT_DEADLINE = {"green8": 18, "crash": 8, "hang": 20, "proxykill": 10}
# seconds after the stage is first seen before the fault starts. The crash waits until the 10 s window
# is full (still within stage 1, since the dwell is 15 s), so the test measures how fast a crash is
# detected rather than when the first window fills.
FAULT_DELAY = {"green8": 3, "crash": 11, "hang": 3, "proxykill": 3}


def _start():
    p, dt = H.ctl("start", "--dwell", str(DWELL))
    out = H.last_json(p)
    return {"rc": p.returncode, "seconds": dt, "out": out, "stderr": p.stderr[-300:],
            "rid": out.get("rollout_id") if isinstance(out, dict) else None}


def _bursty():
    """4 s at 10 rps with app-green failing 40 %, then 6 s at 200 rps clean. The true 10 s ratio stays
    around 1-2 %; averaging per-sample percentages exceeds 5 % at every sampling alignment."""
    def rps(t): return 10 if (t % 10) < 4 else 200
    def toggler():
        end, low = time.time() + 4 * DWELL + 40, None
        while time.time() < end and H.status().get("state") not in ("promoted", "aborted"):
            now_low = (time.time() % 10) < 4
            if now_low != low:
                H.appctl("app-green", cmd="fault", mode="ratio" if now_low else "none", rate=0.4); low = now_low
            time.sleep(0.02)
        H.appctl("app-green", cmd="fault", mode="none")
    return rps, lambda: threading.Thread(target=toggler, daemon=True).start()


def run_promote(name, events_offset):
    """healthy | green3 | blue30 | bursty: rollouts that must be promoted."""
    rps, extra = (lambda t: 60), None
    H.fresh()
    if name == "green3": H.appctl("app-green", cmd="fault", mode="ratio", rate=0.03)
    if name == "blue30": H.appctl("app-blue", cmd="fault", mode="ratio", rate=0.30)
    if name == "bursty": rps, extra = _bursty()
    r = {"name": name, "proc": None}
    with H.Load(rps) as L, H.Watch() as W:
        time.sleep(2); r["start"] = _start()
        if extra: extra()
        if name == "healthy": time.sleep(4); r["proc"] = H.proc_snapshot()
        r["term"], r["t_term"] = W.wait_terminal(4 * DWELL + 40)
        r["green_truth"] = H.appctl("app-green", cmd="stats")["requests"]
        time.sleep(3)
    r.update(rec=L.rec, first=dict(W.first), status_bad=W.bad, final_status=H.status())
    r["events"], r["event_bad"] = H.read_events(events_offset)
    return r


def run_notraffic(events_offset):
    H.fresh(); r = {"name": "notraffic"}
    with H.Watch() as W:
        r["start"] = _start(); time.sleep(2 * DWELL + 5); r["status_idle_traffic"] = H.status()
        with H.Load(lambda t: 60) as L:
            r["term"], r["t_term"] = W.wait_terminal(4 * DWELL + 40)
    r.update(rec=L.rec, first=dict(W.first), status_bad=W.bad, final_status=H.status())
    r["events"], r["event_bad"] = H.read_events(events_offset)
    return r


def run_abort(name, events_offset):
    """green8 | crash | hang | proxykill: rollouts that must abort back to app-blue."""
    at_stage = {"green8": 2, "crash": 1, "hang": 3, "proxykill": 2}[name]
    H.fresh(); r = {"name": name, "proc": None, "c0": None, "c1": None}
    with H.Load(lambda t: 60) as L, H.Watch() as W:
        r["start"] = _start()
        r["t_stage"] = W.wait(("stage", at_stage), 3 * DWELL + 30)
        if r["t_stage"]:
            time.sleep(FAULT_DELAY[name]); r["t_fault"] = time.time()
            if name == "proxykill":
                r["killed"] = H.listener_pids()
                for p in r["killed"]: __import__("os").kill(p, 9)
            else:
                H.appctl("app-green", cmd="fault", mode={"green8": "ratio", "crash": "crash", "hang": "hang"}[name], rate=0.08)
            r["term"], r["t_term"] = W.wait_terminal(ABORT_DEADLINE[name] + 15)
            if r["term"] == "aborted":
                time.sleep(max(0, r["t_term"] + 2 - time.time())); r["c0"] = H.appctl("app-green", cmd="stats")["requests"]
                if name == "proxykill": time.sleep(2); r["proc"] = H.proc_snapshot(); time.sleep(4)
                else: time.sleep(6)
                H.appctl("app-green", cmd="fault", mode="none")          # green recovers: the abort must stick
                time.sleep(20); r["c1"] = H.appctl("app-green", cmd="stats")["requests"]
        else:
            r["term"] = r["t_term"] = r["t_fault"] = None
    r.update(rec=L.rec, first=dict(W.first), status_bad=W.bad, final_status=H.status())
    r["events"], r["event_bad"] = H.read_events(events_offset)
    return r
