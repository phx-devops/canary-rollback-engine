The task is well designed and the verifier is careful. It uses deterministic faults, ground-truth counters, decisions recomputed from the engine's own samples, and mutants that all fail. However, once the package was made runnable, it was not difficult enough to meet the gate.

## Difficulty assessment

The gate requires at least 2 failed trials out of 6:

- 3 GPT-6.1 Sol trials
- 3 Kimi 3.1 Pro trials

On the fixed build:

- All 3 GPT-6.1 Sol trials passed `110/110`:
  - 11 minutes
  - 12 minutes
  - 53 minutes
- Kimi 3.1 Pro:
  - 1 trial failed 1 live-split check:
    - Green share: `0.591`
    - 50/50 stage in the `green3` scenario
    - Score: `109/110`
  - 2 trials passed `110/110`

Three earlier Kimi runs failed only because the verifier rejected `"pid": null` after the rollout had ended. We replaced them with fresh runs on the fixed verifier rather than counting them.

With one failure out of six, the gate was not met. The passing agents built a proxy, sampler, rollback rule, and privilege drop that passed every scenario, so the failure modes in the difficulty explanation were not exercised.

To pass the gate, the task needs a core step that models are likely to get wrong.

## Fixes carried into the next version

1. `docker-compose.yaml` named its service `client`, but Harbor runs the agent and verifier in `main`. The service capabilities therefore did not reach the task container. Renamed the service to `main`.

2. On Modal, the container failed with:

   ```text
   exec /usr/local/sbin/start-env: permission denied
   ```

   The ZIP marks the file executable, but that mode does not always survive packaging. Added an explicit `chmod 0755` in the Dockerfile.

3. The verifier finds the `:8080` listener by reading `/proc/<pid>/fd` from the canary-owned proxy. This requires `CAP_SYS_PTRACE`, which Docker's default capability set and Modal do not provide. Modal initially returned `EACCES`.

   - Added `SYS_PTRACE` to `cap_add`.
   - The golden solution then passed `110/110` on Modal.
   - The affected checks were:
     - `test_up_is_idempotent`
     - Five proxy-kill tests
     - Three isolation tests

4. `tests/harness.py` rejected `"pid": null` in every non-idle state. The prompt allows a null PID and requires it to be live only while shifting.

   - Allow null when the state is not `shifting`.
   - Added three self-tests.
   - All 45 self-tests pass.

   Outlier's QC pre-check also flagged this line.

5. `test_required_processes_live_in_namespace` required the `:8080` listener and engine to be separate processes. The prompt allows a design where one process performs both roles.

   The test now requires:

   - A listener process in the namespace.
   - An engine PID while shifting.
   - Both processes may be the same process.

6. `start-env.sh` used `ip netns exec`, which remounts `/sys` and fails under the review sandbox's rootless Docker.

   - Switched to `nsenter --net=/run/netns/canary`.
   - Added `ca-certificates` because the image lacked it and installs use `--no-install-recommends`.
   - The package can now install Codex in the trial container.

7. Updated `test.sh`:

   - Writes reward `0` first.
   - Also writes `reward.json`.
   - Sets agent timeout to `28800` seconds.
   - Sets verifier timeout to `1800` seconds.

8. Removed the statement "No package registry is reachable" from the prompt because trial containers have internet access and Codex installs itself from the network.

## Smaller points

- The `"canaryctl start"` replacement rule is not tested: every scenario starts after the previous rollout has ended.
- `"canaryctl reset"` during a rollout is not tested.
- `test_90_final` resets the deployment before checking it, so it tests reset behavior rather than the end state left by the agent.
- The notes describe the 5-second timeout as applying to response headers, but the prompt only says the server was "sent no response within 5 seconds". State explicitly that this means response headers to remove ambiguity.
- The idle-status check passes as soon as weights and `abort_reason` are correct. It does not reject an idle status containing:
  - A `rollout_id`
  - A stage number
  - A non-null PID
- After promotion, the reference's `canaryctl up` routes `100/0` to blue while `status.json` still reports the promoted `0/100`. No test covers `up` after a promotion.
- The verifier runs in the agent container with the image's system `python3-pytest`, which the agent can change.
  - The tests must see the live namespace and processes, so a separate verifier is not an option.
  - Install pytest at verification time in a private environment using `test.sh` (`uv` or a venv).
  - Alternatively, state in `task.toml` that the verifier relies on system packages.
  - Outlier's CI check flagged this issue.
- The final-notes failure test ran with the verifier and previous reference provided as input and did not execute the privileged checks. It is therefore not blind difficulty evidence.