"""Shared machinery for the canary-rollback-engine verifier.

Ground truth comes from the client side (a load generator inside the namespace that records which
target answered each request) and from the targets' own root-only admin sockets. The agent's status
file and event log are checked against their schemas and against that ground truth, never trusted alone.

The pure functions here (status_problem, parse_events, rule_problems, cadence_problem) take plain data
so tests/selftest can exercise them without the environment.
"""
import http.client, json, os, pwd, socket, statistics, subprocess, threading, time
from concurrent.futures import ThreadPoolExecutor

CTL = "/opt/canary/bin/canaryctl"
NETNS = "/run/netns/canary"
STATUS = "/run/canary/status.json"
EVENTS = "/var/log/canary/events.jsonl"
DWELL = 15                      # dwell used for every test rollout
THRESHOLD, MIN_REQ, WINDOW = 0.05, 20, 5

STATES = {"idle", "shifting", "promoted", "aborted"}
STAGE_W = {1: {"app-blue": 90, "app-green": 10}, 2: {"app-blue": 50, "app-green": 50}, 3: {"app-blue": 0, "app-green": 100}}
BLUE, GREEN = {"app-blue": 100, "app-green": 0}, {"app-blue": 0, "app-green": 100}
DECISION_TYPES = ("sample", "stage", "aborted", "promoted", "resumed")
BASELINE = "/etc/canary-env/baseline.txt"    # /run and /tmp entries present at container start


def isint(x): return isinstance(x, int) and not isinstance(x, bool)
def isnum(x): return isinstance(x, (int, float)) and not isinstance(x, bool)


def process_alive(pid, name=None):
    if not isint(pid) or pid <= 0: return False
    try:
        with open(f"/proc/{pid}/stat") as f: stat = f.read()
        comm = stat[stat.index("(") + 1:stat.rindex(")")]
        state = stat[stat.rindex(")") + 2:].split()[0]
        return state not in ("Z", "X") and (name is None or comm == name)
    except (OSError, ValueError, IndexError):
        return False


# ---------------------------------------------------------------- pure checks (unit-tested in selftest)

def status_problem(s):
    """None if `s` matches the status schema in instruction.md, else a description of the first problem."""
    need = ("rollout_id", "state", "stage", "weights", "pid", "window", "abort_reason")
    if not isinstance(s, dict): return "status is not a JSON object"
    missing = [k for k in need if k not in s]
    if missing: return f"missing keys {missing}"
    st = s["state"]
    if st not in STATES: return f"bad state {st!r}"
    if st == "idle":
        idle = (s["rollout_id"] is None and s["stage"] is None and s["weights"] == BLUE
                and s["pid"] is None and s["window"] is None and s["abort_reason"] is None)
        return None if idle else "idle must have null rollout_id, stage, pid, window and abort_reason, with 100/0 weights"
    if not isinstance(s["rollout_id"], str) or not s["rollout_id"]: return "rollout_id must be a non-empty string"
    if s["stage"] not in (1, 2, 3) or isinstance(s["stage"], bool): return "stage must be 1, 2 or 3"
    if st == "shifting" and not isint(s["pid"]): return "pid must be an integer while shifting"
    if st != "shifting" and not (s["pid"] is None or isint(s["pid"])): return "pid must be an integer or null"
    w = s["window"]
    if not (isinstance(w, dict) and w.get("seconds") == 10 and isint(w.get("green_requests")) and isint(w.get("green_errors"))
            and (w.get("error_rate") is None or isnum(w.get("error_rate")))):
        return f"bad window {w!r}"
    if st == "shifting" and (s["weights"] != STAGE_W[s["stage"]] or s["abort_reason"] is not None): return "shifting: weights must match the stage, abort_reason null"
    if st == "promoted" and (s["weights"] != GREEN or s["stage"] != 3 or s["abort_reason"] is not None): return "promoted: stage 3, weights 0/100, abort_reason null"
    if st == "aborted" and (s["weights"] != BLUE or not isinstance(s["abort_reason"], str) or not s["abort_reason"]): return "aborted: weights 100/0 and a non-empty abort_reason"
    return None


