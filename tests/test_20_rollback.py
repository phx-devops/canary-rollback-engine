"""Rollouts that must abort: app-green at 8 % (50/50), crash (50/50), hang (0/100 bake), ingress killed
(50/50), and the engine killed as app-green starts failing (50/50)."""
import pytest
import harness as H
from scenarios import ABORT_DEADLINE

ABORT = ["green8", "crash", "hang", "proxykill", "killfault"]


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
    if name == "killfault": t0 = max(t0, r["up"]["t_done"] + 5)
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


def test_split_holds_while_green_fails(scenario):
    """Only the engine changes the split: while app-green fails 8 % at 50/50 and before the abort,
    app-green still answers about half of the requests."""
    r = scenario("green8")
    assert r["t_term"], "no abort"
    during = [x for x in r["rec"] if r["t_fault"] + 1 <= x[0] < r["t_term"] - 0.5]
    g = H.green_share(during)
    assert len(during) > 100 and g is not None and abs(g - 0.5) <= 0.05, f"green share {g} over {len(during)} requests"


def test_killfault_engine_restarted_by_up(scenario):
    u = scenario("killfault").get("up")
    assert u and u["rc"] == 0, u
    assert u["out"] and u["out"].get("state") in ("shifting", "aborted") and u["out"].get("rollout_id") == scenario("killfault")["start"]["rid"], u["out"]
    if u["out"].get("state") == "shifting": assert u["pid_alive"], "status pid after `canaryctl up` is not a live process"


def test_killfault_logged_resumed(scenario):
    r = scenario("killfault")
    mine = H.rollout_events(r["events"], r["start"]["rid"])
    assert [e for e in mine if e["type"] == "resumed"], "no resumed event after the engine was restarted"


@pytest.mark.parametrize("name", ["green8", "crash", "killfault"])
def test_no_advance_while_green_is_failing(scenario, name):
    """A failing canary must never be advanced. (nginx's passive health check hides green's failures, so
    an engine behind a default nginx config sees a 'healthy' window and promotes the broken canary.)"""
    r = scenario(name)
    at = {"green8": 2, "crash": 2, "killfault": 2}[name]
    assert r.get("t_fault"), "fault was not injected"
    later = [k[1] for k, t in r["first"].items() if isinstance(k, tuple) and k[1] and k[1] > at and t >= r["t_fault"]]
    assert not later, f"advanced to stage {max(later)} after app-green started failing at stage {at}"
    assert r["term"] == "aborted" and "promoted" not in r["first"], f"terminal state {r['term']}"
