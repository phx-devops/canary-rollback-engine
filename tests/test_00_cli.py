"""canaryctl contract (instruction.md: Command-line interface). Runs before any rollout."""
import harness as H
import time


def test_up_succeeds_and_prints_status():
    p, _ = H.ctl("up")
    assert p.returncode == 0, p.stderr[-300:]
    assert isinstance(H.last_json(p), dict), f"last stdout line is not a JSON object: {p.stdout[-200:]!r}"


def test_up_is_idempotent():
    p, _ = H.ctl("up")
    assert p.returncode == 0, p.stderr[-300:]
    assert len(H.listener_pids()) >= 1, "nothing listening on 127.0.0.1:8080 after up"
    out = H.last_json(p)
    assert isinstance(out, dict), f"last stdout line is not a JSON object: {p.stdout[-200:]!r}"
    assert out.get("state") in ("idle", "promoted", "aborted")


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
    p, _ = H.ctl("start", "--dwell", "9")
    assert p.returncode != 0
    assert H.status().get("state") != "shifting", "a rejected start must not begin a rollout"


def test_start_accepts_dwell_10():
    try:
        p, _ = H.ctl("start", "--dwell", "10")
        out = H.last_json(p)
        assert p.returncode == 0, p.stderr[-300:]
        assert isinstance(out, dict) and out.get("state") == "shifting", out
        time.sleep(2)
    finally:
        H.ctl("reset")


def test_start_without_dwell_uses_30_second_default():
    try:
        p, elapsed = H.ctl("start")
        out = H.last_json(p)
        assert p.returncode == 0 and elapsed < 5, p.stderr[-300:]
        assert isinstance(out, dict) and out.get("state") == "shifting", out
        rid = out["rollout_id"]
        first = second = None
        end = time.monotonic() + 50
        while time.monotonic() < end:
            events, bad = H.read_events(0)
            assert not bad, bad[:3]
            mine = H.rollout_events(events, rid)
            for event in mine:
                if event["type"] == "stage" and event["stage"] == 1 and first is None:
                    first = event["ts"]
                if event["type"] == "stage" and event["stage"] == 2 and second is None:
                    second = event["ts"]
            if first is not None and second is not None:
                break
            time.sleep(0.2)
        assert first is not None, "stage 1 was never logged"
        assert second is not None and second - first >= 29, "default dwell was shorter than 30 seconds"
        assert second - first <= 50, "stage 2 did not arrive within 50 seconds"
    finally:
        H.ctl("reset")


def test_start_replaces_active_rollout_and_reset_stops_it():
    first_pid = second_pid = reset_offset = None
    try:
        try:
            start_offset = H.os.path.getsize(H.EVENTS)
        except OSError:
            start_offset = 0
        first, _ = H.ctl("start", "--dwell", "60")
        first_status = H.last_json(first)
        assert first.returncode == 0 and H.status_problem(first_status) is None, first.stderr[-300:]
        first_pid = first_status["pid"]
        first_rid = first_status["rollout_id"]
        time.sleep(2)

        second, _ = H.ctl("start", "--dwell", "60")
        second_status = H.last_json(second)
        assert second.returncode == 0 and H.status_problem(second_status) is None, second.stderr[-300:]
        second_pid = second_status["pid"]
        assert second_status["rollout_id"] != first_status["rollout_id"]
        assert H.process_alive(second_pid), "second start did not return a live engine pid"
        time.sleep(2)

        events, bad = H.read_events(start_offset)
        assert not bad, bad[:3]
        old_samples = [e for e in events if e.get("rollout_id") == first_rid and e["type"] == "sample"]
        assert not old_samples, "replaced rollout continued sampling"
        new_samples = [e for e in events if e.get("rollout_id") == second_status["rollout_id"] and e["type"] == "sample"]
        assert new_samples, "replacement rollout did not sample"
    finally:
        try:
            reset_offset = H.os.path.getsize(H.EVENTS)
        except OSError:
            reset_offset = 0
        reset, _ = H.ctl("reset")

    reset_status = H.last_json(reset)
    assert reset.returncode == 0 and H.status_problem(reset_status) is None, reset.stderr[-300:]
    assert reset_status["state"] == "idle"
    time.sleep(2)
    events, bad = H.read_events(reset_offset)
    assert not bad, bad[:3]
    assert not [e for e in events if e["type"] == "sample"], "reset left a rollout sampling"
    assert not H.process_alive(first_pid), "reset left the first rollout engine running"
    assert not H.process_alive(second_pid), "reset left the second rollout engine running"


def test_ingress_serves_blue_when_idle():
    with H.Load(lambda t: 30) as L:
        __import__("time").sleep(3)
    assert L.rec and all(r[2] == 200 and r[3] == "app-blue" for r in L.rec), f"green share {H.green_share(L.rec)}"
