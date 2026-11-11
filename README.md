# Canary Rollback Engine

This Terminal Bench task challenges an agent to build an automated canary
deployment toolchain for two versions of a web service. The toolchain must
shift traffic from `app-blue` to `app-green` in stages, evaluate green's
request failures, and automatically roll back when the candidate or ingress
proxy fails.

The task also tests process isolation: the ingress and rollout engine must run
detached inside a Linux network namespace as an unprivileged user. The complete
requirements and command-line contract are in [instruction.md](instruction.md).

## Repository layout

- `environment/` — Ubuntu-based Docker environment, network-namespace setup,
  and the sample applications.
- `solution/` — reference implementation: a Go ingress proxy and Python
  rollout controller. `solution/alternatives/haproxy/` contains an alternate
  implementation.
- `tests/` — pytest verifier, self-tests for the verifier, and mutation tests.
- `RUNNING.md` — detailed environment, grading, and validation notes.

## Requirements

- Docker with the Docker Compose plugin
- A shell on the host

The container image provides the task's runtime dependencies, including
Python, pytest, Go, and the networking utilities. Runtime network access is
not required.

## Build and start the environment

Run these commands from the repository root:

```sh
docker compose -f environment/docker-compose.yaml build
docker compose -f environment/docker-compose.yaml up -d
docker compose -f environment/docker-compose.yaml exec client bash
```

The final command opens a root shell in the task container. The task environment
and test workflow are described in [RUNNING.md](RUNNING.md).

## Install and run the reference solution

From the repository root, copy the reference solution into the container and
run its setup script:

```sh
docker compose -f environment/docker-compose.yaml cp solution client:/solution
docker compose -f environment/docker-compose.yaml exec client bash /solution/solve.sh
```

This builds and installs the proxy and controller under `/opt/canary/bin`, then
starts the ingress at 100% blue.

## Run the verifier

Copy the tests into the running container and run the full suite:

```sh
docker compose -f environment/docker-compose.yaml cp tests client:/tests
docker compose -f environment/docker-compose.yaml exec client bash /tests/test.sh
```

The verifier writes its reward and logs under `/logs/verifier/`. Additional
pytest arguments can be passed to select tests or scenarios:

```sh
docker compose -f environment/docker-compose.yaml exec client bash /tests/test.sh -k "crash or hang"
```

For verifier self-tests and the mutation check, see [tests/README.md](tests/README.md).
