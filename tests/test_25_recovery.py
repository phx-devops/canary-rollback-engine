"""Crash and reboot recovery (instruction.md: Crashes and reboots)."""
import time
import pytest
import harness as H
from harness import DWELL

SPLIT = {1: 0.10, 2: 0.50, 3: 1.00}


def exempt(r):
    """Requests may fail only between the reboot and 5 s after the following `canaryctl up` returned."""
    return (r.get("t_reboot", 0), r.get("t_reboot_up", 0) + 5)


def test_chaos_every_up_succeeds_and_resumes(scenario):
    r = scenario("chaos"); rid = r["start"]["rid"]
    assert len(r["ups"]) == 4, f"only {len(r['ups'])} of 4 restarts happened (rollout ended early?)"
    for i, u in enumerate(r["ups"]):
        assert u["rc"] == 0, f"up #{i + 1} failed: {u['stderr']}"
        out = u["out"] or {}
        assert out.get("rollout_id") == rid and out.get("state") == "shifting", f"up #{i + 1} printed {out}"
        assert u["pid_alive"], f"up #{i + 1}: status pid is not a live process"


def test_chaos_promoted(scenario):
    r = scenario("chaos")
    assert r["term"] == "promoted", f"terminal state {r['term']}; status {r['final_status']}"
    assert r["final_status"].get("rollout_id") == r["start"]["rid"]


def test_chaos_stages_in_order_and_held(scenario):
    r = scenario("chaos"); f = r["first"]
    t = [f.get(("stage", i)) for i in (1, 2, 3)] + [r["t_term"]]
    assert all(t) and t[0] < t[1] < t[2] < t[3], t
    for i in range(3):
        assert t[i + 1] - t[i] >= DWELL - 1, f"stage {i + 1} held {t[i + 1] - t[i]:.1f}s < dwell"


@pytest.mark.parametrize("stage", [1, 2, 3])
def test_chaos_live_split(scenario, stage):
    """The split holds through engine kills, and comes back at the current stage after the reboot."""
    r = scenario("chaos"); f = r["first"]
    b = [f.get(("stage", 1)), f.get(("stage", 2)), f.get(("stage", 3)), r["t_term"]]
    assert all(b), "rollout did not pass through every stage"
    lo, hi = exempt(r)
    recs = [x for x in r["rec"] if b[stage - 1] + 3 <= x[0] < b[stage] - 1 and not lo <= x[0] < hi]
    g = H.green_share(recs)
    assert g is not None and abs(g - SPLIT[stage]) <= 0.05, f"green share {g} over {len(recs)} requests"


def test_chaos_split_restored_after_reboot(scenario):
    """Within 5 s of `canaryctl up` after the reboot, the ingress serves stage 2 again (not 100 % blue)."""
    r = scenario("chaos")
    assert r.get("t_reboot_up"), "the reboot step did not run"
    t3 = r["first"].get(("stage", 3)) or r["t_reboot_up"] + 15
    recs = [x for x in r["rec"] if r["t_reboot_up"] + 5 <= x[0] < min(t3, r["t_reboot_up"] + 12)]
    g = H.green_share(recs)
    assert g is not None and abs(g - 0.5) <= 0.07, f"green share {g} over {len(recs)} requests after the reboot"


def test_chaos_no_client_errors_outside_reboot(scenario):
    r = scenario("chaos"); lo, hi = exempt(r)
    bad = [x for x in r["rec"] if x[2] != 200 and not lo <= x[0] < hi]
    assert not bad, f"{len(bad)} failed requests outside the reboot window, e.g. {bad[:3]}"


def test_chaos_events(scenario):
    r = scenario("chaos")
    assert not r["event_bad"], r["event_bad"][:3]
    mine = H.rollout_events(r["events"], r["start"]["rid"])
    resumed = [e for e in mine if e["type"] == "resumed"]
    assert len(resumed) == 4, f"{len(resumed)} resumed events for 4 restarts"
    p = H.terminal_problem(mine, "promoted")
    assert p is None, p                                   # each stage logged exactly once, one promotion
    assert H.cadence_problem(mine) is None, H.cadence_problem(mine)


def test_chaos_decisions_follow_rule_on_own_samples(scenario):
    r = scenario("chaos")
    probs = H.rule_problems(H.rollout_events(r["events"], r["start"]["rid"]))
    assert not probs, probs[:3]


def test_chaos_status_schema(scenario):
    assert not scenario("chaos")["status_bad"], scenario("chaos")["status_bad"][:2]


def test_abort_survives_reboot(scenario):
    r = scenario("abortreboot")
    assert r["term"] == "aborted", f"did not abort before the reboot: {r['final_status']}"
    u = r["up"]
    assert u and u["rc"] == 0, u
    assert (u["out"] or {}).get("state") == "aborted" and u["out"].get("weights") == H.BLUE, u["out"]
    s = r["final_status"]
    assert s.get("state") == "aborted" and s.get("rollout_id") == r["start"]["rid"] and s.get("abort_reason"), s


def test_abort_reboot_traffic_stays_blue(scenario):
    r = scenario("abortreboot")
    assert r["up"], "the reboot step did not run"
    after = [x for x in r["rec"] if x[0] >= r["up"]["t_done"] + 5]
    assert after and all(x[2] == 200 and x[3] == "app-blue" for x in after), f"green share {H.green_share(after)}"
    assert r["c1"] == r["c0"], f"app-green served {r['c1'] - r['c0']} requests after the reboot"


def test_abort_reboot_events(scenario):
    r = scenario("abortreboot")
    assert not r["event_bad"], r["event_bad"][:3]
    mine = H.rollout_events(r["events"], r["start"]["rid"])
    assert [e["type"] for e in mine].count("aborted") == 1, "the abort must be logged exactly once"
    assert H.terminal_problem(mine, "aborted") is None, H.terminal_problem(mine, "aborted")
    assert not H.rule_problems(mine), H.rule_problems(mine)[:3]
