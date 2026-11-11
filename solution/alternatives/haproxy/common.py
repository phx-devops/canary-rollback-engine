import json, os, socket, time
RUN = "/run/canary"; LOGDIR = "/var/log/canary"
HAP_SOCK = f"{RUN}/haproxy.sock"; STATUS = f"{RUN}/status.json"; EVENTS = f"{LOGDIR}/events.jsonl"

def hap(cmd, timeout=2.0):
    for attempt in range(30):                     # the admin socket can lag the pidfile right after start
        try:
            s = socket.socket(socket.AF_UNIX); s.settimeout(timeout); s.connect(HAP_SOCK); break
        except (ConnectionRefusedError, FileNotFoundError):
            s.close(); time.sleep(0.1)
    else: raise RuntimeError("haproxy admin socket unavailable")
    s.sendall((cmd + "\n").encode()); out = b""
    while (chunk := s.recv(65536)): out += chunk
    s.close(); return out.decode()

def get_weights():
    w = {}
    for line in hap("show servers state apps").splitlines():
        p = line.split()
        if len(p) > 7 and p[3] in ("app-blue", "app-green"): w[p[3]] = int(p[7])
    return w

def set_weights(blue, green):
    """Raise the gaining side before lowering the losing side: both weights are never 0 at once."""
    cur = get_weights()
    order = [("app-green", green), ("app-blue", blue)] if green >= cur.get("app-green", 0) else [("app-blue", blue), ("app-green", green)]
    for srv, w in order: hap(f"set server apps/{srv} weight {w}")

def write_status(st):
    tmp = f"{STATUS}.{os.getpid()}.tmp"
    with open(tmp, "w") as f: json.dump(st, f)
    os.replace(tmp, STATUS)

def event(ev):
    ev = {"ts": round(time.time(), 3), **ev}
    with open(EVENTS, "a") as f: f.write(json.dumps(ev) + "\n")
