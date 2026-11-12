"""Verify an agent-produced terminal state before resetting the environment for cleanup."""
import time
import harness as H


def test_promoted_state_survives_up_before_cleanup(scenario):
    r = scenario("healthy")
    try:
        assert r["term"] == "promoted", r["final_status"]
        assert r["final_status"].get("state") == "promoted" and r["final_status"].get("weights") == H.GREEN, r["final_status"]
        assert r["up_after_promotion"]["rc"] == 0, r["up_after_promotion"]
        assert r["up_after_promotion"]["out"].get("state") == "promoted", r["up_after_promotion"]["out"]
        after_up = [item for item in r["rec"] if r["up_after_promotion"]["t_done"] <= item[0] < r["up_after_promotion"]["t_done"] + 2]
        assert after_up and all(item[2] == 200 and item[3] == "app-green" for item in after_up)
    finally:
        H.ctl("reset")
