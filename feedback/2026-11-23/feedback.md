## Round 9 Feedback

Thank you for round 9.

### Implemented Improvements

You replaced the missing-log-line check with real decision-interruption behavior:

- `kill_at_decision` targets stage changes and promotions in nginx, the event log, or status.
- Added nginx worker-kill scenarios.
- Added status recovery for dead engines.
- Added concurrent `canaryctl up` handling.
- Added the new faults to [instruction.md](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/instruction.md).
- Retained the round-8 dwell-review edit.

These changes address the round-8 missing-event concern. The instruction now states the new faults, and a correct engine can recover from each.

## Review Sandbox Corrections

Please keep the following corrections made during review.

### 1. Decision Kills Triggered by nginx

The nginx trigger originally fired on any change to nginx's children:

- An old worker exiting after a reload could trigger the kill.
- The signal could arrive before the intended decision.

The trigger now fires only when new workers appear.

A reload that does not change the split can still trigger it. A fully safe trigger should verify that the new split is live before firing.

### 2. Event-Log Baseline Race

`kill_at_decision` read the baseline status before beginning event-log polling.

It could therefore miss a decision appended between those two operations. The implementation now follows the log first.

### 3. Worker Replacement Deadline

The worker-kill scenario previously judged replacement from one snapshot 1.5 seconds after the kill.

That deadline was not stated in the instruction. The scenario now waits up to 10 seconds for replacement.

### 4. Decision-Event Filtering

The decision-follows-sample check counted every event type.

The instruction allows and ignores additional event types, and one GPT-6.1 Sol engine logs a `stage_applied` event.

`rollout_events` now retains only the five documented event types.

### 5. Master-Kill Worker Precondition

The master-kill scenario previously found orphaned workers by scanning every `/proc/<pid>/fd` entry after the master was killed.

An engine that immediately notices the master's death and shuts down orphan workers within 0.3 seconds would correctly pass behaviorally but fail this test.

The scenario now:

1. Records the master's workers before the kill.
2. Checks that those workers remain present immediately afterward.

This narrows the race but does not completely close it. The worker-precondition should not itself be a graded assertion.

### Verified Results

With corrections 1–4:

- `solve.sh`: **reward 1**.
- Runtime: approximately 15 minutes.
- Empty solution: **reward 0**.
- `persist_last` mutant: caught.

Correction 5 was applied before the trials were graded.

## Difficulty Assessment

Difficulty remains the blocking issue.

On the fixed task:

- Three GPT-6.1 Sol review runs passed **203/203**:
  - 20 minutes.
  - 24 minutes.
  - 27 minutes.
- Two local GPT-6.1 Sol runs passed **203/203**:
  - Approximately 16 minutes each.
- Gemini 4 Carbon trials were stopped after GPT-6.1 Sol was consistently passing.

Outlier's later `pass@k` check identifies tasks that GPT-6.1 Sol passes as unsuitable difficulty targets.

Both saved engine implementations handle the new faults:

- One commits each decision and pending event to a SQLite journal before touching nginx, the event log, or status, then re-appends missing events during `up`.
- The other uses `events.jsonl` as an fsynced write-ahead journal and completes pending decisions during `up`.

Because the faults are stated explicitly, a strong model can implement recovery on its first attempt.

### Potentially Harder Alternatives

The review considered but did not test these changes:

- Log rotation during recovery.
- Engine and ingress failure together outside a reboot.
- Telemetry that reads response headers while response bodies continue streaming.

Each alternative would likely break both saved engines, but each would also be a buildable stated rule. The strongest solution would require a new nginx telemetry path.

Rounds 7, 8, and 9 repeatedly added stated faults, and GPT-6.1 Sol passed all of them. The design is therefore unlikely to reach the difficulty bar. A new task is likely needed.

## Remaining Smaller Points

### Prompt Attachment

The form's `canary-rollback-engine-prompt.zip` attachment is still from round 8:

- It lacks the round-9 instruction additions.
- It has the old 1800-second verifier timeout.

Rebuild it from the golden package.

### Final Prompt Field

Replace the proposal text in the form's **Final Prompt** field with [instruction.md](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/instruction.md).

### Decision-Kill Timing

`kill_at_decision` polls and only then sends SIGKILL.

Consequently, the engine can finish the decision before the signal arrives.

`test_25_recovery` currently checks only whether `kill_at_decision.fired` is true. An engine that reports the decision before durably persisting it could pass if the poll misses the persistence window.

Strengthen the test to cover the exact persistence-to-signal race.

### Forking Through Preexec

[harness.py](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/tests/harness.py) launches commands through `preexec_fn` while load and watcher threads run.

Python code after fork in a threaded parent can deadlock. An external launcher such as `nsenter` should be preferred.

### Mid-Body Reset Telemetry

`count_green` in [common.py](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/solution/canary/common.py) uses nginx's `$upstream_status` and may classify a request as successful if headers returned 200 before the connection reset mid-body.

The instruction defines a reset as a failure.

Choose one fix:

- Count mid-body resets as failures.
- Narrow the instruction's failure definition.

### Immediate Engine-Exit Checks

[instruction.md](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/instruction.md) requires the old engine to exit before `canaryctl start` or `reset` returns.

[test_00_cli.py](/Users/anand/workspace/terminal_bench/HFC/canary-rollback-engine/tests/test_00_cli.py) currently allows up to two seconds after the command returns.

A fire-and-forget kill could therefore pass.

Check the engine PID immediately when the command returns.