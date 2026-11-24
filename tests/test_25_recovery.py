"""Crash and reboot recovery (instruction.md: Crashes and reboots)."""
import json, os, tempfile, time
import pytest
import harness as H
from harness import DWELL

SPLIT = {1: 0.10, 2: 0.50, 3: 1.00}


def exempt(r):
    """Requests may fail only between the reboot and 5 s after the following `canaryctl up` returned."""
    return (r.get("t_reboot", 0), r.get("t_reboot_up", 0) + 5)


# what each `canaryctl up` in the chaos run must report: (state, stage). Ups 2, 4 and 6 follow a kill inside
# the 1->2, 2->3 and promotion decisions; by then each decision had reached nginx, the log or the status
# file, so it must stand ("no crash or reboot may undo it").
CHAOS_UPS = [("shifting", 1), ("shifting", 2), ("shifting", 2), ("shifting", 3), ("shifting", 3), ("promoted", 3)]
CHAOS_DKILLS = {2: "nginx", 3: "event", "term": "status"}


def test_chaos_every_up_succeeds_and_resumes(scenario):
    r = scenario("chaos"); rid = r["start"]["rid"]
    assert len(r["ups"]) == len(CHAOS_UPS), f"only {len(r['ups'])} of {len(CHAOS_UPS)} restarts happened (rollout ended early?)"
    for i, (u, (state, stage)) in enumerate(zip(r["ups"], CHAOS_UPS)):
        assert u["rc"] == 0, f"up #{i + 1} failed: {u['stderr']}"
        out = u["out"] or {}
        assert out.get("rollout_id") == rid and (out.get("state"), out.get("stage")) == (state, stage), \
            f"up #{i + 1} printed {out}, expected {state} at stage {stage}"
        if state == "shifting": assert u["pid_alive"], f"up #{i + 1}: status pid is not a live process"


def test_chaos_kills_landed_on_decisions(scenario):
    """The verifier saw each decision appear (nginx reload, logged event, reported status) and killed then."""
    dk = scenario("chaos")["dkills"]
    for key, channel in CHAOS_DKILLS.items():
        assert dk.get(key, {}).get("fired"), f"decision {key} never showed through {channel}: {dk.get(key)}"


def test_chaos_decisions_never_undone(scenario):
    r = scenario("chaos")
    p = H.regression_problem(r["history"], r["start"]["rid"])
    assert p is None, p


def test_chaos_promoted(scenario):
    r = scenario("chaos")
    assert r["term"] == "promoted", f"terminal state {r['term']}; status {r['final_status']}"
    assert r["final_status"].get("rollout_id") == r["start"]["rid"]


def test_chaos_stages_in_order_and_held(scenario):
    r = scenario("chaos"); e = r["entered"]
    t = [e[1], e[2], e[3], e["term"]]
    assert all(t) and t[0] < t[1] < t[2] < t[3], t
    mine = H.rollout_events(r["events"], r["start"]["rid"])
    assert H.dwell_problem(mine, DWELL, entered=e) is None, H.dwell_problem(mine, DWELL, entered=e)
    assert H.dwell_progress_problem(mine, DWELL, entered=e) is None, H.dwell_progress_problem(mine, DWELL, entered=e)
    for i in range(3):
        assert t[i + 1] - t[i] >= DWELL - 1, f"stage {i + 1} held {t[i + 1] - t[i]:.1f}s < dwell"


@pytest.mark.parametrize("stage", [1, 2, 3])
def test_chaos_live_split(scenario, stage):
    """The split holds through engine kills, and comes back at the current stage after the reboot."""
    r = scenario("chaos"); e = r["entered"]
    b = [e[1], e[2], e[3], e["term"]]
    assert all(b), "rollout did not pass through every stage"
    lo, hi = exempt(r)
    recs = [x for x in r["rec"] if b[stage - 1] + 3 <= x[0] < b[stage] - 1 and not lo <= x[0] < hi]
    g = H.green_share(recs)
    assert g is not None and abs(g - SPLIT[stage]) <= 0.05, f"green share {g} over {len(recs)} requests"


def test_chaos_split_restored_after_reboot(scenario):
    """Within 5 s of `canaryctl up` after the reboot, the ingress serves stage 2 again (not 100 % blue)."""
    r = scenario("chaos")
    assert r.get("t_reboot_up"), "the reboot step did not run"
    t3 = r["entered"][3] or r["t_reboot_up"] + 15
    recs = [x for x in r["rec"] if r["t_reboot_up"] + 5 <= x[0] < min(t3, r["t_reboot_up"] + 12)]
    g = H.green_share(recs)
    assert g is not None and abs(g - 0.5) <= 0.05, f"green share {g} over {len(recs)} requests after the reboot"


def test_chaos_no_client_errors_outside_reboot(scenario):
    r = scenario("chaos"); lo, hi = exempt(r)
    bad = [x for x in r["rec"] if x[2] != 200 and not lo <= x[0] < hi]
    assert not bad, f"{len(bad)} failed requests outside the reboot window, e.g. {bad[:3]}"


def test_chaos_events(scenario):
    r = scenario("chaos")
    assert not r["event_bad"], r["event_bad"][:3]
    mine = H.rollout_events(r["events"], r["start"]["rid"])
    resumed = [e for e in mine if e["type"] == "resumed"]
    restarts = sum(state == "shifting" for state, _ in CHAOS_UPS)
    assert len(resumed) == restarts, f"{len(resumed)} resumed events for {restarts} restarts of a shifting rollout"
    p = H.terminal_problem(mine, "promoted")
    assert p is None, p                                   # each stage logged exactly once, one promotion
    assert H.cadence_problem(mine) is None, H.cadence_problem(mine)


