# Automated canary deployment and rollback engine

## The environment

The network namespace `canary` already exists at `/run/netns/canary`. Its loopback interface is
up, and it stays in place for the whole task and its tests. Two versions of a web service are
already running inside it as the user `apps`:

| target | version | address inside the namespace |
|---|---|---|
| `app-blue` | 1.0 (current) | `127.0.0.1:9001` |
| `app-green` | 2.0 (candidate) | `127.0.0.1:9002` |

Every response carries an `X-App` header naming the target that served it. Do not modify,
restart or reconfigure the two targets. nginx, Python 3, Go and Bash are installed, along with
the system packages needed by the task. An unprivileged user `canary` exists. You work as root.

Build a toolchain, in Python, Go or Bash, that shifts live traffic from `app-blue` to `app-green`
in stages, rolls back automatically when `app-green` misbehaves, and survives its own crashes and
node reboots. The rest of this file states what must be true and the interfaces the tests use.
It does not say which nginx settings, telemetry mechanism, state format or privilege-dropping
method to use. Any approach that produces the stated end state is accepted.

## Ingress

The ingress is the platform's standard proxy: the installed **nginx** (`/usr/sbin/nginx`), run
and configured by your toolchain. It listens on `127.0.0.1:8080` inside the `canary` namespace
and connects to the two targets directly. No other program may accept client traffic, or sit
between nginx and the targets: every process listening on `127.0.0.1:8080`, and every process
holding a connection to `127.0.0.1:9001` or `127.0.0.1:9002`, must be nginx.

- Each client request goes to exactly one target, and the client receives that target's response,
  or an error if the target fails.
- Only the engine changes the split. The share of requests each target receives must match the
  current stage at all times. That includes while `app-green` is failing some or all of its
  requests.
- Changing the split must not fail or drop client requests.

If the ingress stops running during a rollout while the engine is running, the engine must have
it serving 100 % `app-blue` again within 10 seconds and abort the rollout.

## Rollout

A rollout starts with 100 % of traffic on `app-blue`, then moves through three stages,
written as blue/green: **90/10 → 50/50 → 0/100**. Each stage is held for at least the dwell time
before it advances. The time a stage has been held runs from when the stage was entered,
including any time the engine was not running. The rollout is **promoted** once 0/100 has also
been held for the dwell time. The split being served must match the current stage to within 5
percentage points.

## Telemetry and the rollback rule

Every 2 seconds the engine takes one **sample** from the ingress's access logs. A sample counts
the requests routed to `app-green` during those 2 seconds, and how many of them failed. A request
routed to `app-green` has failed if `app-green` answered with a 5xx status, refused or reset the
connection, or did not send response headers within 5 seconds. Here, a response means the
headers; the body need not have completed in that interval. Failures of `app-blue` are not
failures of `app-green`.

The **window** is the five most recent samples (10 seconds). Its **error rate** is the window's
total failed `app-green` requests divided by its total `app-green` requests. The rate is
**evaluated** only when the window holds five samples and at least **20** `app-green` requests.
After each sample:

- If the rate is evaluated and **above 5 %**, the engine aborts at once and returns 100 % of
  traffic to `app-blue`.
- Otherwise, if the rate is evaluated (and so at most 5 %) and the current stage has been held
  for the dwell time, the rollout advances to the next stage, or is promoted after 0/100.
- Otherwise the rollout stays where it is. With too little traffic to evaluate, it waits.

An aborted rollout stays aborted, even if `app-green` recovers, until a new rollout is started.

## Crashes and reboots

The engine can be killed with SIGKILL at any moment. Separately, the node can **reboot**: every
process running as `canary` is killed, and everything under `/run` and `/tmp` that was not there
when the container started is lost. `/var/lib`, `/var/log` and `/opt` survive a reboot. After
either event, the platform's node agent runs `canaryctl up` (it may take a few seconds to do so). While the engine is down, the ingress keeps
serving whatever split it was serving, unless the ingress itself was lost too.

Within 5 seconds of `canaryctl up` returning:

- A rollout that was in progress continues under the same `rollout_id`. The ingress serves the
  current stage's split: never 100 % `app-blue` for a rollout that is still shifting.
- A rollout that had already been aborted stays aborted, with 100 % `app-blue`. A promoted one
  stays promoted, with 100 % `app-green`.

A continuing rollout logs a `resumed` event and starts with an empty window. It makes no decision
until it has five new samples.

**Decisions are durable.** Once a stage change, an abort or a promotion has reached the ingress
or been reported, no crash or reboot may undo it, and none may make it happen a second time.
Every decision appears in the event log exactly once, including one the engine made just before
it was killed.

## Running unprivileged and detached

Every process your toolchain leaves running inside the `canary` namespace must meet all of the
following. That includes the engine, every nginx process and any helpers, but not the two
targets. These are exactly the properties the tests read from `/proc/<pid>/`:

- `status`: all four `Uid` values are `canary`'s UID and all four `Gid` values are `canary`'s
  GID. `Groups` is empty or contains only `canary`'s GID. `CapInh`, `CapPrm`, `CapEff` and
  `CapAmb` are all 0. The bounding set and `NoNewPrivs` are not checked.
- `ns/net`: the same namespace as `/run/netns/canary`.
- `stat`: no controlling terminal (`tty_nr` = 0), and a session ID different from that of the
  test process that ran `canaryctl`. The process keeps running after `canaryctl` exits.

Grant no file capabilities anywhere, and put no setuid or setgid files under `/opt/canary`.

## Command-line interface — `/opt/canary/bin/canaryctl`

