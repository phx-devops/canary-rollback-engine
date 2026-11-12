#!/usr/bin/env python3
"""Install the golden solution into /opt/canary/bin with one deliberate defect.
Usage: mutate.py SOLUTION_DIR NAME   (NAME=golden for none)"""
import os, shutil, sys
SOL, NAME = sys.argv[1], sys.argv[2]
DST = "/opt/canary/bin"

# name: [(file, old, new), ...]; every anchor must occur exactly once
MUTANTS = {
    # --- telemetry / decision mistakes
    "mean_of_ratios":   [("engine.py", "if evaluated and e / n > THRESHOLD:",
                          "if evaluated and sum((x[1] / x[0] if x[0] else 0) for x in window) / len(window) > THRESHOLD:")],
    "all_5xx":          [("engine.py", 'if len(p) != 4 or p[1] != GREEN_ADDR or p[3] == "499": continue',
                          'if len(p) != 4 or p[3] == "499": continue')],
    "empty_is_healthy": [("engine.py", "evaluated = len(window) == WINDOW_SAMPLES and n >= MIN_REQ",
                          "evaluated = True; n = n or 1")],
    "drain_not_zero":   [("common.py", '    if st["state"] == "promoted": return (0, 100)',
                          '    if st["state"] == "promoted": return (0, 100)\n    if st["state"] == "aborted": return (99, 1)')],
    "no_watchdog":      [("engine.py", "    if not nginx_pid():\n        # The ingress died", "    if False:\n        # The ingress died")],
    # --- nginx defaults left in place
    "nginx_max_fails":     [("common.py", 'f"server {addr} weight={w} max_fails=0;"', 'f"server {addr} weight={w};"')],
    "nginx_next_upstream": [("common.py", "            proxy_next_upstream off;\n", "")],
    "nginx_read_timeout":  [("common.py", "            proxy_read_timeout 5s;\n", "")],
    # --- crash / reboot recovery mistakes
    "up_always_blue":   [("canaryctl", "    b, g = desired_weights(st)\n    changed = write_conf(b, g)",
                          "    b, g = desired_weights(st) if st[\"state\"] != \"shifting\" else (100, 0)   # 'safe' default\n    changed = write_conf(b, g)"),
                         ("engine.py", "apply(st); publish(0, 0)", "(None if a.resume else apply(st)); publish(0, 0)")],
    "no_resume":        [("canaryctl", "        if not engine_pid(): launch_engine(resume=True)", "        pass")],
    "abort_not_durable": [("engine.py", "    st.update(state=\"aborted\", abort_reason=reason, window=w); save_state(st)     # 1. durable decision",
                           "    st.update(state=\"aborted\", abort_reason=reason, window=w)")],
    "state_in_run":     [("common.py", 'STATE, STATUS, EVENTS = f"{LIB}/state.json"', 'STATE, STATUS, EVENTS = f"{RUN}/state.json"'),
                         ("common.py", "    d = os.open(LIB, os.O_RDONLY)", "    d = os.open(RUN, os.O_RDONLY)")],
    "no_resumed_event": [("engine.py", 'if a.resume: event({"type": "resumed", "rollout_id": rid, "stage": st["stage"]})', "pass")],
    # --- isolation / interface mistakes
    "root_engine":      [("canaryctl", '"nsenter", f"--net={NETNS}", "--", "setpriv", f"--reuid={U.pw_uid}", f"--regid={U.pw_gid}",\n'
                          '                "--clear-groups", "--inh-caps=-all", "--bounding-set=-all", "--no-new-privs", "--"]',
                          '"nsenter", f"--net={NETNS}", "--"]')],
    "idle_schema":      [("common.py", '"pid": pid, "window": st["window"], "abort_reason": st["abort_reason"]}', '"pid": pid}')],
}

src = os.path.join(SOL, "canary")
files = {f: open(os.path.join(src, f)).read() for f in ("canaryctl", "engine.py", "common.py")}
for f, old, new in MUTANTS.get(NAME, []) if NAME != "golden" else []:
    assert files[f].count(old) == 1, f"mutation anchor for {NAME} not found exactly once in {f}: {old[:60]!r}"
    files[f] = files[f].replace(old, new)
if NAME != "golden" and NAME not in MUTANTS: sys.exit(f"unknown mutant {NAME}")
shutil.rmtree(DST, ignore_errors=True); os.makedirs(DST)
for f, text in files.items():
    with open(os.path.join(DST, f), "w") as fh: fh.write(text)
    os.chmod(os.path.join(DST, f), 0o755 if f != "common.py" else 0o644)
print(f"installed {NAME}")
