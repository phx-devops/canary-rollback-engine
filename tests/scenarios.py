"""Scenario runners. Each drives one rollout end to end and returns a plain dict of observations;
the test modules only assert on these. conftest caches each scenario so it runs once per session."""
import os, threading, time
import harness as H
from harness import DWELL

# fault-onset -> abort deadline (seconds), from instruction.md
ABORT_DEADLINE = {"green8": 18, "crash": 8, "hang": 20, "proxykill": 10, "masterkill": 10, "killfault": 25}
INGRESS_KILLS = ("proxykill", "masterkill")
NO_TRAFFIC_PROMOTION_TIMEOUT = 2 * DWELL + 2 * H.WINDOW + 60  # stages 2-3, one 10s sample window, and 60s margin
# seconds after the stage is first seen before the fault starts. The crash waits until the 10 s window
# holds mostly stage-2 traffic, and still leaves a correct engine two full samples (4 s) before the
# 15 s dwell expires, so it always aborts before it could advance.
FAULT_DELAY = {"green8": 3, "crash": 9, "hang": 3, "proxykill": 3, "masterkill": 3, "killfault": 3}


def _up(cmd="up"):
    t = time.time(); p, dt = H.ctl(cmd); out = H.last_json(p)
    pid = out.get("pid") if isinstance(out, dict) else None
    return {"t": t, "t_done": t + dt, "rc": p.returncode, "out": out, "stderr": p.stderr[-300:],
            "pid_alive": H.isint(pid) and __import__("os").path.exists(f"/proc/{pid}")}


def _concurrently(cmds):
    """Run several canaryctl subcommands at the same instant (the node agent may overlap them)."""
    out, gate = [None] * len(cmds), threading.Barrier(len(cmds))
    def go(i, cmd): gate.wait(); out[i] = _up(cmd)
    threads = [threading.Thread(target=go, args=(i, c), daemon=True) for i, c in enumerate(cmds)]
    for t in threads: t.start()
    for t in threads: t.join()
    return out


def _entered(r, first, dkills=()):
    """When each stage (and the terminal state) was entered: the first time the status file showed it, or
    earlier if a kill inside that decision saw it reach nginx, the log or the status file first."""
    out = {k: first.get(("stage", k)) for k in (1, 2, 3)}
    out["term"] = r.get("t_term")
    for key, dk in dkills:
        if dk and dk.get("fired") and out.get(key): out[key] = min(out[key], dk["t"])
    return out


def _start():
    p, dt = H.ctl("start", "--dwell", str(DWELL))
    out = H.last_json(p)
    return {"rc": p.returncode, "seconds": dt, "out": out, "stderr": p.stderr[-300:],
            "rid": out.get("rollout_id") if isinstance(out, dict) else None}


def _finish_rollout(r, load, watch, events_offset):
    status, events, event_bad, status_bad, window_bad = H.status_event_snapshot(events_offset)
    r.update(rec=load.rec, first=dict(watch.first), status_bad=list(watch.bad),
             final_status=status, events=events, event_bad=event_bad)
    if watch.terminal_wait_diagnostic:
        samples = [e for e in H.rollout_events(events, r["start"]["rid"]) if e["type"] == "sample"]
        r["terminal_wait"] = {
            **watch.terminal_wait_diagnostic,
            "final_status": status,
            "latest_sample_ts": samples[-1]["ts"] if samples else None,
        }
    if status_bad: r["status_bad"].append(status_bad)
    if window_bad: r["status_bad"].append(window_bad)


def _capture_live_window(r, events_offset, label, stage, resumes, allow_promoted=False):
    deadline = time.monotonic() + 8
    snapshot = None
    while time.monotonic() < deadline:
        snapshot = H.status_event_snapshot(events_offset)
        status, events, event_bad, status_bad, window_bad = snapshot
        mine = H.rollout_events(events, r["start"]["rid"])
        resumed = sum(event["type"] == "resumed" for event in mine)
        states = ("shifting", "promoted") if allow_promoted else ("shifting",)
        if status.get("state") in states and status.get("stage") == stage and resumed >= resumes:
            break
        time.sleep(0.1)
    status, events, event_bad, status_bad, window_bad = snapshot
    mine = H.rollout_events(events, r["start"]["rid"])
    resumed = sum(event["type"] == "resumed" for event in mine)
    problems = []
    states = ("shifting", "promoted") if allow_promoted else ("shifting",)
    if status.get("state") not in states or status.get("stage") != stage:
        expected = "shifting or promoted" if allow_promoted else "shifting"
        problems.append(f"expected {expected} stage {stage}, got {status}")
    if resumed < resumes:
        problems.append(f"expected at least {resumes} resumed events, got {resumed}")
    if event_bad: problems.append(f"event errors: {event_bad[:2]}")
    if status_bad: problems.append(f"status error: {status_bad}")
    if window_bad: problems.append(f"window mismatch: {window_bad}")
    r.setdefault("live_window_checks", []).append({"label": label, "problems": problems})


