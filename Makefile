# Prefer Docker; use Podman only when Docker is not installed.
IMAGE_TAG ?= local
DOCKER ?= $(shell if command -v docker >/dev/null 2>&1; then echo docker; elif command -v podman >/dev/null 2>&1; then echo podman; else echo docker; fi)
COMPOSE ?= $(DOCKER) compose
COMPOSE_FILE ?= environment/docker-compose.yaml
SERVICE ?= main
DC = $(COMPOSE) -f $(COMPOSE_FILE)
HARBOR ?= harbor
HARBOR_ARGS ?=
HARBOR_ENV ?= $(DOCKER)
ZIP ?= zip
UNZIP ?= unzip
PACKAGE_DIR ?= dist
SOLUTION_ARCHIVE ?= $(PACKAGE_DIR)/canary-rollback-engine-solution.zip
GOLDEN_ARCHIVE ?= $(PACKAGE_DIR)/canary-rollback-engine-golden.zip
ZIP_EXCLUDES = -x '*/__pycache__/*' '*.pyc' '*/.pytest_cache/*' '.DS_Store' '*/.DS_Store'

.PHONY: all check-runtime build up install run test selftest shell down harbor-oracle harbor-nop oracle nop clean package-solution package-golden package-first-solution package-golden-solution

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

check-harbor:
	@if ! command -v "$(HARBOR)" >/dev/null 2>&1; then \
		echo "Harbor CLI '$(HARBOR)' was not found on PATH." >&2; \
		exit 1; \
	elif ! "$(HARBOR)" run --help >/dev/null 2>&1; then \
		echo "'$(HARBOR)' is not the Harbor task runner (it must support 'harbor run')." >&2; \
		exit 1; \
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

clean:
	find "$(CURDIR)" -type d -name '__pycache__' -prune -exec rm -rf -- {} +
	find "$(CURDIR)" -type d \( -name '.pytest_cache' -o -name '.mypy_cache' -o -name '.ruff_cache' -o -name 'htmlcov' \) -prune -exec rm -rf -- {} +
	rm -rf -- "$(CURDIR)/build" "$(CURDIR)/dist" "$(CURDIR)/output" "$(CURDIR)/outputs" "$(CURDIR)/dist"
	rm -f -- "$(CURDIR)/.coverage" "$(CURDIR)/coverage.xml" "$(CURDIR)/junit.xml"

package-solution:
	mkdir -p "$(PACKAGE_DIR)"
	rm -f -- "$(SOLUTION_ARCHIVE)"
	$(ZIP) -r -X -q "$(abspath $(SOLUTION_ARCHIVE))" Makefile README.md instruction.md task.toml environment solution $(ZIP_EXCLUDES)
	$(UNZIP) -tq "$(abspath $(SOLUTION_ARCHIVE))"
	@archive="$$($(UNZIP) -Z1 "$(abspath $(SOLUTION_ARCHIVE))")"; \
	if printf '%s\n' "$$archive" | grep -q '^tests/'; then \
		echo "Solution archive must not contain verifier tests." >&2; exit 1; \
	fi

package-golden: package-solution
	rm -f -- "$(GOLDEN_ARCHIVE)"
	$(ZIP) -r -X -q "$(abspath $(GOLDEN_ARCHIVE))" Makefile README.md instruction.md task.toml environment solution tests $(ZIP_EXCLUDES)
	$(UNZIP) -tq "$(abspath $(GOLDEN_ARCHIVE))"
	@archive="$$($(UNZIP) -Z1 "$(abspath $(GOLDEN_ARCHIVE))")"; \
	for required in instruction.md task.toml environment/Dockerfile environment/docker-compose.yaml \
		solution/solve.sh tests/test.sh tests/conftest.py tests/harness.py tests/scenarios.py \
		tests/test_00_cli.py tests/test_10_promote.py tests/test_20_rollback.py \
		tests/test_25_recovery.py tests/test_30_isolation.py tests/test_90_final.py \
		tests/selftest/test_harness_selftest.py tests/mutants/mutate.py tests/mutants/run_mutants.sh; do \
		if ! printf '%s\n' "$$archive" | grep -Fqx "$$required"; then \
			echo "Golden archive is missing $$required." >&2; exit 1; \
		fi; \
	done; \
	if printf '%s\n' "$$archive" | grep -Eq '(^|/)__pycache__/|\.pyc$$'; then \
		echo "Golden archive must not contain Python cache files." >&2; exit 1; \
	fi

package-first-solution: package-solution

package-golden-solution: package-golden

harbor-oracle: check-harbor 
	"$(HARBOR)" run -p . -a oracle -e "$(HARBOR_ENV)"

harbor-nop: check-harbor 
	"$(HARBOR)" run -p . -a nop -e "$(HARBOR_ENV)"

oracle: harbor-oracle

nop: harbor-nop
