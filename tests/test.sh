#!/bin/bash
# Terminal Bench entry point: run the verifier suite and write the reward (1 only if every test passes).
# Extra arguments go to pytest, e.g.  test.sh -k "healthy or crash"
cd "$(dirname "$0")"
mkdir -p /logs/verifier
python3 -m pytest -p no:cacheprovider -rfE -q --junitxml=/logs/verifier/junit.xml \
        --ignore=selftest --ignore=mutants "$@" . | tee /logs/verifier/pytest.log
rc=${PIPESTATUS[0]}
if [ "$rc" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
echo "reward=$(cat /logs/verifier/reward.txt)"
exit 0
