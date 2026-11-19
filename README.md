# Canary Rollback Engine

This repository contains a Harbor systems task and its reference solution. The task is to build
an nginx-based ingress and canary rollout engine for two existing services, `app-blue` and
`app-green`. The engine shifts traffic through 90/10, 50/50, and 0/100 stages, monitors the
candidate service, rolls back on failures, and recovers after engine crashes and simulated node
reboots.

The task specification is in [instruction.md](instruction.md). The reference implementation is in
[solution/](solution/), and the verifier is in [tests/](tests/).

## Requirements

- GNU Make
- Docker with the Compose plugin, or Podman with Compose support
- Harbor CLI to run Harbor trials
- A container runtime that supports the task's network namespace and capabilities

The Makefile prefers Docker and falls back to Podman if Docker is unavailable. This task requires
nested network namespace support; rootless Podman is not sufficient. On macOS, use a rootful
Podman machine if Docker is unavailable.

## Build and run locally

Build the task image:

```sh
make build
```

Start the task container:

```sh
make up
```

Install the reference solution into the running container:

```sh
make install
```

Run the full verifier suite against the reference solution:

```sh
make test
```

`make test` builds and starts the environment if needed, installs the reference solution, copies
the tests into the container, and runs them. It exits unsuccessfully unless the verifier reward is
`1`.

Run the verifier's pure-function self-tests on the host:

```sh
make selftest
```

Open a shell in the running container:

```sh
make shell
```

Stop and remove the task containers:

```sh
make down
```

Remove local Python caches and generated build, coverage, and output directories. Harbor job and
trial logs are intentionally preserved:

```sh
make clean
```

## Packaging

Create a solution archive and a Golden Solution archive:

```sh
make package-solution
make package-golden
```

The archives are written under `dist/` by default. The solution archive contains the task,
environment, and reference implementation. The Golden Solution archive also contains the complete
verifier suite, including tests, self-tests, and mutants. Both targets validate their ZIP contents
and exclude Python cache files.

These are package artifacts, not agent Prompt Files. This task has no separate agent input files;
do not attach either archive as Prompt Files.

## Harbor trials

Run the task with Harbor's oracle agent or no-op agent:

```sh
make oracle
make nop
```

These are aliases for `make harbor-oracle` and `make harbor-nop`. The Harbor CLI must be installed
and configured. The selected container environment is passed from the Makefile's `DOCKER` setting;
override `HARBOR`, `HARBOR_ENV`, or `HARBOR_ARGS` as needed, for example:

```sh
make oracle HARBOR_ARGS="--job-name canary-oracle-check"
```
