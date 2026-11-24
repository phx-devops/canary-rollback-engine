## Round 8 Feedback

Thank you for round 8.

### Changes Added

You added three tightenings from round 7:

- The chaos run requires an advance on the first eligible sample after the dwell period.
- A decision must be logged again if its event is missing after a kill.
- The live status window is compared with samples at seven points during the chaos run.

The following additions are also present:

- Mutants: `reset_dwell_on_resume`, `reset_window_on_stage`, and `no_reconcile_events`.
- Fixed anchor for the `all_5xx` mutant.

### Review Sandbox Results

The build is sound in the review sandbox:

- `solve.sh`: **reward 1** — all 156 tests pass.
- Empty solution: **reward 0**.
- `reset_dwell_on_resume` mutant: **reward 0**.
- Agent image: contains no solution or test files.
- Modal preflight: oracle **1**, nop **0**.

## Blocking Issue 1: Missing-Event Check

The missing-event check tests a fault that the instruction never states.

The scenarios in [scenarios.py](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/tests/scenarios.py) wait for a stage or terminal status, kill the engine, and call `omit_decision_event`. That helper overwrites a matching event line in `events.jsonl`.

This fault is outside the stated crash model:

- SIGKILL may occur at any moment.
- Reboots preserve `/var/log`.
- A line that the engine already appended is not lost.

Overwriting an existing event line represents log corruption, not a stated SIGKILL or reboot behavior.

### Validity of the Gate Failures

Both failing gate trials, Gemini 4 Carbon, received **151/156** and failed only five checks that derive from the missing-event corruption:

- `test_up_preserves_promoted_state_and_green_traffic`
- `test_events_record_stages_and_promotion[healthy]`
- `test_chaos_stages_in_order_and_held`
- `test_chaos_events`
- `test_abort_reboot_events`

The trajectories show that both engines use an outbox design:

1. The decision and pending event are stored durably.
2. The event is appended to the log.
3. The pending entry is cleared.
4. `canaryctl up` re-appends a pending entry if it is missing.

This design can log every decision exactly once under the stated model. It cannot repair an event line deleted after it was written.

GPT-6.1 Sol passed because all three engine implementations rebuild `events.jsonl` from a SQLite journal on every start. The missing-log-line implementation is not a real gap for their outbox behavior.

### Recommended Fix

Test an actual decision interruption instead:

- Kill the engine between saving state, changing nginx, and appending the event.
- Use a harness hook or several random-offset kills immediately after a stage change.
- Do not edit or corrupt the agent's event-log files.

If lost log lines should count as a supported fault, the instruction must explicitly state:

- That the log line may be lost.
- What recovery is required to restore it.

Also update `omit_decision_event`, or replace it, so that it returns `False` when no matching event line is found. Returning `True` currently allows the “did not create the recovery case” assertions to pass falsely.

## Blocking Issue 2: Difficulty

The task remains too easy for the difficulty check.

- GPT-6.1 Sol passed 3/3 runs.
- Scores: 156/156.
- Completion times: 17, 19, and 20 minutes.
- The new dwell and live-window checks passed.

The only valid failures come from issue 1, so this run does not meet the gate requirement of at least two valid failures across six trials.

Three fast GPT-6.1 Sol passes can also trigger Outlier's later `pass@k` result.

The new checks test rules that are plainly stated, and the solution follows them on the first attempt. The difficulty must require work beyond reading the instruction.

Recommended sources of difficulty:

- Real engine kills inside a decision.
- Subtle traffic shapes:
  - Bursty traffic.
  - Exactly 20 green requests.
  - Exactly 5% error rate.
- Nginx behavior that appears only under load.

Fix issue 1 first, then rerun the difficulty check on the final files and report the result.

## Required Reviewer Edit

In [harness.py](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/tests/harness.py), `dwell_progress_problem` must count a sample as eligible starting at:

```text
stage_ts + dwell + 1
```

Do not use `stage_ts + dwell - 1`.

The `dwell - 1` form demands an advance when the stage has been held only 14–15 seconds of a 15-second dwell, which the instruction forbids. The chaos scenarios do not currently place an eligible sample in that window, but an affected scenario could falsely fail a correct engine.

The `reset_dwell_on_resume` mutant continues to score 0 with the `+1` behavior.

## Smaller Required Changes

### Status PID Liveness

[instruction.md](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/instruction.md) states that whenever a `canaryctl` command returns with state `shifting`, `pid` is live.

However, `canaryctl status` prints the saved state without checking whether the engine is alive. After SIGKILL, it can report `shifting` with a dead PID.

Choose one fix:

- Check process liveness in `canaryctl status`, or
- Narrow the instruction's sentence.

Add a test covering the mismatch.

### Dwell Checks in Healthy Promotion Scenarios

`dwell_progress_problem` currently runs only in the chaos test. The healthy promotion scenarios check only the minimum dwell.

Consider applying the first-eligible-sample advancement check to those scenarios as well.

### Abort-Reboot Status Assertions

The abort-reboot scenario collects `status_bad`, but no test asserts that the list is empty.

Add the equivalent of `test_chaos_status_schema` for abort-reboot status.

### File-Capability Scope

`file_privilege_problems` currently runs `getcap` over five directories, while the instruction says to grant no file capabilities anywhere.

Choose one fix:

- Widen the scan, or
- Narrow the instruction's wording.

### Final Prompt

The form's **Final Prompt** field still contains proposal text.

Replace it with the contents of [instruction.md](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/instruction.md).