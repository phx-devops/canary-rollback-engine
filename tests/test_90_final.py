"""End state: ingress left up at 100 % app-blue with no rollout in progress."""
import time
import harness as H


def test_ingress_left_serving_blue_and_idle():
    H.fresh(); time.sleep(1)
    with H.Load(lambda t: 30) as L: time.sleep(3)
    assert L.rec and all(r[2] == 200 and r[3] == "app-blue" for r in L.rec), f"green share {H.green_share(L.rec)}"
    assert H.status().get("state") == "idle"
