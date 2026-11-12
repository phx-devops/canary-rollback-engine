"""Rollouts that must be promoted: healthy, app-green at 3 %, app-blue at 30 %, bursty traffic, no traffic."""
import pytest
import harness as H
from harness import DWELL

PROMOTE = ["healthy", "green3", "blue30", "bursty"]
ALL = PROMOTE + ["notraffic"]
SPLIT = {1: 0.10, 2: 0.50, 3: 1.00}


@pytest.mark.parametrize("name", ALL)
def test_start_returns_new_rollout_within_5s(scenario, name):
    s = scenario(name)["start"]
    assert s["rc"] == 0, s["stderr"]
    assert s["seconds"] < 5, f"took {s['seconds']:.1f}s"
    assert isinstance(s["out"], dict) and s["out"].get("state") == "shifting" and s["rid"], s["out"]


@pytest.mark.parametrize("name", ALL)
def test_rollout_promoted(scenario, name):
    r = scenario(name)
    assert r["term"] == "promoted", f"terminal state {r['term']}; status {r['final_status']}"


@pytest.mark.parametrize("name", PROMOTE)
def test_stages_in_order_and_each_held_for_dwell(scenario, name):
    r = scenario(name); f = r["first"]
    t = [f.get(("stage", i)) for i in (1, 2, 3)] + [r["t_term"]]
    assert all(t), f"stages seen at {t}"
    assert t[0] < t[1] < t[2] < t[3], t
    for i in range(3):
        assert t[i + 1] - t[i] >= DWELL - 1, f"stage {i + 1} held {t[i + 1] - t[i]:.1f}s < dwell {DWELL}s"


@pytest.mark.parametrize("stage", [1, 2, 3])
@pytest.mark.parametrize("name", PROMOTE)
def test_live_split_matches_stage(scenario, name, stage):
    r = scenario(name); f = r["first"]
    bounds = [f.get(("stage", 1)), f.get(("stage", 2)), f.get(("stage", 3)), r["t_term"]]
    assert all(bounds), "rollout did not pass through every stage"
    g = H.green_share([x for x in r["rec"] if bounds[stage - 1] + 3 <= x[0] < bounds[stage] - 1])
    assert g is not None and abs(g - SPLIT[stage]) <= 0.05, f"measured green share {g}, expected {SPLIT[stage]}"


@pytest.mark.parametrize("name", PROMOTE)
def test_all_traffic_on_green_after_promotion(scenario, name):
    r = scenario(name)
    assert r["t_term"], "not promoted"
    after = [x for x in r["rec"] if r["t_term"] + 0.5 <= x[0] < r["t_term"] + 3]
    assert H.green_share(after) == 1.0, f"green share {H.green_share(after)}"


def test_up_preserves_promoted_state_and_green_traffic(scenario):
    r = scenario("healthy")
    assert r["term"] == "promoted", r["final_status"]
    up = r["up_after_promotion"]
    status = up["out"]
    assert up["rc"] == 0, up["stderr"]
    assert status["state"] == "promoted" and status["rollout_id"] == r["start"]["rid"], status
    assert status["weights"] == H.GREEN
    after_up = [item for item in r["rec"] if up["t_done"] <= item[0] < up["t_done"] + 2]
    assert after_up and all(item[2] == 200 and item[3] == "app-green" for item in after_up)


def test_healthy_rollout_has_no_client_errors(scenario):
    bad = [x for x in scenario("healthy")["rec"] if x[2] != 200]
    assert not bad, f"{len(bad)} failed client requests, e.g. {bad[:3]}"


def test_sample_counts_match_app_green_counter(scenario):
    r = scenario("healthy")
    samples = [e for e in H.rollout_events(r["events"], r["start"]["rid"]) if e["type"] == "sample"]
    n, truth = sum(e["green_requests"] for e in samples), r["green_truth"]
    assert truth and abs(n - truth) / truth <= 0.05, f"engine counted {n}, app-green served {truth}"


def test_notraffic_holds_at_stage_1(scenario):
    s = scenario("notraffic")["status_idle_traffic"]
    assert s.get("state") == "shifting" and s.get("stage") == 1, f"after {2 * DWELL + 5}s without traffic: {s}"


@pytest.mark.parametrize("name", ALL)
def test_status_file_matched_schema_on_every_poll(scenario, name):
    bad = scenario(name)["status_bad"]
    assert not bad, bad[:2]


@pytest.mark.parametrize("name", ALL)
def test_event_log_well_formed(scenario, name):
    assert not scenario(name)["event_bad"], scenario(name)["event_bad"][:3]


@pytest.mark.parametrize("name", ALL)
def test_samples_every_2s(scenario, name):
    r = scenario(name)
    assert H.cadence_problem(H.rollout_events(r["events"], r["start"]["rid"])) is None


@pytest.mark.parametrize("name", ALL)
def test_events_record_stages_and_promotion(scenario, name):
    r = scenario(name)
    p = H.terminal_problem(H.rollout_events(r["events"], r["start"]["rid"]), "promoted")
    assert p is None, p


@pytest.mark.parametrize("name", ALL)
def test_decisions_follow_rule_on_own_samples(scenario, name):
    r = scenario(name)
    probs = H.rule_problems(H.rollout_events(r["events"], r["start"]["rid"]))
    assert not probs, probs[:3]
