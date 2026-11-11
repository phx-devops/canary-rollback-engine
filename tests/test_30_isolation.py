"""Unprivileged, detached, inside the namespace (instruction.md: Running unprivileged and detached).
Checked on every non-target process in the netns during a healthy rollout and after the proxy restart."""
import pytest
import harness as H

WHEN = {"mid-rollout": "healthy", "after-proxy-restart": "proxykill"}


@pytest.mark.parametrize("when", WHEN)
def test_required_processes_live_in_namespace(scenario, when):
    snap = scenario(WHEN[when])["proc"]
    assert snap, "snapshot not taken (scenario failed earlier)"
    want = 2 if snap["shifting"] else 1
    assert len(snap["need"]) >= want, f"expected the :8080 listener{' and the engine' if snap['shifting'] else ''}, got {snap['need']}"
    missing = snap["need"] - set(snap["procs"])
    assert not missing, f"pids {missing} are not inside the canary netns"


@pytest.mark.parametrize("when", WHEN)
def test_every_toolchain_process_is_unprivileged_and_detached(scenario, when):
    snap = scenario(WHEN[when])["proc"]
    assert snap and snap["procs"], "no toolchain processes found in the namespace"
    bad = {pid: (p["name"], p["problems"]) for pid, p in snap["procs"].items() if p["problems"]}
    assert not bad, bad


def test_no_file_capabilities_or_setuid_helpers():
    assert not H.file_privilege_problems(), H.file_privilege_problems()
