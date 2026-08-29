SHELL := /bin/bash
.DEFAULT_GOAL := help

HOST ?= 0.0.0.0
PORT ?= 8833

.PHONY: help setup dev dev-gunicorn worker init-db db-migrate db-upgrade test test-cov lint format clean

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

## ── Setup ─────────────────────────────────────────────────────────────────

setup: ## Setup local uv environment, dependencies, .env, and initialize database
	@echo "Syncing dependencies with uv..."
	uv sync
	@if [ ! -f .env ]; then \
		echo "Creating .env from .env.example..."; \
		cp .env.example .env; \
	fi
	@echo "Initializing database..."
	uv run flask --app deaddit.wsgi init-db
	@echo "Setup complete!"

## ── Development ───────────────────────────────────────────────────────────

dev: ## Start the development server on $(HOST):$(PORT) with auto-reload
	@mkdir -p logs
	PYTHONUNBUFFERED=1 uv run flask --app deaddit.wsgi run --host $(HOST) --port $(PORT) --debug 2>&1 | tee -a logs/dev.log

dev-gunicorn: ## Start server using Gunicorn on $(HOST):$(PORT)
	uv run gunicorn -b $(HOST):$(PORT) -c gunicorn.conf.py deaddit.wsgi:app

worker: ## Start the background agent worker process (auto-restarts on code changes)
	@mkdir -p logs
	PYTHONUNBUFFERED=1 uv run watchfiles --filter python deaddit-worker deaddit 2>&1 | tee -a logs/worker.log

## ── Database ──────────────────────────────────────────────────────────────

init-db: ## Run database migrations and seed default settings
	uv run flask --app deaddit.wsgi init-db

db-migrate: ## Generate new database migration
	uv run flask --app deaddit.wsgi db migrate

db-upgrade: ## Apply database migrations
	uv run flask --app deaddit.wsgi db upgrade

## ── Quality & Testing ─────────────────────────────────────────────────────

test: ## Run test suite
	uv run pytest

test-cov: ## Run test suite with coverage report
	uv run pytest --cov=deaddit

lint: ## Check code style with ruff
	uv run ruff check .

format: ## Format code with ruff
	uv run ruff format .

## ── Cleanup ───────────────────────────────────────────────────────────────

clean: ## Remove python caches and temporary build/test artifacts
	rm -rf .pytest_cache .ruff_cache .coverage logs
	find . -type d -name '__pycache__' -not -path './.venv/*' -exec rm -rf {} +
