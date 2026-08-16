.DEFAULT_GOAL := help

UV ?= uv
SRC := src tests
COMPOSE_NETWORK ?= evenkeel_default

# Colour only when a human is watching.
#
# NO_COLOR (https://no-color.org) wins over everything; FORCE_COLOR is the
# escape hatch for a pager or a CI job that renders ANSI. The check is on
# stderr, not stdout: `$(shell ...)` has its stdout captured by make, so
# `test -t 1` is false even in a terminal, while stderr is inherited.
#
# GNU Make 4.1 has MAKE_TERMOUT for exactly this, and macOS ships 3.81.
ifdef NO_COLOR
COLOUR :=
else
COLOUR := $(if $(FORCE_COLOR),1,$(shell test -t 2 && echo 1))
endif

ifeq ($(COLOUR),1)
ESC := $(shell printf '\033')
BOLD := $(ESC)[1m
DIM := $(ESC)[2m
RED := $(ESC)[31m
GREEN := $(ESC)[32m
YELLOW := $(ESC)[33m
CYAN := $(ESC)[36m
RESET := $(ESC)[0m
endif

# Announce a step. Everything below prints one, so a failure inside a five-part
# gate says which part without scrolling back.
#
# The quality recipes are silenced with `@` because the banner already names
# them and make's echo of the command line on top of it is noise. Everything
# else keeps its echo: a docker invocation or an alembic command is worth
# reading, and often worth copying.
step = @printf '$(CYAN)$(BOLD)▸$(RESET) $(BOLD)%s$(RESET)\n' $(1)

# The gates `check` runs, in the order CI runs them: cheapest first, so a
# formatting slip does not wait behind the test suite.
GATES := lint arch types schema-check test

.PHONY: help sync fmt lint arch types schema schema-check test test-integration \
	check run run-mcp migrate revision new-vertical demo load clean

##@ General

help: ## Show this help
	@printf '\n$(BOLD)evenkeel$(RESET) $(DIM)- a FastAPI template that keeps its claims checkable$(RESET)\n'
	@awk 'BEGIN {FS = ":.*## "} \
		/^##@ / { printf "\n$(BOLD)$(YELLOW)%s$(RESET)\n", substr($$0, 5); next } \
		/^[a-zA-Z0-9_.-]+:.*## / { printf "  $(CYAN)%-17s$(RESET) %s\n", $$1, $$2 }' \
		$(MAKEFILE_LIST)
	@printf '\n$(DIM)NO_COLOR=1 disables colour, FORCE_COLOR=1 forces it.$(RESET)\n\n'

##@ Setup

sync: ## Install the development environment
	$(call step,sync)
	$(UV) sync --all-extras

clean: ## Remove local caches
	$(call step,clean)
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov .import_linter_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

##@ Quality

fmt: ## Format
	$(call step,fmt)
	@$(UV) run ruff format $(SRC)

lint: ## Lint (formatting is checked, not applied)
	$(call step,lint)
	@$(UV) run ruff format --check $(SRC)
	@$(UV) run ruff check $(SRC)

arch: ## Verify the layer contracts
	$(call step,arch)
	@$(UV) run lint-imports

types: ## Type-check in strict mode
	$(call step,types)
	@$(UV) run mypy src

schema-check: ## Fail if openapi.json no longer matches the application
	$(call step,schema-check)
	@$(UV) run python tools/dump_openapi.py --check

test: ## Run everything that needs no external service
	$(call step,test)
	@$(UV) run pytest -m "not integration"

test-integration: ## Run tests against a real database (needs Docker)
	$(call step,test-integration)
	@$(UV) run pytest -m integration

# One command, and it is the same list CI runs — a local gate weaker than CI
# only moves the failure to the pull request.
#
# Every gate runs even after one fails, and the summary reports all of them.
# make's own fail-fast tells you about one broken thing at a time and charges a
# full rerun to find the next. Same reasoning as the aggregating `gate` job in
# the CI workflow, which exists so a skipped check cannot pass as a green tick.
check: ## Full local quality gate, the same list CI runs
	@failed=""; \
	for gate in $(GATES); do \
		start=$$(date +%s); \
		if $(MAKE) --no-print-directory $$gate; then \
			printf '$(GREEN)\xe2\x9c\x93$(RESET)  %-14s $(DIM)%ss$(RESET)\n' "$$gate" "$$(( $$(date +%s) - start ))"; \
		else \
			printf '$(RED)\xe2\x9c\x97$(RESET)  %-14s $(DIM)%ss$(RESET)\n' "$$gate" "$$(( $$(date +%s) - start ))"; \
			failed="$$failed $$gate"; \
		fi; \
	done; \
	printf '\n'; \
	if [ -n "$$failed" ]; then \
		printf '$(RED)$(BOLD)\xe2\x9c\x97 failed:$(RESET)$(RED)%s$(RESET)\n\n' "$$failed"; \
		exit 1; \
	fi; \
	printf '$(GREEN)$(BOLD)\xe2\x9c\x93 every gate passed$(RESET)\n\n'

##@ Run

run: ## Start the API
	$(call step,run)
	$(UV) run evenkeel-web

run-mcp: ## Start the MCP server on stdio (a client normally spawns this itself)
	$(call step,run-mcp)
	$(UV) run evenkeel-mcp

##@ Database

migrate: ## Apply migrations
	$(call step,migrate)
	$(UV) run alembic upgrade head

revision: ## Autogenerate a migration: make revision M="add wallets"
	@test -n "$(M)" || { \
		printf '$(RED)M is required$(RESET), e.g. $(BOLD)make revision M="add wallets"$(RESET)\n'; \
		exit 1; \
	}
	$(call step,revision)
	$(UV) run alembic revision --autogenerate -m "$(M)"

##@ Scaffolding

new-vertical: ## Scaffold a vertical across the layers: make new-vertical NAME=orders
	@test -n "$(NAME)" || { \
		printf '$(RED)NAME is required$(RESET), e.g. $(BOLD)make new-vertical NAME=orders$(RESET)\n'; \
		exit 1; \
	}
	$(UV) run python tools/new_vertical.py --name $(NAME)

##@ Artefacts

schema: ## Regenerate the committed OpenAPI document
	$(call step,schema)
	$(UV) run python tools/dump_openapi.py

demo: ## Re-record docs/demo.gif (needs a running stack)
	$(call step,demo)
	docker build -q -t evenkeel-vhs tools/demo
	docker run --rm -v "$(PWD):/vhs" --network $(COMPOSE_NETWORK) \
		evenkeel-vhs tools/demo/api.tape

load: ## Drive the API under k6 (needs a running stack, see tools/load/README.md)
	$(call step,load)
	docker run --rm -i --network $(COMPOSE_NETWORK) \
		-e BASE_URL=http://api:8000 grafana/k6 run - < tools/load/wallets.js
