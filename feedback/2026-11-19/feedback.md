# Round 6 Feedback

Thank you for round 6. The verifier is present in the upload, and the build passed in the review sandbox:

- `solve.sh` scored **reward 1** (all tests, approximately 13 minutes).
- An empty solution scored **0**.
- The `nginx_max_fails` mutant scored **0**.
- The agent image contains no solution or test files.
- The `/usr/sbin/nginx` packaged-binary check (`dpkg md5sum`) passes.

## Blocking Issues

The failed GPT-6.1 Sol runs passed every other check but failed only on checks left open by the instruction. The failures do not demonstrate sufficient task difficulty.

### 1. Status-window `error_rate`

In both GPT-6.1 Sol runs:

- `"error_rate"` was `null` until the window contained five samples and 20 requests.
- The runs failed 11 status-schema checks.
- The instruction table describes `error_rate` as a number or `null`.
- [tests/harness.py](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/tests/harness.py) requires `green_errors / green_requests` whenever `green_requests > 0`.

Add this rule explicitly to the status table before running the gate.

### 2. Aborted-event `error_rate` with an empty window

One Sol run (`4oQcwLm`) scored **153/154** in approximately 28 minutes:

- An `ingress_unavailable` abort logged `"error_rate": null` with zero requests.
- [tests/harness.py](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/tests/harness.py) requires a number.
- The status window uses `null` for the same case, while [engine.py](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/solution/canary/engine.py) logs `0.0` for abort events.

The gate was changed to accept `null` when `green_requests` is zero.

### 3. Engine process exit

Sol runs `247uiqZ` and `kqbDUjZ` scored **153/154** in 29 and 31 minutes:

- Both failed only the final assertions in `test_start_replaces_active_rollout_and_reset_stops_it`.
- The replaced engine had stopped sampling, but its process remained alive.
- `reset` did not stop the first rollout engine process before the test returned.

The instruction does not currently require:

- A replaced engine process to exit.
- A reset engine process to exit within a specific deadline.

Two Gemini runs continued for 99 minutes or longer.

## Required Changes

### Instruction rules

Add these rules to [instruction.md](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/instruction.md):

- Define `error_rate` as `null` when the status window has no requests.
- Define `error_rate` for aborted events with an empty window.
- If process termination is required, state that `start` and `reset` must end the old engine process within the test deadline of two seconds after `reset` returns.

After clarifying the rules:

1. Rerun the difficulty check on the final files.
2. If valid Sol runs pass, add rollback difficulty through scenarios such as bursty traffic, stage-boundary crashes, or borderline resumed windows.

### Event-log offset

`test_start_without_dwell_uses_30_second_default` currently calls `H.read_events(0)`, so it reads lines from before grading.

- Use `EVENTS_OFFSET` from [conftest.py](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/tests/conftest.py).
- This is the same issue previously raised for the replacement test.

### 499 response handling

[engine.py](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/solution/canary/engine.py) skips every line with client status `499` before checking the upstream status.

This means a green `5xx` response answered after the client gave up is not counted as a failure. Either:

- State this exception in the instruction, or
- Count those upstream `5xx` lines.

### Prompt Files and solution attachment

- `canary-rollback-engine-solution.zip` still contains [solution/](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/solution).
- Agent-facing attachments must not include the reference solution (`engine.py`, `common.py`, `canaryctl`, `solve.sh`, and related files).
- If the task requires no input files, attach no solution archive.

### Final Prompt

Paste [instruction.md](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/instruction.md) into the form's **Final Prompt** field rather than retaining the proposal text.

### Recovery status

[canaryctl](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/solution/canary/canaryctl) currently rewrites `status.json` only when `rollout_id` differs.

If the engine is killed after saving `promoted` or `aborted` to `state.json` but before publishing `status.json`, status can remain `shifting`.

Rewrite the status from `state.json` whenever the rollout is no longer in the `shifting` state.

## Required Regression Check

Keep the packaged-nginx check in [test_30_isolation.py](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/tests/test_30_isolation.py):

```python
test_ingress_binary_is_the_packaged_nginx
```
