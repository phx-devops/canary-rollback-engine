#!/bin/bash
# Terminal Bench entry point: run the verifier suite and write the reward (1 only if every test passes).
# Extra arguments go to pytest, e.g.  test.sh -k "healthy or crash"
cd "$(dirname "$0")"
mkdir -p /logs/verifier
printf '0\n' > /logs/verifier/reward.txt
printf '{"reward": 0}\n' > /logs/verifier/reward.json
python3 -m pytest -p no:cacheprovider -rfE -q --junitxml=/logs/verifier/junit.xml \
        --ignore=selftest --ignore=mutants "$@" | tee /logs/verifier/pytest.log
rc=${PIPESTATUS[0]}
reward=0
if [ "$rc" -eq 0 ]; then reward=1; fi
printf '%s\n' "$reward" > /logs/verifier/reward.txt
printf '{"reward": %s}\n' "$reward" > /logs/verifier/reward.json
echo "reward=$(cat /logs/verifier/reward.txt)"
exit 0