def _bursty():
    """Inject two low-volume, high-error samples at stage 2, diluted by the preceding clean traffic."""
    burst_start = [None]
    burst_duration = 3.7
    def rps(_):
        started = burst_start[0]
        return 5 if started is not None and time.monotonic() - started < burst_duration else 200
    def toggler(stop):
        try:
            while not stop.is_set():
                state = H.status()
                if state.get("state") in ("promoted", "aborted"): return
                if state.get("state") == "shifting" and state.get("stage") == 2:
                    H.appctl("app-green", cmd="fault", mode="ratio", rate=0.4)
                    burst_start[0] = time.monotonic()
                    stop.wait(burst_duration)
                    return
                stop.wait(0.02)
        finally:
            H.appctl("app-green", cmd="fault", mode="none")
    def start_toggler():
        stop = threading.Event()
        thread = threading.Thread(target=toggler, args=(stop,), daemon=True)
        thread.start()
        return stop, thread
    return rps, start_toggler


def _stop_toggler(toggler):
    if toggler:
        stop, thread = toggler
        stop.set()
        thread.join()


def _workerkill(r):
    """SIGKILL one nginx worker 3 s into stage 2 and again 3 s into stage 3. nginx's master replaces it:
    the ingress never stopped running, so the rollout must carry on."""
    r["worker_kills"] = []
    def run(stop):
        for stage in (2, 3):
            while not stop.is_set():
                s = H.status()
                if s.get("state") in ("promoted", "aborted"): return
                if s.get("state") == "shifting" and s.get("stage") == stage: break
                stop.wait(0.1)
            if stop.wait(3): return
            master = H.nginx_master(); workers = H.nginx_workers(master) if master else None
            if not workers:
                r["worker_kills"].append({"stage": stage, "error": f"no nginx workers (master {master})"}); continue
            victim = min(workers); os.kill(victim, 9); t = time.time()
            stop.wait(1.5)
            r["worker_kills"].append({"stage": stage, "t": t, "pid": victim, "master_alive": H.process_alive(master),
                                      "workers_after": sorted(H.nginx_workers(master) or ())})
    stop = threading.Event(); thread = threading.Thread(target=run, args=(stop,), daemon=True); thread.start()
    return stop, thread


def run_promote(name, events_offset):
    """healthy | green3 | blue30 | bursty | workerkill: rollouts that must be promoted."""
    rps, extra = (lambda t: 60), None
    r = {"name": name, "proc": None}
    H.fresh()
    if name == "green3": H.appctl("app-green", cmd="fault", mode="ratio", rate=0.03)
    if name == "blue30": H.appctl("app-blue", cmd="fault", mode="ratio", rate=0.30)
    if name == "bursty": rps, extra = _bursty()
    if name == "workerkill": extra = lambda: _workerkill(r)
    with H.Load(rps) as L, H.Watch() as W:
        time.sleep(2); r["start"] = _start()
        toggler = extra() if extra else None
        try:
            if name == "healthy":
                time.sleep(4); r["proc"] = H.proc_snapshot()
                for k in (2, 3):                     # the live window right after each stage change
                    if W.wait(("stage", k), 2 * DWELL + 30): _capture_live_window(r, events_offset, f"stage-{k}", k, 0)
            r["term"], r["t_term"] = W.wait_terminal(4 * DWELL + 40)
            r["green_truth"] = H.appctl("app-green", cmd="stats")["requests"]
            if name == "healthy" and r["term"] == "promoted":
                r["up_after_promotion"] = _up()
                time.sleep(2)
            time.sleep(3)
        finally:
            _stop_toggler(toggler)
    r["history"] = list(W.history)
    _finish_rollout(r, L, W, events_offset)
    return r


