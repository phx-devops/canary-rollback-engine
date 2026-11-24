Thanks for the thorough revision. Making nginx the required ingress and adding crash and reboot recovery turned this into a task that holds up:

- Every round-1 fix is carried.
- The package ran as submitted in our checks:
  - Reference: `1`
  - Empty solution: `0`
  - Environments: Modal and the review sandbox

## Difficulty trials

We ran:

- 3 GPT-6.1 Sol trials
- 3 Kimi 3.1 Pro trials
- Up to 8 hours per trial

Two trials failed, which meets the target of 2 failures. The remaining four passed every test in 14 to 28 minutes of agent time, so the margin is thin: with one fewer failure, the bar would not have been met.

- One Kimi run wrote `{"state":"shifting","pid":null}`, which the every-poll schema check caught in all 11 polled scenarios.
- One Sol run remained at `50/50` in the bursty scenario for over 80 seconds with a 15-second dwell, despite its status showing an evaluated, healthy window of 8 failures in 620 green requests, so it was never promoted.

## Test changes

### 1. Rollout replacement and reset behavior

Previously, `test_start_replaces_active_rollout_and_reset_stops_it` required the engine PID to change when replacing a rollout and required the engine process to be named `python3`.

- The instruction allows Python, Go, or Bash.
- It does not require a replaced rollout to use a new engine process.

Two local GPT-6.1 Sol runs failed only that assertion: 145 of 146 tests passed because each retained one engine process and started the new rollout within it.

The test now checks the requirements stated by the instruction:

- The second start returns a new `rollout_id` and a live engine PID.
- The replaced rollout writes no sample events from 2 seconds after the second start.
- The new rollout samples.
- From 2 seconds after reset, no rollout samples events and the state is `idle`.

The reference passes all 146 tests with this change, while a reference version that does not stop the old engine fails it. The change was in place before any gate trial reached its verifier.

### 2. Reboot behavior

The original `harness.reboot()` selected surviving entries under `/run` and `/tmp` from `/etc/canary-env/baseline.txt`. It re-read the file at reboot time, kept listed entries whole, ignored entries it failed to delete, and never touched `/dev/shm`.

Because the agent runs as root, it could preserve state across the simulated reboot despite the task's durable-state requirement. Possible routes included:

- Appending an agent-owned `/run` directory to the keep list.
- Putting files inside a kept directory such as `/run/netns`.
- Replacing a kept entry such as `/run/lock` with a file.
- Bind-mounting a state file onto itself so deletion fails.
- Tagging a kept directory with an extended attribute.
- Simply writing to `/dev/shm`, which a real reboot clears.

The reboot harness now:

- Ignores the keep list if it changed more than 60 seconds after container startup, checked with ctime, which cannot be set backward.
- Keeps only `/run/netns` and `/run/apps`.
- Removes entries created or changed after the trust point inside every kept directory.
- Strips user and trusted extended attributes from kept directories.
- Keeps non-directory entries only if they are unchanged since the trust point.
- Clears new entries in `/dev/shm`.
- Fails the test if anything that should be removed or stripped cannot be handled, such as a bind mount or read-only remount. The namespace mount is exempt.

`start-env.sh` writes the keep list and creates those directories once at boot, so honest solutions see no change.

We checked each route directly in the task image:

- The old harness preserved the state.
- The new harness removes the state or fails the reboot.
- The namespace and both targets continue to work.

Two limits remain:

- A simulated reboot inside the agent's own container cannot be airtight.
- A keep-list change within the first 60 seconds of the container remains trusted. Building the keep list into the tests instead of reading an agent-writable file would close this gap.
- `reboot()` kills only processes running as `canary`. A root process left by the agent retains its memory across the simulated reboot. Killing every process outside the container's init, apps, and verifier would close this gap.

### 3. Dwell and default behavior

Two instruction rules were not previously tested:

- `--dwell 9` must be rejected; only `5` was tested.
- Starting a rollout without `--dwell` must use the 30-second default.

The test suite now:

- Runs `9` in `test_start_rejects_dwell_below_10s`.
- Starts a rollout without `--dwell` under healthy traffic.
- Checks that stage 1 is held for at least 29 seconds, allowing the suite's standard 1-second polling delay.
- Checks that stage 2 arrives within 50 seconds.

A reference version that accepts `9` and uses a 15-second default fails both tests. The reference passes all 148 tests with these changes.

These tests were added after the gate trials. Adding tests can only change passes to failures, so the gate result stands.

## Package changes

- Removed `tests/README.md` and the package-root notes files (`README.md`, `CODER_NOTES.md`, and others) from the shipped task because the platform hygiene check does not allow them there. Nothing reads them.
- Updated `task.toml` under `[verifier]` to explain that `test.sh` uses the image's system-wide `python3-pytest` because tests must run in the agent's container.
- Added the `difficulty_explanation`, `solution_explanation`, and `verification_explanation` fields based on `instruction.md`, the solution, and the tests. Please check that they express the intended information.

## Suggestions

None of these suggestions changed the result.

- The form's **Final Prompt** field still contains the round-1 proposal text. The agent prompt was built from `instruction.md` in the package, so please keep the two in sync.
- A few boundary cases are not exercised:
  - The dwell check allows a stage to advance up to 1 second early (`DWELL - 1`).
  - No scenario sets a window to exactly 5%.
  - No scenario tests near the 20-request minimum.
  - `--dwell 10` is never accepted in a test.

  Edge cases at these points would catch off-by-one engines.

- Two stated rules are checked only indirectly:
  - Dwell is meant to count downtime, but the chaos scenario checks only a minimum hold. An engine that restarts its dwell clock after a crash could still pass. Checking each stage's end against its entry time would catch this.
  - The status window is meant to contain the five most recent samples, but `status_problem` checks only the shape. Comparing a poll with the event log would verify the contents.

- The recovery split test allows 7 percentage points (`test_25_recovery.py`, `abs(g - 0.5) <= 0.07`) even though the instruction requires the split to match within 5 percentage points.
- The aborted event's `error_rate` is checked only for presence. Comparing it with failures divided by requests in the window would catch an incorrect rate.
- `test_00_cli.py:16` calls `.get` on `H.last_json(p)`, and `test_10_promote.py:57-59` indexes parsed output from `canaryctl up` (`H.last_json`) without checking for `None`. A canaryctl implementation that prints non-JSON would raise `AttributeError` or `TypeError` instead of producing a readable assertion.