def test_chaos_decisions_follow_rule_on_own_samples(scenario):
    r = scenario("chaos")
    probs = H.rule_problems(H.rollout_events(r["events"], r["start"]["rid"]))
    assert not probs, probs[:3]


def test_chaos_status_schema(scenario):
    assert not scenario("chaos")["status_bad"], scenario("chaos")["status_bad"][:2]


def test_chaos_live_windows_match_samples_after_transitions(scenario):
    checks = scenario("chaos").get("live_window_checks", [])
    assert [c["label"] for c in checks] == ["stage-1", "resume-stage-1", "decision-stage-2", "resume-stage-2",
                                            "decision-stage-3", "resume-stage-3"], checks
    assert not [check for check in checks if check["problems"]], checks


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
    assert (r.get("dkill") or {}).get("fired"), f"the abort never reached nginx: {r.get('dkill')}"
    mine = H.rollout_events(r["events"], r["start"]["rid"])
    assert [e["type"] for e in mine].count("aborted") == 1, "the abort must be logged exactly once"
    assert H.terminal_problem(mine, "aborted") is None, H.terminal_problem(mine, "aborted")
    assert not H.rule_problems(mine), H.rule_problems(mine)[:3]


def test_abort_reboot_status_schema(scenario):
    assert not scenario("abortreboot")["status_bad"], scenario("abortreboot")["status_bad"][:2]


def test_abort_reboot_never_undone(scenario):
    r = scenario("abortreboot")
    p = H.regression_problem(r["history"], r["start"]["rid"])
    assert p is None, p


# ---------------------------------------------------------------- overlapping commands from the node agent

def test_status_after_engine_death_names_a_live_engine(scenario):
    """`canaryctl status` returning `shifting` must name a live engine, even right after the engine died."""
    r = scenario("concurrentup")
    s = r.get("status_after_kill")
    assert s and s["rc"] == 0, s
    out = s["out"] or {}
    assert out.get("rollout_id") == r["start"]["rid"] and out.get("state") == "shifting", out
    assert s["pid_alive"] and out.get("pid") != r["killed1"], f"status reported pid {out.get('pid')} (killed {r['killed1']})"


def test_concurrent_ups_after_kill_start_one_engine(scenario):
    r = scenario("concurrentup")
    ups = r.get("ups_after_kill") or []
    assert len(ups) == 3 and all(u and u["rc"] == 0 for u in ups), [u and (u["rc"], u["stderr"]) for u in ups]
    outs = [u["out"] or {} for u in ups]
    assert all(o.get("state") == "shifting" and o.get("rollout_id") == r["start"]["rid"] for o in outs), outs
    pids = {o.get("pid") for o in outs}
    assert len(pids) == 1 and all(u["pid_alive"] for u in ups), f"concurrent ups reported engines {pids}"
    assert pids != {r["killed2"]}, "the killed engine was reported as running"


def test_concurrent_ups_while_running_are_harmless(scenario):
    r = scenario("concurrentup")
    ups = r.get("ups_while_running") or []
    assert len(ups) == 4 and all(u and u["rc"] == 0 for u in ups), [u and (u["rc"], u["stderr"]) for u in ups]
    pids = {(u["out"] or {}).get("pid") for u in ups}
    assert pids == {r["pid_running"]}, f"engine {r['pid_running']} was running; the commands reported {pids}"


def test_concurrentup_promoted_with_one_engine_throughout(scenario):
    r = scenario("concurrentup")
    assert r["term"] == "promoted", f"terminal state {r['term']}; status {r['final_status']}"
    assert not r["event_bad"], r["event_bad"][:3]
    assert not r["status_bad"], r["status_bad"][:2]
    mine = H.rollout_events(r["events"], r["start"]["rid"])
    resumed = [e for e in mine if e["type"] == "resumed"]
    assert len(resumed) == 2, f"{len(resumed)} resumed events for 2 engine deaths (one engine per restart)"
    assert H.cadence_problem(mine) is None, H.cadence_problem(mine)        # two engines would sample twice as often
    assert H.terminal_problem(mine, "promoted") is None, H.terminal_problem(mine, "promoted")
    assert not H.rule_problems(mine), H.rule_problems(mine)[:3]
    assert H.regression_problem(r["history"], r["start"]["rid"]) is None


def test_up_rewrites_stale_status_for_same_terminal_rollout(scenario):
    """`up` rebuilds the status from durable state, not from a stale status file. The durable state holds
    whichever rollout ran last (aborted or promoted, depending on scenario order), so take it from `up`."""
    scenario("abortreboot")                       # at least one rollout has ended before this runs
    p, _ = H.ctl("up")
    terminal = H.last_json(p)
    assert p.returncode == 0 and isinstance(terminal, dict), p.stderr[-300:]
    assert terminal.get("state") in ("aborted", "promoted") and terminal.get("stage") in (1, 2, 3), terminal
    stale = dict(terminal, state="shifting", weights=H.STAGE_W[terminal["stage"]],
                 pid=terminal["pid"] or 1, abort_reason=None)
    fd, temporary = tempfile.mkstemp(dir=os.path.dirname(H.STATUS), prefix="status-test-")
    try:
        with os.fdopen(fd, "w") as f: json.dump(stale, f)
        os.replace(temporary, H.STATUS)
        p, _ = H.ctl("up")
        out = H.last_json(p)
        assert p.returncode == 0, p.stderr[-300:]
        assert out["state"] == terminal["state"] and out["rollout_id"] == terminal["rollout_id"], out
        assert out["weights"] == terminal["weights"], out
        assert H.status() == out
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
        p, _ = H.ctl("up")
        assert p.returncode == 0, p.stderr[-300:]
