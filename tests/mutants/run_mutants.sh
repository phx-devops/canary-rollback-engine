#!/bin/bash
# Prove the suite has teeth: each mutant must make its targeted tests fail (reward 0).
# Usage: run_mutants.sh [/path/to/solution]     (run as root in the task container; needs go)
SRC="${1:-$(cd "$(dirname "$0")/../../solution" && pwd)}"
HERE="$(cd "$(dirname "$0")" && pwd)"; SUITE="$HERE/.."
declare -A TARGET=(
  [mean_of_ratios]="bursty"        [all_5xx]="blue30"          [empty_is_healthy]="notraffic"
  [drain_not_zero]="green8"        [no_watchdog]="proxykill"   [default_timeout]="hang"
  [redispatch]="crash"             [root_engine]="mid-rollout" [idle_schema]="test_00_cli"
)
reinstall() {
  # stop whatever the previous install left running, whoever it runs as
  pkill -f "/opt/canary/bin/engine.py" 2>/dev/null; pkill -f "/opt/canary/bin/canary-proxy" 2>/dev/null; sleep 1
  rm -f /run/canary/proxy.pid /run/canary/engine.pid /run/canary/proxy.sock
  python3 "$HERE/mutate.py" "$SRC" "$1" >/dev/null && /opt/canary/bin/canaryctl up >/dev/null 2>&1; true
}
caught=0; total=0
for m in mean_of_ratios all_5xx empty_is_healthy drain_not_zero no_watchdog default_timeout redispatch root_engine idle_schema; do
  total=$((total+1)); reinstall "$m"
  out=$(bash "$SUITE/test.sh" -k "${TARGET[$m]}" 2>&1)
  failed=$(grep -E "^FAILED" <<<"$out" | sed -E 's/^FAILED [^:]*::([^ ]*).*/\1/' | head -4 | paste -sd, -)
  nfail=$(grep -cE "^FAILED" <<<"$out")
  if grep -q "reward=0" <<<"$out"; then caught=$((caught+1)); verdict=CAUGHT; else verdict=MISSED; fi
  printf '%-17s -k %-12s %-7s %2s failed: %s\n' "$m" "${TARGET[$m]}" "$verdict" "$nfail" "$failed"
done
reinstall golden
echo "mutants caught: $caught/$total"
[ "$caught" -eq "$total" ]
