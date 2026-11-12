#!/bin/bash
# Prove the suite has teeth: each mutant must make its targeted tests fail (reward 0).
# Usage: run_mutants.sh [/path/to/solution]     (run as root in the task container)
SRC="${1:-$(cd "$(dirname "$0")/../../solution" && pwd)}"
HERE="$(cd "$(dirname "$0")" && pwd)"; SUITE="$HERE/.."
MUTANTS=(mean_of_ratios all_5xx empty_is_healthy drain_not_zero no_watchdog
         nginx_max_fails nginx_next_upstream nginx_read_timeout
         up_always_blue no_resume abort_not_durable state_in_run no_resumed_event
         root_engine idle_schema)
declare -A TARGET=(
  [mean_of_ratios]="bursty"  [all_5xx]="blue30"  [empty_is_healthy]="notraffic"  [drain_not_zero]="green8"
  [no_watchdog]="proxykill"  [nginx_max_fails]="crash"  [nginx_next_upstream]="crash"  [nginx_read_timeout]="hang"
  [up_always_blue]="chaos"  [no_resume]="chaos"  [abort_not_durable]="abort_survives_reboot or abort_reboot"  [state_in_run]="chaos"
  [no_resumed_event]="chaos"  [root_engine]="mid-rollout"  [idle_schema]="test_00_cli"
)
reinstall() {
  # stop whatever the previous install left running, whoever it runs as, and forget its state
  pkill -f "/opt/canary/bin/engine.py" 2>/dev/null; pkill -x nginx 2>/dev/null; sleep 1
  rm -rf /run/canary /var/lib/canary
  python3 "$HERE/mutate.py" "$SRC" "$1" >/dev/null && /opt/canary/bin/canaryctl up >/dev/null 2>&1; true
}
caught=0; total=0
for m in "${MUTANTS[@]}"; do
  total=$((total+1)); reinstall "$m"
  out=$(bash "$SUITE/test.sh" -k "${TARGET[$m]}" 2>&1)
  failed=$(grep -E "^FAILED" <<<"$out" | sed -E 's/^FAILED [^:]*::([^ ]*).*/\1/' | head -4 | paste -sd, -)
  nfail=$(grep -cE "^FAILED" <<<"$out")
  nerr=$(grep -cE "^ERROR" <<<"$out")
  # caught only if some targeted test actually failed (reward 0 alone could mean nothing was selected)
  if [ "$((nfail+nerr))" -gt 0 ] && grep -q "reward=0" <<<"$out"; then caught=$((caught+1)); verdict=CAUGHT; else verdict=MISSED; fi
  printf '%-20s -k %-40s %-7s %2s failed: %s\n' "$m" "${TARGET[$m]}" "$verdict" "$nfail" "$failed"
done
reinstall golden
echo "mutants caught: $caught/$total"
[ "$caught" -eq "$total" ]
