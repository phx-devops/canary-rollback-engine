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
- `solution/` — reference implementation: the nginx ingress and Python
  rollout controller.
- `tests/` — pytest verifier, self-tests for the verifier, and mutation tests.
- `RUNNING.md` — detailed environment, grading, and validation notes.

## Requirements

- Docker with the Docker Compose plugin, or rootful Podman with a Compose provider
- A shell on the host

The container image provides the task's runtime dependencies, including
Python, pytest, Go, and the networking utilities. The solution does not need
runtime network access. Rootless Podman is not supported because the
environment creates and enters a nested network namespace.

## Build and start the environment

Run these commands from the repository root. `make up` builds the image and
starts the environment; `make shell` opens a root shell in the task container:

```sh
make up
make shell
```

The task environment and test workflow are described in [RUNNING.md](RUNNING.md).

## Install and run the reference solution

`make install` starts the environment if needed, copies the reference solution
into the container, and runs its setup script:

```sh
make install
```

This installs the controller under `/opt/canary/bin`, then starts the ingress
at 100% blue.

## Run the verifier

Run the full verifier suite with:

```sh
make test
```

The verifier writes its reward and logs under `/logs/verifier/`. Additional
pytest arguments can be passed to select tests or scenarios:

```sh
docker compose -f environment/docker-compose.yaml exec main bash /tests/test.sh -k "crash or hang"
```

Run the verifier's pure self-tests on the host with `make selftest`. Use
`make down` to stop and remove the container. For the mutation check, see
[tests/README.md](tests/README.md).
