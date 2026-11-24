## Round 7 Feedback

Thank you for round 7.

### Current Fixes

The runtime, verifier, and attachment points from round 6 are fixed:

- The instruction states when `error_rate` is `null` in status windows and aborted events.
- The old engine process must exit before `start` and `reset` return.
- The default-dwell test reads from the grading offset.
- A green 5xx is counted even when the client gives up.
- `canaryctl up` always rewrites status from the saved state.
- The prompt attachment no longer contains the solution.

The build passes in the review sandbox:

- The solution scores **reward 1**, with all 155 tests passing.
- An empty solution scores **0**.
- The `nginx_max_fails` mutant scores **0**.
- The agent image contains neither solution nor test files.

## Difficulty Assessment

The task is currently too easy for the difficulty check. Five of six gate trials passed all 155 tests:

- GPT-6.1 Sol: **3/3 passes** (17, 24, and 33 minutes).
- Gemini 4 Carbon: **2/2 passes** (47 and 60 minutes).

The sixth trial was stopped after the result was settled. The difficulty check requires at least two of six trials to fail.

Two GPT-6.1 Sol runs from the review produced differing results:

- One passed in 21 minutes.
- The other scored **140/155**.
- 14 failures were caused by the engine aborting healthy rollouts with `ingress stopped running`, despite nginx remaining up.

The difficulty must come from the rollout and recovery logic itself, not merely from the wording.

## Recommended Test Tightening

Three areas where the instruction states a rule but the verifier currently checks it loosely:

### 1. Dwell Across Downtime

The instruction says that a stage's hold time runs from when the stage was entered, including time when the engine was not running.

The chaos test rejects advances that happen too early, but it does not require an eligible advance to occur. Therefore, it can miss an engine that resets the dwell clock on every `up`.

Required changes:

- Require the advance on the first eligible sample after the original dwell period has passed.
- Add a mutant that resets the stage clock and prove that the new check catches it.

### 2. Crashes Inside a Decision

Every decision must appear in the event log exactly once, including decisions made immediately before the engine is killed.

The current chaos kills occur after stage observations or fixed sleeps, so they rarely interrupt the interval between publishing a decision and saving it.

Required changes:

- Kill the engine at stage-change, abort, and promotion decision points.
- Verify that each decision survives exactly once.

### 3. Live Status Window

The status window must contain the five most recent samples. The current tests compare it with sample events only at the end.

An engine that clears its window at every stage change or resume could pass.

Required changes:

- Compare the live status window with the latest five sample events.
- Check immediately after stage changes and resumes.
- Add a mutant that clears or mishandles the window and prove that the check catches it.

## Additional Difficulty Opportunities

- Bursty traffic.
- A crash exactly at a stage boundary.
- Borderline windows:
  - Exactly 19 and 20 green requests.
  - Exactly 5% error rate.

The final files should then be used to rerun the difficulty check and report the result.

## Smaller Required Changes

- Replace the proposal text in the form's **Final Prompt** field with `instruction.md`.
- Keep `test_ingress_binary_is_the_packaged_nginx`.
- Update `tests/mutants/mutate.py`:
  - The `all_5xx` mutant still anchors on the old `count()` line in `engine.py`.
  - `count()` moved to `count_green` in `common.py`.
  - Point the mutant at `common.py`.