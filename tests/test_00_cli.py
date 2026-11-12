"""canaryctl contract (instruction.md: Command-line interface). Runs before any rollout."""
import harness as H


def test_up_succeeds_and_prints_status():
    p, _ = H.ctl("up")
    assert p.returncode == 0, p.stderr[-300:]
    assert isinstance(H.last_json(p), dict), f"last stdout line is not a JSON object: {p.stdout[-200:]!r}"


def test_up_is_idempotent():
    p, _ = H.ctl("up")
    assert p.returncode == 0, p.stderr[-300:]
    assert len(H.listener_pids()) >= 1, "nothing listening on 127.0.0.1:8080 after up"
    assert H.last_json(p).get("state") in ("idle", "promoted", "aborted")


def test_reset_reports_schema_valid_idle_status():
    p, _ = H.ctl("reset")
    out = H.last_json(p)
    assert p.returncode == 0, p.stderr[-300:]
    assert out and out.get("state") == "idle", out
    assert H.status_problem(out) is None, H.status_problem(out)
    assert H.status_problem(H.status()) is None, f"status.json after reset: {H.status_problem(H.status())}"


def test_status_subcommand_matches_status_file():
    p, _ = H.ctl("status")
    assert p.returncode == 0 and H.last_json(p) == H.status()


def test_start_rejects_dwell_below_10s():
    p, _ = H.ctl("start", "--dwell", "5")
    assert p.returncode != 0
    assert H.status().get("state") != "shifting", "a rejected start must not begin a rollout"


def test_start_replaces_active_rollout_and_reset_stops_it():
    first_pid = second_pid = None
    try:
        first, _ = H.ctl("start", "--dwell", "60")
        first_status = H.last_json(first)
        assert first.returncode == 0 and H.status_problem(first_status) is None, first.stderr[-300:]
        first_pid = first_status["pid"]

        second, _ = H.ctl("start", "--dwell", "60")
        second_status = H.last_json(second)
        assert second.returncode == 0 and H.status_problem(second_status) is None, second.stderr[-300:]
        second_pid = second_status["pid"]
        assert second_status["rollout_id"] != first_status["rollout_id"]
        assert second_pid != first_pid
        assert H.process_alive(second_pid, "python3")
        assert not H.process_alive(first_pid, "python3"), "replaced rollout engine is still running"
    finally:
        reset, _ = H.ctl("reset")

    reset_status = H.last_json(reset)
    assert reset.returncode == 0 and H.status_problem(reset_status) is None, reset.stderr[-300:]
    assert reset_status["state"] == "idle"
    assert not H.process_alive(first_pid, "python3"), "reset left the first rollout engine running"
    if second_pid is not None:
        assert not H.process_alive(second_pid, "python3"), "reset left the second rollout engine running"


def test_ingress_serves_blue_when_idle():
    with H.Load(lambda t: 30) as L:
        __import__("time").sleep(3)
    assert L.rec and all(r[2] == 200 and r[3] == "app-blue" for r in L.rec), f"green share {H.green_share(L.rec)}"
