# Use Podman as the Docker CLI when available.
DOCKER ?= $(shell if command -v podman >/dev/null 2>&1; then echo podman; else echo docker; fi)
COMPOSE ?= $(DOCKER) compose
COMPOSE_FILE ?= environment/docker-compose.yaml
SERVICE ?= main
DC = $(COMPOSE) -f $(COMPOSE_FILE)

.PHONY: all check-runtime build up install run test selftest shell down

all: test

check-runtime:
	@if [ "$(DOCKER)" = "podman" ]; then \
		rootless=$$(podman info --format '{{.Host.Security.Rootless}}') || exit $$?; \
		if [ "$$rootless" = "true" ]; then \
			echo "This environment requires nested network namespaces; rootless Podman cannot start it." >&2; \
			echo "Use a rootful Podman machine, then rerun make." >&2; \
			echo "On macOS: podman machine stop && podman machine set --rootful && podman machine start" >&2; \
			exit 1; \
		fi; \
	fi

build: check-runtime
	$(DC) build

up: build
	$(DC) up -d

install: up
	$(DC) exec -T $(SERVICE) mkdir -p /solution
	$(DC) cp solution/. $(SERVICE):/solution/
	$(DC) exec -T $(SERVICE) bash /solution/solve.sh

run: install

test: install
	$(DC) exec -T $(SERVICE) /opt/canary/bin/canaryctl reset
	$(DC) exec -T $(SERVICE) mkdir -p /tests
	$(DC) cp tests/. $(SERVICE):/tests/
	$(DC) exec -T $(SERVICE) bash -lc 'bash /tests/test.sh; test "$$(cat /logs/verifier/reward.txt)" = 1'

selftest:
	CANARY_SELFTEST=1 python3 -m pytest -p no:cacheprovider tests/selftest

shell: up
	$(DC) exec $(SERVICE) bash

down:
	$(DC) down
