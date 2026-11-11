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


def test_ingress_serves_blue_when_idle():
    with H.Load(lambda t: 30) as L:
        __import__("time").sleep(3)
    assert L.rec and all(r[2] == 200 and r[3] == "app-blue" for r in L.rec), f"green share {H.green_share(L.rec)}"
