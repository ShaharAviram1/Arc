# Arc — developer entry points. Keep in sync with the command list in CLAUDE.md.
# Written for GNU make 3.81 (the version shipped with macOS): no ::=, no
# .RECIPEPREFIX, no $(file ...).

SHELL := /bin/bash

COMPOSE := docker compose --env-file .env -f deploy/docker-compose.yml
COMPOSE_DEV := $(COMPOSE) -f deploy/docker-compose.dev.yml

# Load the root .env into the environment of a recipe, preferring the DEV_*
# variants (host processes talk to the published ports, not the compose
# service names). Trailing ';' so it can prefix a command.
LOAD_ENV := set -a; if [ -f .env ]; then . ./.env; fi; set +a; \
	export DATABASE_URL="$${DEV_DATABASE_URL:-$$DATABASE_URL}"; \
	export QBIT_URL="$${DEV_QBIT_URL:-$$QBIT_URL}";

.PHONY: help dev test lint fmt migrate revision up down logs ps clean

help:
	@echo "Arc — make targets"
	@echo "  make dev      db + qbittorrent in docker, api + worker + vite on the host"
	@echo "  make test     pytest (server) + vitest (client)"
	@echo "  make lint     ruff + mypy (server), eslint + tsc + prettier (client)"
	@echo "  make fmt      ruff format (server) + prettier (client)"
	@echo "  make migrate  alembic upgrade head"
	@echo "  make up       production compose stack, built and detached"
	@echo "  make down     stop the compose stack"

# --- Development ------------------------------------------------------------

dev:
	./scripts/dev.sh

# --- Quality ----------------------------------------------------------------

test:
	cd server && uv run pytest
	pnpm --dir client test --run

lint:
	cd server && uv run ruff check .
	cd server && uv run ruff format --check .
	cd server && uv run mypy arc
	pnpm --dir client lint
	pnpm --dir client exec tsc -b --noEmit
	pnpm --dir client format:check

fmt:
	cd server && uv run ruff format .
	cd server && uv run ruff check --fix .
	pnpm --dir client format

# --- Database ---------------------------------------------------------------

migrate:
	$(LOAD_ENV) cd server && uv run alembic upgrade head

# Usage: make revision m="add users table"
revision:
	$(LOAD_ENV) cd server && uv run alembic revision --autogenerate -m "$(m)"

# --- Deployment -------------------------------------------------------------

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100

ps:
	$(COMPOSE) ps

clean:
	rm -rf server/.pytest_cache server/.mypy_cache server/.ruff_cache
	find server -name '__pycache__' -type d -prune -exec rm -rf {} +
