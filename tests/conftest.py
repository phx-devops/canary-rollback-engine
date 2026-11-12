"""Session setup: join the canary netns (the verifier's own traffic runs inside it), remember the
default netns for canaryctl, and cache each scenario so it runs once no matter how many tests read it."""
import os, sys
import pytest

sys.path.insert(0, os.path.dirname(__file__))
import harness as H
import scenarios as S

if os.environ.get("CANARY_SELFTEST") != "1":
    H.HOST_NS = os.open("/proc/self/ns/net", os.O_RDONLY)
    os.setns(os.open(H.NETNS, os.O_RDONLY), os.CLONE_NEWNET)
    try: EVENTS_OFFSET = os.path.getsize(H.EVENTS)        # judge only event lines written during grading
    except OSError: EVENTS_OFFSET = 0

PROMOTE = ["healthy", "green3", "blue30", "bursty"]
ABORT = ["green8", "crash", "hang", "proxykill", "killfault"]

_cache = {}


@pytest.fixture(scope="session")
def scenario():
    def get(name):
        if name not in _cache:
            if name in PROMOTE: _cache[name] = S.run_promote(name, EVENTS_OFFSET)
            elif name in ABORT: _cache[name] = S.run_abort(name, EVENTS_OFFSET)
            elif name == "notraffic": _cache[name] = S.run_notraffic(EVENTS_OFFSET)
            elif name == "chaos": _cache[name] = S.run_chaos(EVENTS_OFFSET)
            elif name == "abortreboot": _cache[name] = S.run_abort_reboot(EVENTS_OFFSET)
            elif name == "ingress": _cache[name] = S.run_ingress_check(EVENTS_OFFSET)
            else: raise KeyError(name)
        return _cache[name]
    return get


def pytest_sessionfinish(session, exitstatus):
    """Record measured fault-to-abort latency for every abort scenario that ran (for the report)."""
    out = {name: round(r["t_term"] - r["t_fault"], 2) for name, r in _cache.items()
           if name in ABORT and r.get("t_term") and r.get("t_fault")}
    if out:
        import json
        os.makedirs("/logs/verifier", exist_ok=True)
        with open("/logs/verifier/abort_latency.json", "w") as f: json.dump(out, f, indent=1)
