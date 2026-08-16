.DEFAULT_GOAL := help

UV ?= uv
SRC := src tests

.PHONY: help sync fmt lint arch types test test-integration check run run-mcp migrate revision clean

help: ## Show available commands
	@awk 'BEGIN {FS = ":.*## "; printf "\nTargets:\n\n"} /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-18s %s\n", $$1, $$2} END {printf "\n"}' $(MAKEFILE_LIST)

sync: ## Install the development environment
	$(UV) sync --all-extras

fmt: ## Format
	$(UV) run ruff format $(SRC)

lint: ## Lint (formatting is checked, not applied)
	$(UV) run ruff format --check $(SRC)
	$(UV) run ruff check $(SRC)

schema: ## Regenerate the committed OpenAPI document
	$(UV) run python tools/dump_openapi.py

schema-check: ## Fail if openapi.json no longer matches the application
	$(UV) run python tools/dump_openapi.py --check

arch: ## Verify the layer contracts
	$(UV) run lint-imports

types: ## Type-check in strict mode
	$(UV) run mypy src

test: ## Run everything that needs no external service
	$(UV) run pytest -m "not integration"

test-integration: ## Run tests against a real database (needs Docker)
	$(UV) run pytest -m integration

# One command, and it is the same list CI runs. A local gate that is weaker
# than CI just moves the failure to the pull request.
check: lint arch types schema-check test ## Full local quality gate

run-mcp: ## Start the MCP server on stdio (a client normally spawns this itself)
	$(UV) run evenkeel-mcp

run: ## Start the API
	$(UV) run evenkeel-web

migrate: ## Apply migrations
	$(UV) run alembic upgrade head

revision: ## Autogenerate a migration: make revision M="add wallets"
	@test -n "$(M)" || (echo 'M is required, e.g. make revision M="add wallets"' && exit 1)
	$(UV) run alembic revision --autogenerate -m "$(M)"

clean: ## Remove local caches
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov .import_linter_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