def parse_events(lines):
    """Validate event-log lines against the schema. Returns (events, problems)."""
    evs, bad = [], []
    for i, l in enumerate(lines, 1):
        if not l.strip(): continue
        try: e = json.loads(l)
        except ValueError: bad.append(f"line {i}: not JSON"); continue
        if not (isinstance(e, dict) and isnum(e.get("ts")) and isinstance(e.get("type"), str)):
            bad.append(f"line {i}: needs numeric ts and string type"); continue
        t = e["type"]
        if t in DECISION_TYPES and not (isinstance(e.get("rollout_id"), str) and e["rollout_id"]):
            bad.append(f"line {i}: {t} lacks rollout_id")
        elif t == "sample" and not (isint(e.get("green_requests")) and isint(e.get("green_errors"))
                                    and 0 <= e["green_errors"] <= e["green_requests"]):
            bad.append(f"line {i}: bad sample counts")
        elif t == "stage" and not (e.get("stage") in (1, 2, 3) and e.get("weights") == STAGE_W.get(e.get("stage"))):
            bad.append(f"line {i}: stage event needs stage 1-3 and that stage's weights")
        elif t == "aborted" and not (isinstance(e.get("reason"), str) and e["reason"] and isint(e.get("green_requests"))
                                     and isint(e.get("green_errors")) and "error_rate" in e):
            bad.append(f"line {i}: aborted event needs reason, green_requests, green_errors, error_rate")
        elif t == "resumed" and e.get("stage") not in (1, 2, 3):
            bad.append(f"line {i}: resumed event needs stage 1-3")
        evs.append(e)
    for a, b in zip(evs, evs[1:]):
        if b["ts"] < a["ts"] - 0.5: bad.append("timestamps go backwards"); break
    return evs, bad


def rollout_events(evs, rid): return [e for e in evs if e.get("rollout_id") == rid]


def segments(mine):
    """Sample runs of one engine lifetime: a `resumed` event starts a new segment."""
    segs = [[]]
    for e in mine:
        if e["type"] == "resumed": segs.append([])
        elif e["type"] == "sample": segs[-1].append(e)
    return segs


def cadence_problem(mine):
    samples = [e for e in mine if e["type"] == "sample"]
    gaps = [b["ts"] - a["ts"] for seg in segments(mine) for a, b in zip(seg, seg[1:])]
    if len(gaps) < 3: return f"only {len(samples)} samples"
    med = statistics.median(gaps)
    if not (1.7 <= med <= 2.3) or max(gaps) > 3.0: return f"median gap {med:.2f}s, max {max(gaps):.2f}s"
    return None


def terminal_problem(mine, expect):
    cur = None
    for e in mine:
        if e["type"] == "stage": cur = e["stage"]
        if e["type"] == "resumed" and e["stage"] != cur: return f"resumed at stage {e['stage']} but the log is at stage {cur}"
    stages = [e["stage"] for e in mine if e["type"] == "stage"]
    term = [e["type"] for e in mine if e["type"] in ("aborted", "promoted")]
    if stages[:1] != [1] or stages != sorted(stages) or len(set(stages)) != len(stages): return f"stage events {stages}"
    if term != [expect]: return f"terminal events {term}, expected one {expect}"
    if expect == "promoted" and stages != [1, 2, 3]: return f"promoted after stages {stages}"
    return None


