.DEFAULT_GOAL := help
VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

.PHONY: help venv install dev test lint fmt run merchant settle openapi docker-up docker-down clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv: ## Create the local virtualenv
	test -d $(VENV) || python3 -m venv $(VENV)

install: venv ## Install the package with dev extras
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

dev: ## Run the API with auto-reload on :8000
	$(VENV)/bin/uvicorn app.main:app --reload --port 8000

run: ## Run the API (no reload)
	$(VENV)/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

test: ## Run the test suite
	$(PY) -m pytest

cov: ## Run tests with coverage report
	$(PY) -m pytest --cov=app --cov-report=term-missing

lint: ## Ruff lint + format check
	$(VENV)/bin/ruff check app tests
	$(VENV)/bin/ruff format --check app tests

fmt: ## Auto-format and fix lint
	$(VENV)/bin/ruff format app tests
	$(VENV)/bin/ruff check --fix app tests

merchant: ## Create a demo merchant and print its API key (NAME="Shop" URL=https://...)
	$(PY) -m app.cli create-merchant "$(or $(NAME),Demo Shop)" $(if $(URL),--webhook-url $(URL),)

settle: ## Run settlement for a date (DATE=YYYY-MM-DD, default yesterday)
	$(PY) -m app.cli run-settlement $(if $(DATE),--date $(DATE),)

openapi: ## Export the OpenAPI document to openapi.json
	$(PY) scripts/export_openapi.py

docker-up: ## Start API + Postgres with docker compose
	docker compose up --build -d

docker-down: ## Stop the compose stack
	docker compose down

clean: ## Remove caches and local DB
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage gateway.db
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