def run_notraffic(events_offset):
    H.fresh(); r = {"name": "notraffic"}
    with H.Watch() as W:
        r["start"] = _start(); time.sleep(2 * DWELL + 5); r["status_idle_traffic"] = H.status()
        with H.Load(lambda t: 60) as L:
            r["term"], r["t_term"] = W.wait_terminal(NO_TRAFFIC_PROMOTION_TIMEOUT)
    _finish_rollout(r, L, W, events_offset)
    return r


def run_abort(name, events_offset):
    """green8 | crash | hang | proxykill | masterkill | killfault: rollouts that must abort back to app-blue."""
    # the crash happens at 50/50, where green carries real traffic: at 90/10 two stray failures
    # already exceed 5 % of a thin window, which would mask a proxy that hides most of them
    at_stage = {"green8": 2, "crash": 2, "hang": 3, "proxykill": 2, "masterkill": 2, "killfault": 2}[name]
    H.fresh(); r = {"name": name, "proc": None, "c0": None, "c1": None}
    with H.Load(lambda t: 60) as L, H.Watch() as W:
        r["start"] = _start()
        r["t_stage"] = W.wait(("stage", at_stage), 3 * DWELL + 30)
        if r["t_stage"]:
            time.sleep(FAULT_DELAY[name]); r["t_fault"] = time.time()
            if name == "proxykill":
                r["killed"] = H.listener_pids()
                for p in r["killed"]: os.kill(p, 9)
            elif name == "masterkill":
                # only the master dies; its workers live on, still holding :8080 and serving 50/50
                master = H.nginx_master(); r["killed"] = {master} if master else set()
                for p in r["killed"]: os.kill(p, 9)
                r["orphans"] = sorted(H.listener_pids())
            elif name == "killfault":
                # green starts failing at the very moment the engine dies; the node agent restarts it 2 s later
                H.appctl("app-green", cmd="fault", mode="ratio", rate=0.08); H.kill_engine()
                time.sleep(2); r["up"] = _up()
            else:
                H.appctl("app-green", cmd="fault", mode={"green8": "ratio", "crash": "crash", "hang": "hang"}[name], rate=0.08)
            r["term"], r["t_term"] = W.wait_terminal(ABORT_DEADLINE[name] + 15)
            if r["term"] == "aborted":
                time.sleep(max(0, r["t_term"] + 2 - time.time())); r["c0"] = H.appctl("app-green", cmd="stats")["requests"]
                if name in INGRESS_KILLS: time.sleep(2); r["proc"] = H.proc_snapshot(); time.sleep(4)
                else: time.sleep(6)
                H.appctl("app-green", cmd="fault", mode="none")          # green recovers: the abort must stick
                time.sleep(20); r["c1"] = H.appctl("app-green", cmd="stats")["requests"]
        else:
            r["term"] = r["t_term"] = r["t_fault"] = None
    _finish_rollout(r, L, W, events_offset)
    return r


def run_chaos(events_offset):
    """A healthy rollout under the node agent's worst luck: engine kills mid-stage, kills inside the 1->2 and
    2->3 stage changes and inside the promotion, and a node reboot, each followed by `canaryctl up`.
    Each decision kill fires the moment the decision first shows: 1->2 when nginx reloads, 2->3 when the
    stage event is logged, the promotion when the status file reports it."""
    H.fresh(); r = {"name": "chaos", "ups": [], "proc": None, "dkills": {}}
    with H.Load(lambda t: 60) as L, H.Watch(allow_missing=True) as W:
        r["start"] = _start()
        if W.wait(("stage", 1), 10):
            _capture_live_window(r, events_offset, "stage-1", 1, 0)
            time.sleep(5); H.kill_engine(); time.sleep(2); r["ups"].append(_up())        # mid stage 1
            _capture_live_window(r, events_offset, "resume-stage-1", 1, 1)
            time.sleep(1)
            r["dkills"][2] = H.kill_at_decision("nginx", 2 * DWELL + 30)                 # inside 1 -> 2
            time.sleep(1); r["ups"].append(_up())
            _capture_live_window(r, events_offset, "decision-stage-2", 2, 2)
        if W.wait(("stage", 2), 10):
            time.sleep(4); r["t_reboot"] = time.time(); H.reboot()                         # mid stage 2
            time.sleep(2); u = _up(); r["ups"].append(u); r["t_reboot_up"] = u["t_done"]
            _capture_live_window(r, events_offset, "resume-stage-2", 2, 3)
            time.sleep(3); r["proc"] = H.proc_snapshot()
            r["dkills"][3] = H.kill_at_decision("event", 2 * DWELL + 30)                 # inside 2 -> 3
            time.sleep(1); r["ups"].append(_up())
            _capture_live_window(r, events_offset, "decision-stage-3", 3, 4)
        if W.wait(("stage", 3), 10):
            time.sleep(5); H.kill_engine(); time.sleep(3); r["ups"].append(_up())           # mid stage 3
            _capture_live_window(r, events_offset, "resume-stage-3", 3, 5)
            r["dkills"]["term"] = H.kill_at_decision("status", 2 * DWELL + 30)           # inside the promotion
            time.sleep(1); r["ups"].append(_up())
        r["term"], r["t_term"] = W.wait_terminal(30)
        r["green_truth"] = H.appctl("app-green", cmd="stats")["requests"]
        time.sleep(3)
    r["history"] = list(W.history)
    r["entered"] = _entered(r, W.first, r["dkills"].items())
    _finish_rollout(r, L, W, events_offset)
    return r