def rule_problems(mine, error_abort=True):
    """Apply the rollback rule to the engine's own samples and list every decision it got wrong.
    A `resumed` event starts a fresh window. A decision must come right after the sample that
    triggered it (an abort because the ingress died is exempt when error_abort is False)."""
    probs, seen, prev = [], [], None
    for i, e in enumerate(mine):
        t = e["type"]
        if t == "resumed": seen = []
        elif t == "sample": seen.append(e)
        else:
            decision = (t == "stage" and e["stage"] > 1) or t == "promoted" or (t == "aborted" and error_abort)
            if decision and (prev is None or prev["type"] != "sample"):
                probs.append(f"{t} {e.get('stage', '')} does not directly follow the sample that triggered it")
            last = seen[-WINDOW:]
            n = sum(x["green_requests"] for x in last); f = sum(x["green_errors"] for x in last)
            evaluated = len(last) == WINDOW and n >= MIN_REQ
            if (t == "stage" and e["stage"] > 1) or t == "promoted":
                if not (evaluated and f / n <= THRESHOLD):
                    probs.append(f"{t} {e.get('stage', '')} decided on window {f}/{n} ({len(last)} samples)")
            if t == "aborted" and error_abort:
                if not (evaluated and f / n > THRESHOLD):
                    probs.append(f"abort decided on window {f}/{n}")
                elif (e["green_requests"], e["green_errors"]) != (n, f):
                    probs.append(f"abort logged {e['green_errors']}/{e['green_requests']} but window was {f}/{n}")
        prev = e
    # every evaluated breach must be followed at once by the abort
    win = []
    for i, e in enumerate(mine):
        if e["type"] == "resumed": win = []; continue
        if e["type"] != "sample": continue
        win.append(e); w = win[-WINDOW:]
        n = sum(x["green_requests"] for x in w); f = sum(x["green_errors"] for x in w)
        if len(w) == WINDOW and n >= MIN_REQ and f / n > THRESHOLD:
            nxt = mine[i + 1] if i + 1 < len(mine) else None
            if not (nxt and nxt["type"] == "aborted"):
                probs.append(f"breach {f}/{n} at event {i} was not acted on")
    return probs


# ---------------------------------------------------------------- environment access

HOST_NS = None   # fd of the container's default netns, set by conftest before it joins the canary netns


def ctl(*args, timeout=30):
    """Run canaryctl as root from the container's default namespace, like an operator would."""
    t = time.monotonic()
    p = subprocess.run([CTL, *args], capture_output=True, text=True, timeout=timeout,
                       preexec_fn=lambda: os.setns(HOST_NS, os.CLONE_NEWNET), pass_fds=(HOST_NS,))
    return p, time.monotonic() - t


def last_json(p):
    try: return json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError): return None


def appctl(app, **cmd):
    s = socket.socket(socket.AF_UNIX); s.connect(f"/run/apps/{app}.sock")
    s.sendall((json.dumps(cmd) + "\n").encode()); out = s.makefile().readline(); s.close()
    return json.loads(out)


def status():
    try:
        with open(STATUS) as f: return json.load(f)
    except Exception: return {}


def read_events(offset):
    try:
        with open(EVENTS, "rb") as f: f.seek(offset); return parse_events(f.read().decode(errors="replace").splitlines())
    except OSError as e: return [], [str(e)]


def fresh():
    ctl("reset")
    for a in ("app-blue", "app-green"): appctl(a, cmd="fault", mode="none"); appctl(a, cmd="reset")


def green_share(recs):
    served = [r for r in recs if r[3] in ("app-blue", "app-green")]
    return (sum(r[3] == "app-green" for r in served) / len(served)) if served else None


class Load:
    """Open-loop load generator: a new connection per request; records (sent, done, status, X-App)."""
    def __init__(self, rps): self.rps, self.rec, self.run = rps, [], True; self.pool = ThreadPoolExecutor(800)
    def one(self):
        t0, st, app = time.time(), -1, None
        try:
            c = http.client.HTTPConnection("127.0.0.1", 8080, timeout=12); c.request("GET", "/")
            r = c.getresponse(); r.read(); st, app = r.status, r.getheader("X-App"); c.close()
        except Exception: pass
        self.rec.append((t0, time.time(), st, app))
    def loop(self):
        nxt = time.time()
        while self.run:
            r = self.rps(time.time())
            if r <= 0: time.sleep(0.05); nxt = time.time(); continue
            nxt += 1.0 / r; self.pool.submit(self.one); time.sleep(max(0, nxt - time.time()))
    def __enter__(self): threading.Thread(target=self.loop, daemon=True).start(); return self
    def __exit__(self, *a): self.run = False; self.pool.shutdown(wait=True)
    def window(self, t0, t1): return [r for r in self.rec if t0 <= r[0] < t1]


