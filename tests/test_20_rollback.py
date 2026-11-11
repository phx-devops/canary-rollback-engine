"""Rollouts that must abort: app-green at 8 % (50/50), crash (90/10), hang (0/100 bake), proxy killed (50/50)."""
import pytest
import harness as H
from scenarios import ABORT_DEADLINE

ABORT = ["green8", "crash", "hang", "proxykill"]


@pytest.mark.parametrize("name", ABORT)
def test_reached_fault_stage(scenario, name):
    r = scenario(name)
    assert r["start"]["rc"] == 0, r["start"]["stderr"]
    assert r["t_stage"], f"never reached the stage where the fault is injected; status {r['final_status']}"


@pytest.mark.parametrize("name", ABORT)
def test_aborted_within_deadline(scenario, name):
    r = scenario(name)
    assert r["term"] == "aborted", f"terminal state {r['term']}; status {r['final_status']}"
    lat = r["t_term"] - r["t_fault"]
    assert lat <= ABORT_DEADLINE[name], f"aborted {lat:.1f}s after the fault, limit {ABORT_DEADLINE[name]}s"


@pytest.mark.parametrize("name", ABORT)
def test_traffic_back_on_blue_without_errors(scenario, name):
    r = scenario(name)
    assert r["t_term"], "no abort"
    t0 = r["t_term"] + 1 if name != "proxykill" else max(r["t_term"] + 1, r["t_fault"] + 10)
    after = [x for x in r["rec"] if t0 <= x[0] < r["t_term"] + 28]
    assert after, "no requests recorded after the abort"
    bad = [x for x in after if x[2] != 200 or x[3] != "app-blue"]
    assert not bad, f"{len(bad)} of {len(after)} requests not a 200 from app-blue, e.g. {bad[:3]}"


@pytest.mark.parametrize("name", ABORT)
def test_green_gets_nothing_after_abort_even_once_healthy(scenario, name):
    r = scenario(name)
    assert r["c0"] is not None, "no abort"
    assert r["c1"] == r["c0"], f"app-green served {r['c1'] - r['c0']} requests after abort time + 2 s"


@pytest.mark.parametrize("name", ABORT)
def test_status_left_aborted_100_0_with_reason(scenario, name):
    s = scenario(name)["final_status"]
    assert s.get("state") == "aborted" and s.get("weights") == H.BLUE and s.get("abort_reason"), s


@pytest.mark.parametrize("name", ABORT)
def test_status_file_matched_schema_on_every_poll(scenario, name):
    assert not scenario(name)["status_bad"], scenario(name)["status_bad"][:2]


@pytest.mark.parametrize("name", ABORT)
def test_event_log_well_formed(scenario, name):
    assert not scenario(name)["event_bad"], scenario(name)["event_bad"][:3]


@pytest.mark.parametrize("name", ABORT)
def test_samples_every_2s(scenario, name):
    r = scenario(name)
    assert H.cadence_problem(H.rollout_events(r["events"], r["start"]["rid"])) is None


@pytest.mark.parametrize("name", ABORT)
def test_events_record_stages_and_abort(scenario, name):
    r = scenario(name)
    p = H.terminal_problem(H.rollout_events(r["events"], r["start"]["rid"]), "aborted")
    assert p is None, p


@pytest.mark.parametrize("name", ABORT)
def test_decisions_follow_rule_on_own_samples(scenario, name):
    r = scenario(name)
    probs = H.rule_problems(H.rollout_events(r["events"], r["start"]["rid"]), error_abort=(name != "proxykill"))
    assert not probs, probs[:3]
