# Arc — developer entry points. Keep in sync with the command list in CLAUDE.md.
# Written for GNU make 3.81 (the version shipped with macOS): no ::=, no
# .RECIPEPREFIX, no $(file ...).

SHELL := /bin/bash

COMPOSE := docker compose --env-file .env -f deploy/docker-compose.yml
COMPOSE_DEV := $(COMPOSE) -f deploy/docker-compose.dev.yml

# Load the root .env into the environment of a recipe. Its URLs are already
# host-side (Compose overrides them for containers), so nothing is rewritten
# here. Trailing ';' so it can prefix a command.
LOAD_ENV := set -a; if [ -f .env ]; then . ./.env; fi; set +a; case "$${DATA_DIR:-}" in ""|/*) ;; *) export DATA_DIR="$$PWD/$${DATA_DIR\#./}";; esac;

.PHONY: help dev dev-db test lint fmt migrate revision up down logs ps clean

help:
	@echo "Arc — make targets"
	@echo "  make dev      db + qbittorrent in docker, api + worker + vite on the host"
	@echo "  make dev-db   just postgres, in docker, waited for until healthy"
	@echo "  make test     pytest (server) + vitest (client)"
	@echo "  make lint     ruff + mypy (server), eslint + tsc + prettier (client)"
	@echo "  make fmt      ruff format (server) + prettier (client)"
	@echo "  make migrate  alembic upgrade head"
	@echo "  make up       production compose stack, built and detached"
	@echo "  make down     stop the compose stack"

# --- Development ------------------------------------------------------------

dev:
	./scripts/dev.sh

# Postgres alone, published on localhost:5432, waited for until the compose
# healthcheck passes. The server test suite needs it; `make test` depends on
# this target so a cold checkout works with one command.
dev-db:
	@$(COMPOSE_DEV) up -d db
	@printf 'waiting for postgres'; \
	for i in $$(seq 1 60); do \
		state=$$($(COMPOSE_DEV) ps --format '{{.Health}}' db 2>/dev/null); \
		if [ "$$state" = "healthy" ]; then echo " ok"; exit 0; fi; \
		printf '.'; sleep 1; \
	done; \
	echo " timed out after 60s"; $(COMPOSE_DEV) logs --tail=30 db; exit 1

# --- Quality ----------------------------------------------------------------

test: dev-db
	cd server && uv run pytest
	pnpm --dir client test --run

# ruff covers `scripts/` as well as `server/`: the capture script imports the
# application's own query documents, so it is Arc's code and is held to Arc's
# rules. `--config` is explicit because ruff would otherwise look for a config
# next to `../scripts`, find none, and quietly lint it with its defaults —
# a different line length and a narrower rule set than the rest of the repo.
RUFF := uv run ruff
RUFF_PATHS := . ../scripts
RUFF_CONFIG := --config pyproject.toml

lint:
	cd server && $(RUFF) check $(RUFF_CONFIG) $(RUFF_PATHS)
	cd server && $(RUFF) format --check $(RUFF_CONFIG) $(RUFF_PATHS)
	cd server && uv run mypy arc
	pnpm --dir client lint
	pnpm --dir client exec tsc -b --noEmit
	pnpm --dir client format:check

fmt:
	cd server && $(RUFF) format $(RUFF_CONFIG) $(RUFF_PATHS)
	cd server && $(RUFF) check --fix $(RUFF_CONFIG) $(RUFF_PATHS)
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
