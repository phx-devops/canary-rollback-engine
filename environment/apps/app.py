#!/usr/bin/env python3
"""Stand-in for the app-blue / app-green containers.

Main port: any GET returns 200 JSON and an X-App header.
Admin: line-delimited JSON over a unix socket (root-only directory), used by the
verifier to inject faults and read ground-truth counters.
  {"cmd":"stats"} -> {"requests":N,"errors":M}
  {"cmd":"fault","mode":"none|ratio|crash|hang","rate":0.08}
  {"cmd":"reset"}
"""
import argparse, json, os, socket, socketserver, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser()
ap.add_argument("--name", required=True)
ap.add_argument("--version", required=True)
ap.add_argument("--port", type=int, required=True)
ap.add_argument("--admin", required=True)
a = ap.parse_args()

lock = threading.Lock()
state = {"mode": "none", "rate": 0.0, "k": 0, "requests": 0, "errors": 0}

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *x): pass
    def do_GET(self):
        with lock:
            state["requests"] += 1
            mode, rate = state["mode"], state["rate"]
            err = False
            if mode == "ratio":
                state["k"] += 1; k = state["k"]
                err = int(k * rate) != int((k - 1) * rate)
            if err: state["errors"] += 1
        if mode == "hang":
            time.sleep(120)
        body = json.dumps({"app": a.name, "version": a.version}).encode()
        self.send_response(500 if err else 200)
        self.send_header("X-App", a.name); self.send_header("X-App-Version", a.version)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)

class Srv(ThreadingHTTPServer):
    """Tracks open client connections so a simulated crash can drop them all, as a real crash would
    (otherwise a keep-alive connection would keep being served after the listener closed)."""
    daemon_threads = True; allow_reuse_address = True; request_queue_size = 512
    def __init__(self, *a, **kw):
        self.open_conns = set(); self.conns_lock = threading.Lock(); super().__init__(*a, **kw)
    def process_request(self, request, client_address):
        with self.conns_lock: self.open_conns.add(request)
        super().process_request(request, client_address)
    def shutdown_request(self, request):
        with self.conns_lock: self.open_conns.discard(request)
        super().shutdown_request(request)
    def drop_all(self):
        with self.conns_lock: conns = list(self.open_conns)
        for c in conns:
            try: c.shutdown(socket.SHUT_RDWR)
            except OSError: pass

srv = None
def serve():
    global srv
    srv = Srv(("127.0.0.1", a.port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
def crash():
    global srv
    if srv: srv.shutdown(); srv.server_close(); srv.drop_all(); srv = None

class Admin(socketserver.StreamRequestHandler):
    def handle(self):
        for line in self.rfile:
            c = json.loads(line); out = {"ok": True}
            with lock:
                if c["cmd"] == "stats": out = {"requests": state["requests"], "errors": state["errors"], "mode": state["mode"]}
                elif c["cmd"] == "reset": state.update(requests=0, errors=0, k=0)
                elif c["cmd"] == "fault":
                    state.update(mode=c["mode"], rate=float(c.get("rate", 0)), k=0)
            if c["cmd"] == "fault":
                if c["mode"] == "crash": crash()
                elif srv is None: serve()
            self.wfile.write((json.dumps(out) + "\n").encode()); self.wfile.flush()

class AdminSrv(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

serve()
if os.path.exists(a.admin): os.unlink(a.admin)
AdminSrv(a.admin, Admin).serve_forever()