def run_abort_reboot(events_offset):
    """app-green starts failing at 50/50 and the node reboots the moment the engine's abort reaches nginx.
    After `canaryctl up` the rollout is still aborted, all traffic is on app-blue, and the abort is logged
    exactly once."""
    H.fresh(); r = {"name": "abortreboot", "up": None, "c0": None, "c1": None, "dkill": None}
    with H.Load(lambda t: 60) as L, H.Watch(allow_missing=True) as W:
        r["start"] = _start()
        if W.wait(("stage", 2), 3 * DWELL + 30):
            time.sleep(3); H.appctl("app-green", cmd="fault", mode="ratio", rate=0.08)
            def reboot(): r["t_reboot"] = time.time(); H.reboot()
            r["dkill"] = H.kill_at_decision("nginx", 30, action=reboot)
            H.appctl("app-green", cmd="fault", mode="none")                # green looks healthy again
            if r["dkill"]["fired"]:
                time.sleep(2); r["up"] = _up()
                r["term"], r["t_term"] = W.wait_terminal(10)
                time.sleep(5); r["c0"] = H.appctl("app-green", cmd="stats")["requests"]
                time.sleep(15); r["c1"] = H.appctl("app-green", cmd="stats")["requests"]
            else:
                r["term"], r["t_term"] = W.wait_terminal(1)
        else:
            r["term"] = r["t_term"] = None
    r["history"] = list(W.history)
    _finish_rollout(r, L, W, events_offset)
    return r


def run_concurrent_up(events_offset):
    """The node agent overlaps its commands. Right after the engine is killed, `canaryctl status` must still
    name a live engine (it recovers the rollout first). After a second kill, three `canaryctl up` run at
    once; later three more and a `status` run at once while the engine is healthy. Either way exactly one
    engine must be left running the rollout."""
    H.fresh(); r = {"name": "concurrentup"}
    with H.Load(lambda t: 60) as L, H.Watch(allow_missing=True) as W:
        r["start"] = _start()
        if W.wait(("stage", 1), 10):
            time.sleep(4); r["killed1"] = H.kill_engine(); time.sleep(1)
            r["status_after_kill"] = _up("status")
            time.sleep(4); r["killed2"] = H.kill_engine(); time.sleep(1)
            r["ups_after_kill"] = _concurrently(["up"] * 3)
            time.sleep(4); r["pid_running"] = H.status().get("pid")
            r["ups_while_running"] = _concurrently(["up", "up", "up", "status"])
        r["term"], r["t_term"] = W.wait_terminal(4 * DWELL + 40)
        time.sleep(3)
    r["history"] = list(W.history)
    _finish_rollout(r, L, W, events_offset)
    return r