The tests run `canaryctl` as **root from the container's default network namespace**. Each
subcommand exits 0 on success and prints the current status (below) as one JSON object on the
last line of stdout.

- `canaryctl up`: make sure the ingress and, if a rollout is in progress, the engine are running,
  recovering from a crash or reboot as described above. If no rollout is in progress, serve the
  split of the last rollout's final state (100 % `app-blue` when there has been none). Running it
  again must be harmless.
- `canaryctl start [--dwell SECONDS]`: start a new rollout with a new `rollout_id` (default dwell
  30 s). Values below 10 must be rejected with a non-zero exit. Any rollout in progress is
  replaced. Return within 5 seconds, printing the new rollout's status (`state` = `shifting`),
  while the rollout continues in the background.
- `canaryctl status`: print the current status.
- `canaryctl reset`: stop any rollout in progress, serve 100 % `app-blue`, and set the status to
  `idle`.

## Status file — `/run/canary/status.json`

This file holds the current status. It is replaced atomically and never partially written. It
may be missing only between a reboot and the next `canaryctl up`. Every key below is always
present; extra keys are allowed.

| key | type | value |
|---|---|---|
| `rollout_id` | string or null | the rollout's ID; null when `idle` |
| `state` | string | `idle`, `shifting`, `promoted` or `aborted` |
| `stage` | 1, 2, 3 or null | the current stage, or the last one reached; null when `idle` |
| `weights` | object | `{"app-blue": <int>, "app-green": <int>}`, the split being served |
| `pid` | integer or null | the engine's process ID while `shifting`; null or the last engine's ID otherwise |
| `window` | object or null | `{"seconds": 10, "green_requests": <int>, "green_errors": <int>, "error_rate": <number or null>}`; null when `idle` |
| `abort_reason` | string or null | non-empty when `aborted`; null otherwise |

`weights` is `{"app-blue": 100, "app-green": 0}` when `idle` or `aborted`, matches the current
stage while `shifting`, and is `{"app-blue": 0, "app-green": 100}` when `promoted`. Whenever a
`canaryctl` command returns with `state` = `shifting`, `pid` is a live process.

## Event log — `/var/log/canary/events.jsonl`

Append one JSON object per line. Every line has `ts` (Unix time in seconds, a number) and `type`
(a string). Other types are allowed and ignored. These types are required, with these fields:

| `type` | when | required fields besides `ts` and `type` |
|---|---|---|
| `sample` | every 2 s while an engine is running a rollout | `rollout_id`, `green_requests`, `green_errors` (that sample's counts only) |
| `stage` | entering a stage, including stage 1 | `rollout_id`, `stage` (1–3), `weights` (the stage's split, as in the status file) |
| `aborted` | on abort | `rollout_id`, `reason`, `green_requests`, `green_errors` (the window totals behind the decision), `error_rate` |
| `promoted` | on promotion | `rollout_id` |
| `resumed` | when a rollout continues after a crash or reboot | `rollout_id`, `stage` |

A decision event (`stage` after stage 1, `aborted`, `promoted`) comes after the `sample` event
that triggered it, and before any later `sample` or `resumed` event. An abort because the ingress
died can happen at any time, between samples. The tests apply the rollback rule above to your own
`sample` events, starting a fresh window at each `resumed` event, and check that each decision
follows from them. They also compare your sample counts with `app-green`'s own request counter.

When you finish, leave the ingress running with 100 % of traffic on `app-blue` and no rollout in
progress.

## How it will be checked

The tests send requests through the ingress from inside the namespace at a steady 60 per
second, each on a new connection, and record which target answered. They inject faults into the
targets deterministically: a target set to fail 8 % of requests fails exactly 8 of every 100. They
also read the targets' own request counters. Rollouts use a 15-second dwell. The tests poll the
status file every 0.2 s. The **abort time** is the first moment it shows `aborted`.

- A healthy rollout is promoted through all three stages in order. The live split is checked at
  every stage, and no client request fails.
- Rollouts with `app-green` failing 3 % of its requests, with `app-blue` failing 30 % of its
  requests, or with uneven bursts of traffic must all be promoted. The live split is checked at
  every stage.
- With no traffic, the rollout must not advance. Once traffic arrives, it proceeds.
- Each of these faults must cause an abort within the given time of the fault starting:
  - `app-green` failing 8 % of its requests at 50/50: 18 s.
  - `app-green` crashing at 50/50: 8 s.
  - `app-green` hanging (accepting connections but never answering) during the final 0/100
    stage: 20 s.
  - The ingress being killed at 50/50 while the engine runs: 10 s, after which it serves 100 %
    `app-blue`.
  - `app-green` starting to fail 8 % of its requests at 50/50 at the moment the engine is
    killed, with `canaryctl up` run 2 s later: 25 s.
- During a healthy rollout, the engine is killed several times and the node is rebooted once,
  each followed by `canaryctl up`. The rollout must still be promoted under the same
  `rollout_id`, with the live split checked at every stage. No client request may fail, except
  between a reboot and 5 s after the following `canaryctl up` returns.
- A rollout is aborted, then the node is rebooted. After `canaryctl up` it is still aborted, with
  all traffic on `app-blue`.
- After an abort, requests sent from 1 s after the abort time are all served by `app-blue`
  without error. `app-green`'s own counter does not increase from 2 s after the abort time,
  including after `app-green` recovers. Requests already in flight at the abort may finish
  either way.
- The status file is checked against the schema above on every poll, and the event log against
  its schema. The process properties above are checked on every non-target process in the
  namespace, which must include the `:8080` listeners and, while shifting, the engine. The
  `:8080` listeners and every process holding a connection to a target must be
  `/usr/sbin/nginx`.