class Watch:
    """Poll the status file every 0.2 s: validate it and record when each stage/terminal state first appears."""
    def __init__(self, allow_missing=False): self.first, self.run, self.bad, self.allow_missing = {}, True, [], allow_missing
    def loop(self):
        while self.run:
            s = status()
            if not s and self.allow_missing and not os.path.exists(STATUS): time.sleep(0.2); continue
            p = status_problem(s)
            if p and len(self.bad) < 5: self.bad.append(f"{p}: {s}")
            key = s.get("state") if s.get("state") in ("promoted", "aborted") else ("stage", s.get("stage"))
            self.first.setdefault(key, time.time()); time.sleep(0.2)
    def __enter__(self): threading.Thread(target=self.loop, daemon=True).start(); return self
    def __exit__(self, *a): self.run = False
    def wait(self, key, timeout):
        end = time.time() + timeout
        while time.time() < end:
            if key in self.first: return self.first[key]
            time.sleep(0.1)
    def wait_terminal(self, timeout):
        end = time.time() + timeout
        while time.time() < end:
            for k in ("promoted", "aborted"):
                if k in self.first: return k, self.first[k]
            time.sleep(0.1)
        return None, None


def listener_pids():
    """PIDs holding the LISTEN socket on 127.0.0.1:8080 (read from the canary netns' /proc/net/tcp)."""
    with open("/proc/net/tcp") as f: rows = f.read().splitlines()[1:]
    inodes = {r.split()[9] for r in rows if r.split()[1] == "0100007F:1F90" and r.split()[3] == "0A"}
    socks, out = {f"socket:[{i}]" for i in inodes}, set()
    for p in filter(str.isdigit, os.listdir("/proc")):
        try:
            if any(os.readlink(f"/proc/{p}/fd/{fd}") in socks for fd in os.listdir(f"/proc/{p}/fd")): out.add(int(p))
        except OSError: pass
    return out


def proc_snapshot():
    """Every process in the canary netns except the two targets and this verifier, with the properties the
    instruction promises to check, plus the PIDs that must be among them."""
    ns, me = os.stat(NETNS).st_ino, os.getsid(0)
    cu, apps = pwd.getpwnam("canary"), pwd.getpwnam("apps").pw_uid
    U, G = str(cu.pw_uid), str(cu.pw_gid)
    procs = {}
    for p in filter(str.isdigit, os.listdir("/proc")):
        pid = int(p)
        if pid == os.getpid(): continue
        try:
            if os.stat(f"/proc/{pid}/ns/net").st_ino != ns: continue
            with open(f"/proc/{pid}/status") as f:
                s = dict(l.split(":\t", 1) for l in f.read().splitlines() if ":\t" in l)
            with open(f"/proc/{pid}/cmdline") as f: cmd = f.read().replace("\0", " ").strip()
            if s["Uid"].split()[0] == str(apps) and "app.py" in cmd: continue
            with open(f"/proc/{pid}/stat") as f: stat = f.read().rsplit(")", 1)[1].split()
            groups = set(s.get("Groups", "").split())
            problems = []
            if set(s["Uid"].split()) != {U}: problems.append(f"Uid {s['Uid'].split()}")
            if set(s["Gid"].split()) != {G}: problems.append(f"Gid {s['Gid'].split()}")
            if not groups <= {G}: problems.append(f"Groups {sorted(groups)}")
            for k in ("CapInh", "CapPrm", "CapEff", "CapAmb"):
                if int(s[k], 16): problems.append(f"{k}={s[k].strip()}")
            if int(stat[4]) != 0: problems.append(f"tty_nr={stat[4]}")
            if int(stat[3]) == me: problems.append("shares the verifier's session")
            procs[pid] = {"name": s["Name"].strip(), "cmd": cmd[:120], "problems": problems}
        except (OSError, KeyError, ValueError): pass
    st = status()
    need = set(listener_pids())
    if st.get("state") == "shifting" and isint(st.get("pid")): need.add(st["pid"])
    return {"procs": procs, "need": need, "shifting": st.get("state") == "shifting"}


