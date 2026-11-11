"""Shared by canaryctl and engine.py: paths, the proxy's admin protocol, status and event writing."""
import json, os, socket, time

BIN = os.path.dirname(os.path.abspath(__file__))
RUN = "/run/canary"; LOGDIR = "/var/log/canary"
PROXY_SOCK = f"{RUN}/proxy.sock"; PROXY_PID = f"{RUN}/proxy.pid"
STATUS = f"{RUN}/status.json"; EVENTS = f"{LOGDIR}/events.jsonl"
BLUE, GREEN = "app-blue", "app-green"

def proxy_argv():
    """The ingress always starts at 100 % app-blue, so a restarted proxy comes back serving blue."""
    return [f"{BIN}/canary-proxy", "-listen", "127.0.0.1:8080",
            "-backend", f"{BLUE}=http://127.0.0.1:9001", "-backend", f"{GREEN}=http://127.0.0.1:9002",
            "-weights", f"{BLUE}=100,{GREEN}=0", "-upstream-timeout", "5s",
            "-admin", PROXY_SOCK, "-pidfile", PROXY_PID]

def alive(pid, name):
    """True if pid is a running (not zombie) process called `name`. A killed process stays a zombie
    until its parent reaps it, and an orphan's parent is PID 1, which may reap late or never."""
    try:
        with open(f"/proc/{pid}/stat") as f: stat = f.read()
        comm, state = stat[stat.index("(") + 1:stat.rindex(")")], stat[stat.rindex(")") + 2]
        return comm == name and state not in "ZX"
    except (OSError, ValueError, IndexError): return False

def proxy_pid():
    try: pid = int(open(PROXY_PID).read().split()[0])
    except (OSError, ValueError, IndexError): return None
    return pid if alive(pid, "canary-proxy") else None

def proxy(cmd, timeout=2.0):
    """One request on the proxy's admin socket (JSON per line). Retries briefly while it starts."""
    for _ in range(30):
        try:
            s = socket.socket(socket.AF_UNIX); s.settimeout(timeout); s.connect(PROXY_SOCK); break
        except (ConnectionRefusedError, FileNotFoundError):
            s.close(); time.sleep(0.1)
    else: raise RuntimeError("proxy admin socket unavailable")
    with s:
        s.sendall((json.dumps(cmd) + "\n").encode())
        out = json.loads(s.makefile().readline())
    if "error" in out: raise RuntimeError(f"proxy: {out['error']}")
    return out

def set_weights(blue, green):
    """Both weights change in one atomic step inside the proxy: never a moment with no backend."""
    return proxy({"cmd": "set", "weights": {BLUE: blue, GREEN: green}})

def stats(): return proxy({"cmd": "stats"})

def write_status(st):
    tmp = f"{STATUS}.{os.getpid()}.tmp"
    with open(tmp, "w") as f: json.dump(st, f)
    os.replace(tmp, STATUS)

def event(ev):
    ev = {"ts": round(time.time(), 3), **ev}
    with open(EVENTS, "a") as f: f.write(json.dumps(ev) + "\n")
