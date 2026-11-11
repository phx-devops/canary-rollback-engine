"""Self-tests for the verifier's pure checks. No environment needed:
    CANARY_SELFTEST=1 python3 -m pytest tests/selftest
Each bad case is a mistake a plausible engine could make; the checker must flag it."""
import json, os, sys
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import harness as H

GOOD_SHIFTING = {"rollout_id": "r1", "state": "shifting", "stage": 2, "weights": {"app-blue": 50, "app-green": 50}, "pid": 42,
                 "window": {"seconds": 10, "green_requests": 150, "green_errors": 0, "error_rate": 0.0}, "abort_reason": None}
GOOD_IDLE = {"rollout_id": None, "state": "idle", "stage": None, "weights": H.BLUE, "pid": None, "window": None, "abort_reason": None}


def variant(base, **kw): d = json.loads(json.dumps(base)); d.update(kw); return d


# ---------------------------------------------------------------- status schema
@pytest.mark.parametrize("s", [
    GOOD_IDLE, GOOD_SHIFTING,
    variant(GOOD_SHIFTING, extra_key="allowed"),
    variant(GOOD_SHIFTING, state="promoted", stage=3, weights=H.GREEN),
    variant(GOOD_SHIFTING, state="aborted", weights=H.BLUE, abort_reason="green 8%"),
    variant(GOOD_SHIFTING, window={"seconds": 10, "green_requests": 0, "green_errors": 0, "error_rate": None}),
], ids=["idle", "shifting", "extra-key", "promoted", "aborted", "empty-window"])
def test_status_schema_accepts(s):
    assert H.status_problem(s) is None


@pytest.mark.parametrize("s", [
    {k: v for k, v in GOOD_IDLE.items() if k != "pid"},                       # key missing
    variant(GOOD_IDLE, weights=H.GREEN),                                      # idle not on blue
    variant(GOOD_SHIFTING, state="rolling"),                                  # unknown state
    variant(GOOD_SHIFTING, weights={"app-blue": 90, "app-green": 10}),        # weights disagree with stage
    variant(GOOD_SHIFTING, weights={"blue": 50, "green": 50}),                # wrong weight keys
    variant(GOOD_SHIFTING, stage=4),
    variant(GOOD_SHIFTING, stage=True),
    variant(GOOD_SHIFTING, pid="42"),
    variant(GOOD_SHIFTING, rollout_id=""),
    variant(GOOD_SHIFTING, window={"seconds": 10, "green_requests": 1.5, "green_errors": 0, "error_rate": 0}),
    variant(GOOD_SHIFTING, window=None),
    variant(GOOD_SHIFTING, abort_reason="should be null"),
    variant(GOOD_SHIFTING, state="aborted", weights=H.BLUE, abort_reason=""),       # abort without a reason
    variant(GOOD_SHIFTING, state="aborted", weights={"app-blue": 50, "app-green": 50}, abort_reason="x"),
    variant(GOOD_SHIFTING, state="promoted", stage=2, weights=H.GREEN),
    [],
], ids=["missing-key", "idle-green", "bad-state", "stage-mismatch", "weight-keys", "stage-4", "stage-bool", "pid-str",
        "empty-rid", "float-count", "no-window", "reason-while-shifting", "abort-no-reason", "abort-not-blue",
        "promoted-stage-2", "not-object"])
def test_status_schema_rejects(s):
    assert H.status_problem(s) is not None


# ---------------------------------------------------------------- event log helpers
def ev(ts, type, **kw): return json.dumps({"ts": ts, "type": type, **kw})
def sample(ts, n, f=0, rid="r1"): return ev(ts, "sample", rollout_id=rid, green_requests=n, green_errors=f)
def stage(ts, k, rid="r1"): return ev(ts, "stage", rollout_id=rid, stage=k, weights=H.STAGE_W[k])


def promoted_log(per=10, errs=0):
    """A correct promoted rollout: 8 samples per stage, stage changes after evaluated healthy windows."""
    lines, t = [stage(0, 1)], 0.0
    for k in (1, 2, 3):
        for _ in range(8):
            t += 2; lines.append(sample(t, per, errs))
        lines.append(stage(t, k + 1) if k < 3 else ev(t, "promoted", rollout_id="r1"))
    return lines


def mine(lines): evs, bad = H.parse_events(lines); assert not bad, bad; return H.rollout_events(evs, "r1")


def test_parse_accepts_unknown_types_and_blank_lines():
    evs, bad = H.parse_events([ev(1, "reset"), "", ev(2, "debug", note="x")])
    assert not bad and len(evs) == 2


