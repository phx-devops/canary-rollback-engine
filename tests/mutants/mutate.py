#!/usr/bin/env python3
"""Install the golden solution into /opt/canary/bin with one deliberate defect, rebuilding the Go
proxy from (possibly patched) source.   Usage: mutate.py SOLUTION_DIR NAME   (NAME=golden for none)"""
import os, shutil, subprocess, sys, tempfile
SOL, NAME = sys.argv[1], sys.argv[2]
DST = "/opt/canary/bin"

MUTANTS = {
    # telemetry / decision mistakes (controller)
    "mean_of_ratios":   ("canary/engine.py", "if evaluated and e / n > THRESHOLD:",
                         "if evaluated and sum((x[1] / x[0] if x[0] else 0) for x in window) / len(window) > THRESHOLD:"),
    "all_5xx":          ("canary/engine.py", 's = stats(); g = s["backends"][GREEN]',
                         's = stats(); g = {k: sum(b[k] for b in s["backends"].values()) for k in ("requests", "failures")}'),
    "empty_is_healthy": ("canary/engine.py", "evaluated = len(window) == WINDOW_SAMPLES and n >= MIN_REQ",
                         "evaluated = True; n = n or 1"),
    "drain_not_zero":   ("canary/engine.py", "def abort(reason, n, e):\n    set_weights(100, 0)",
                         "def abort(reason, n, e):\n    set_weights(99, 1)"),
    "no_watchdog":      ("canary/engine.py", "    if not proxy_pid():\n        # The ingress died", "    if False:\n        # The ingress died"),
    # proxy mistakes
    "default_timeout":  ("canary/common.py", '"-upstream-timeout", "5s"', '"-upstream-timeout", "60s"'),
    "redispatch":       ("proxy/main.go", "\t\t\t\tb.requests.Add(1)\n\t\t\t\tb.failures.Add(1)\n",
                         "\t\t\t\tif alt := p.pick(); alt != nil && alt != b { alt.rp.ServeHTTP(w, r); return } // retry elsewhere\n"
                         "\t\t\t\tb.requests.Add(1)\n\t\t\t\tb.failures.Add(1)\n"),
    # isolation mistake: enter the netns but never drop root
    "root_engine":      ("canary/canaryctl", '"setpriv", f"--reuid={U.pw_uid}", f"--regid={U.pw_gid}", "--clear-groups",\n'
                         '               "--inh-caps=-all", "--bounding-set=-all", "--no-new-privs", "--", ', ""),
    # interface mistake: idle status missing schema keys
    "idle_schema":      ("canary/canaryctl", '''write_status({"rollout_id": None, "state": "idle", "stage": None, "weights": {"app-blue": 100, "app-green": 0},
                  "pid": None, "window": None, "abort_reason": None})''',
                         '''write_status({"state": "idle", "weights": {"app-blue": 100, "app-green": 0}})'''),
}

work = tempfile.mkdtemp(prefix="canary-mutant-")
src = os.path.join(work, "solution")
shutil.copytree(SOL, src, ignore=shutil.ignore_patterns("__pycache__", "alternatives"))
if NAME != "golden":
    f, old, new = MUTANTS[NAME]
    p = os.path.join(src, f)
    with open(p) as fh: s = fh.read()
    assert s.count(old) == 1, f"mutation anchor for {NAME} not found exactly once in {f}"
    with open(p, "w") as fh: fh.write(s.replace(old, new))
shutil.rmtree(DST, ignore_errors=True); os.makedirs(DST)
env = dict(os.environ, GOCACHE="/tmp/canary-gocache", GOFLAGS="-mod=mod", CGO_ENABLED="0")
subprocess.run(["go", "build", "-trimpath", "-o", f"{DST}/canary-proxy", "."], cwd=os.path.join(src, "proxy"), env=env, check=True)
for f in ("canaryctl", "engine.py", "common.py"):
    shutil.copy2(os.path.join(src, "canary", f), DST)
shutil.rmtree(work)
print(f"installed {NAME}")