def file_privilege_problems():
    fc = subprocess.run("getcap -r /opt/canary /usr/sbin /usr/bin /usr/local/bin /usr/local/sbin 2>/dev/null",
                        shell=True, capture_output=True, text=True).stdout.strip()
    su = subprocess.run("find /opt/canary -perm /6000 -type f 2>/dev/null", shell=True, capture_output=True, text=True).stdout.strip()
    return [x for x in (fc, su) if x]


# ---------------------------------------------------------------- nginx-only ingress, reboots

NGINX_EXE = "/usr/sbin/nginx"


def _exe(pid):
    try: return os.readlink(f"/proc/{pid}/exe")
    except OSError: return None


def _socket_owners(match):
    """pid -> comm for processes owning /proc/net/tcp rows (in our netns) that satisfy match(row)."""
    with open("/proc/net/tcp") as f: rows = [r.split() for r in f.read().splitlines()[1:]]
    socks = {f"socket:[{r[9]}]" for r in rows if match(r)}
    out = {}
    if not socks: return out
    for p in filter(str.isdigit, os.listdir("/proc")):
        try:
            if any(os.readlink(f"/proc/{p}/fd/{fd}") in socks for fd in os.listdir(f"/proc/{p}/fd")):
                out[int(p)] = _exe(int(p))
        except OSError: pass
    return out


def ingress_problems(seconds=3.0):
    """Every :8080 listener, and every process holding a connection to a target, must be nginx.
    Upstream connections are short-lived, so sample repeatedly while traffic flows."""
    probs, seen_upstream = set(), 0
    for pid, exe in _socket_owners(lambda r: r[1] == "0100007F:1F90" and r[3] == "0A").items():
        if exe != NGINX_EXE: probs.add(f":8080 listener pid {pid} is {exe}")
    end = time.time() + seconds
    while time.time() < end:
        owners = _socket_owners(lambda r: r[2] in ("0100007F:2329", "0100007F:232A") and r[3] == "01")
        seen_upstream += len(owners)
        for pid, exe in owners.items():
            if pid != os.getpid() and exe != NGINX_EXE: probs.add(f"pid {pid} ({exe}) holds a connection to a target")
        time.sleep(0.05)
    if not seen_upstream: probs.add("no upstream connections observed")
    return sorted(probs)


def reboot():
    """Simulate a node reboot: kill every process running as canary, lose /run and /tmp beyond what
    was there when the container started. /var/lib, /var/log and /opt survive."""
    import shutil
    uid = pwd.getpwnam("canary").pw_uid
    for _ in range(3):
        subprocess.run(["pkill", "-9", "-u", str(uid)], capture_output=True)
        time.sleep(0.3)
    try:
        with open(BASELINE) as f: keep = set(f.read().split())
    except OSError: keep = {"/run/netns", "/run/apps"}
    for top in ("/run", "/tmp"):
        for name in os.listdir(top):
            path = f"{top}/{name}"
            if path in keep: continue
            try:
                if os.path.isdir(path) and not os.path.islink(path): shutil.rmtree(path)
                else: os.unlink(path)
            except OSError: pass


def kill_engine():
    st = status(); pid = st.get("pid")
    if isint(pid):
        try: os.kill(pid, 9)
        except OSError: pass
    return pid