@pytest.mark.parametrize("line", [
    "not json", json.dumps({"type": "sample"}), json.dumps({"ts": "1", "type": "sample"}),
    ev(1, "sample", green_requests=3, green_errors=0),                                 # no rollout_id
    ev(1, "sample", rollout_id="r1", green_requests=3, green_errors=4),                # errors > requests
    ev(1, "stage", rollout_id="r1", stage=2, weights=[50, 50]),                        # weights as a list
    ev(1, "stage", rollout_id="r1", stage=2, weights=H.STAGE_W[1]),                    # wrong weights for stage
    ev(1, "aborted", rollout_id="r1", reason="", green_requests=1, green_errors=1, error_rate=1),
    ev(1, "aborted", rollout_id="r1", reason="x", green_requests=1, green_errors=1),    # no error_rate
], ids=["not-json", "no-ts", "string-ts", "no-rid", "errors-gt-requests", "weights-list", "weights-wrong",
        "empty-reason", "no-error-rate"])
def test_parse_rejects(line):
    assert H.parse_events([line])[1]


def test_parse_rejects_time_going_backwards():
    assert H.parse_events([sample(10, 1), sample(5, 1)])[1]


# ---------------------------------------------------------------- rule recomputation
def test_correct_promoted_rollout_has_no_problems():
    m = mine(promoted_log())
    assert H.rule_problems(m) == [] and H.terminal_problem(m, "promoted") is None and H.cadence_problem(m) is None


def test_flags_stage_change_before_window_is_full():
    lines = [stage(0, 1), sample(2, 50), sample(4, 50), stage(4, 2)]
    assert H.rule_problems(mine(lines))


def test_flags_stage_change_with_too_few_requests():        # the "0/0 counts as healthy" mistake
    lines = [stage(0, 1)] + [sample(2 * i, 3) for i in range(1, 6)] + [stage(10, 2)]
    assert any("decided on window" in p for p in H.rule_problems(mine(lines)))


def test_flags_ignored_breach():
    lines = [stage(0, 1)] + [sample(2 * i, 20, 2) for i in range(1, 7)]      # 10 % for 12 s, no abort
    assert any("not acted on" in p for p in H.rule_problems(mine(lines)))


def test_accepts_correct_abort_and_flags_premature_or_mislogged_abort():
    base = [stage(0, 1)] + [sample(2 * i, 20, 0) for i in range(1, 5)] + [sample(10, 20, 6)]   # window 6/100 = 6 %
    ok = base + [ev(10, "aborted", rollout_id="r1", reason="x", green_requests=100, green_errors=6, error_rate=0.06)]
    assert H.rule_problems(mine(ok)) == [] and H.terminal_problem(mine(ok), "aborted") is None
    mislogged = base + [ev(10, "aborted", rollout_id="r1", reason="x", green_requests=20, green_errors=6, error_rate=0.3)]
    assert any("logged" in p for p in H.rule_problems(mine(mislogged)))
    premature = [stage(0, 1)] + [sample(2 * i, 20, 1) for i in range(1, 6)] + [      # 5 / 100 = 5 %: not above
        ev(10, "aborted", rollout_id="r1", reason="x", green_requests=100, green_errors=5, error_rate=0.05)]
    assert H.rule_problems(mine(premature))


def test_mean_of_ratios_engine_is_caught():
    """Bursty traffic: per-sample rates average above 5 % but the true window ratio is low.
    An engine that aborted here must be flagged."""
    lines = [stage(0, 1), sample(2, 2, 1), sample(4, 2, 1), sample(6, 60), sample(8, 60), sample(10, 60),
             ev(10, "aborted", rollout_id="r1", reason="x", green_requests=184, green_errors=2, error_rate=0.4)]
    assert H.rule_problems(mine(lines))


def test_proxy_death_abort_is_exempt_from_window_rule_only_when_asked():
    lines = [stage(0, 1), sample(2, 10), ev(3, "aborted", rollout_id="r1", reason="proxy died",
                                            green_requests=10, green_errors=0, error_rate=0.0)]
    assert H.rule_problems(mine(lines), error_abort=False) == []
    assert H.rule_problems(mine(lines), error_abort=True)


def test_terminal_and_cadence_checks():
    m = mine(promoted_log())
    assert H.terminal_problem(m, "aborted")
    skipping = mine([stage(0, 1), sample(2, 30), sample(4, 30), stage(4, 3), sample(6, 30), sample(8, 30)])
    assert H.terminal_problem(skipping, "promoted")
    slow = mine([stage(0, 1)] + [sample(5 * i, 30) for i in range(1, 8)])
    assert H.cadence_problem(slow)


def test_other_rollouts_are_ignored():
    lines = promoted_log() + [sample(100, 50, 50, rid="other")]
    assert H.rule_problems(mine(lines)) == []