def run_ingress_check(events_offset):
    """Who actually talks to the targets? Hold a few upstream connections open (app-blue hangs briefly
    while the ingress serves 100 % blue) and inspect their owners; then again after a reboot."""
    import http.client
    def probe():
        H.appctl("app-blue", cmd="fault", mode="hang")
        def one():
            try:
                c = http.client.HTTPConnection("127.0.0.1", 8080, timeout=12); c.request("GET", "/"); c.getresponse().read()
            except Exception: pass
        ts = [threading.Thread(target=one, daemon=True) for _ in range(6)]
        for t in ts: t.start()
        time.sleep(1.0); probs = H.ingress_problems(1.5)
        H.appctl("app-blue", cmd="fault", mode="none")
        for t in ts: t.join()
        return probs
    H.fresh(); r = {"name": "ingress"}
    r["before"] = probe()
    H.reboot(); time.sleep(1); r["up"] = _up(); time.sleep(2)
    r["after"] = probe()
    return r


def run_chaos(events_offset):
    """A healthy rollout that survives four engine kills and one node reboot, each followed by
    `canaryctl up` from the node agent."""
    H.fresh(); r = {"name": "chaos", "ups": [], "proc": None}
    with H.Load(lambda t: 60) as L, H.Watch(allow_missing=True) as W:
        r["start"] = _start()
        if W.wait(("stage", 1), 10):
            time.sleep(5); H.kill_engine(); time.sleep(2); r["ups"].append(_up())        # mid stage 1
        if W.wait(("stage", 2), 3 * DWELL + 30):
            time.sleep(4); r["t_reboot"] = time.time(); H.reboot()                         # mid stage 2
            time.sleep(2); u = _up(); r["ups"].append(u); r["t_reboot_up"] = u["t_done"]
            time.sleep(3); r["proc"] = H.proc_snapshot()
        if W.wait(("stage", 3), 3 * DWELL + 40):
            H.kill_engine(); time.sleep(1); r["ups"].append(_up())                          # right after a stage change
            time.sleep(7); H.kill_engine(); time.sleep(3); r["ups"].append(_up())           # mid stage 3
        r["term"], r["t_term"] = W.wait_terminal(3 * DWELL + 60)
        r["green_truth"] = H.appctl("app-green", cmd="stats")["requests"]
        time.sleep(3)
    r.update(rec=L.rec, first=dict(W.first), status_bad=W.bad, final_status=H.status())
    r["events"], r["event_bad"] = H.read_events(events_offset)
    return r


def run_abort_reboot(events_offset):
    """A rollout aborts; then the node reboots. It must come back aborted and all-blue."""
    H.fresh(); r = {"name": "abortreboot", "up": None, "c0": None, "c1": None}
    with H.Load(lambda t: 60) as L, H.Watch(allow_missing=True) as W:
        r["start"] = _start()
        if W.wait(("stage", 2), 3 * DWELL + 30):
            time.sleep(3); H.appctl("app-green", cmd="fault", mode="ratio", rate=0.08)
            r["term"], r["t_term"] = W.wait_terminal(30)
            if r["term"] == "aborted":
                time.sleep(2); r["t_reboot"] = time.time(); H.reboot()
                H.appctl("app-green", cmd="fault", mode="none")            # green looks healthy again
                time.sleep(2); r["up"] = _up()
                time.sleep(5); r["c0"] = H.appctl("app-green", cmd="stats")["requests"]
                time.sleep(15); r["c1"] = H.appctl("app-green", cmd="stats")["requests"]
        else:
            r["term"] = r["t_term"] = None
    r.update(rec=L.rec, first=dict(W.first), status_bad=W.bad, final_status=H.status())
    r["events"], r["event_bad"] = H.read_events(events_offset)
    return r


def run_ingress_check(events_offset):
    """Who actually talks to the targets? Hold a few upstream connections open (app-blue hangs briefly
    while the ingress serves 100 % blue) and inspect their owners; then again after a reboot."""
    import http.client
    def probe():
        H.appctl("app-blue", cmd="fault", mode="hang")
        def one():
            try:
                c = http.client.HTTPConnection("127.0.0.1", 8080, timeout=12); c.request("GET", "/"); c.getresponse().read()
            except Exception: pass
        ts = [threading.Thread(target=one, daemon=True) for _ in range(6)]
        for t in ts: t.start()
        time.sleep(1.0); probs = H.ingress_problems(1.5)
        H.appctl("app-blue", cmd="fault", mode="none")
        for t in ts: t.join(8)
        return probs
    H.fresh(); r = {"name": "ingress"}
    r["before"] = probe()
    H.reboot(); time.sleep(1); r["up"] = _up(); time.sleep(2)
    r["after"] = probe()
    return r
