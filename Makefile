# Arc — developer entry points. Keep in sync with the command list in CLAUDE.md.
# Written for GNU make 3.81 (the version shipped with macOS): no ::=, no
# .RECIPEPREFIX, no $(file ...).

SHELL := /bin/bash

# Which torrent client runs: `vpn` (qBittorrent inside gluetun, the default)
# or `novpn` (on the backend network directly). Compose reads COMPOSE_PROFILES
# from .env by itself, but it has no notion of a *default* profile — so an .env
# written before the VPN existed would start no torrent client at all. Hence
# the default here: the environment wins, then .env, then `vpn`.
DOTENV_PROFILES := $(shell sed -n 's/^COMPOSE_PROFILES=//p' .env 2>/dev/null | tail -1)
PROFILES := $(strip $(if $(COMPOSE_PROFILES),$(COMPOSE_PROFILES),$(if $(DOTENV_PROFILES),$(DOTENV_PROFILES),vpn)))

# Media on a dedicated disk: when ARC_DATA_DIR is set in .env, the host
# override binds the arc_data volume to it (deploy/docker-compose.host.yml).
DOTENV_ARC_DATA_DIR := $(shell sed -n 's/^ARC_DATA_DIR=//p' .env 2>/dev/null | tail -1)
HOST_OVERRIDE := $(if $(strip $(DOTENV_ARC_DATA_DIR)),-f deploy/docker-compose.host.yml,)

COMPOSE := COMPOSE_PROFILES=$(PROFILES) docker compose --env-file .env -f deploy/docker-compose.yml $(HOST_OVERRIDE)
COMPOSE_DEV := $(COMPOSE) -f deploy/docker-compose.dev.yml

# Every profile, for the verbs that only ever look at or remove what is already
# running: `down` and `ps` filter by profile too, so a stack brought up in one
# mode and stopped in the other would leave a container behind.
COMPOSE_ANY := COMPOSE_PROFILES=vpn,novpn docker compose --env-file .env -f deploy/docker-compose.yml $(HOST_OVERRIDE)

# Load the root .env into the environment of a recipe. Its URLs are already
# host-side (Compose overrides them for containers), so nothing is rewritten
# here. Trailing ';' so it can prefix a command.
LOAD_ENV := set -a; if [ -f .env ]; then . ./.env; fi; set +a; case "$${DATA_DIR:-}" in ""|/*) ;; *) export DATA_DIR="$$PWD/$${DATA_DIR\#./}";; esac;

.PHONY: help dev dev-db test lint fmt migrate revision up down logs ps clean \
	backup backups restore

help:
	@echo "Arc — make targets"
	@echo "  make dev      db + qbittorrent in docker, api + worker + vite on the host"
	@echo "  make dev-db   just postgres, in docker, waited for until healthy"
	@echo "  make test     pytest (server) + vitest (client)"
	@echo "  make lint     ruff + mypy (server), eslint + tsc + prettier (client)"
	@echo "  make fmt      ruff format (server) + prettier (client)"
	@echo "  make migrate  alembic upgrade head"
	@echo "  make up       production compose stack: build, migrate, start"
	@echo "                (torrent client mode: $(PROFILES) — COMPOSE_PROFILES in .env)"
	@echo "  make down     stop the compose stack"
	@echo "  make backup   take a Postgres dump now"
	@echo "  make backups  list the dumps in the backups volume"
	@echo "  make restore file=<name.sql.gz> [db=<database>]   put one back"

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

# Build, migrate, start — in that order, and the order is the point. The
# migration runs as a **one-off `api` container** rather than as a service:
# `docker compose run` starts `db` first (the api's `depends_on` waits for it
# to be healthy), applies the migration, exits with alembic's status, and
# leaves nothing behind. A migrate *service* would need its own healthcheck
# and a `service_completed_successfully` condition on every other service to
# say the same thing, and would still be running on the next `up`.
#
# Migrating before `up -d` and not after matters: the api creates the
# bootstrap admin at startup and the worker starts claiming jobs immediately,
# and neither can do that against a schema that is not there yet.
#
# `uv` is not in the runtime image — only the virtualenv it built is, and that
# is on PATH — so this is `alembic`, not `uv run alembic`.
up:
	$(COMPOSE) build
	$(COMPOSE) run --rm api alembic upgrade head
	$(COMPOSE) up -d

down:
	$(COMPOSE_ANY) down

logs:
	$(COMPOSE_ANY) logs -f --tail=100

ps:
	$(COMPOSE_ANY) ps

# --- Backups ----------------------------------------------------------------
# The `backup` service dumps nightly on its own (deploy/backup.sh); these are
# the on-demand halves. `run --rm` rather than `exec` so they work whether or
# not the loop container happens to be up.

backup:
	$(COMPOSE) run --rm backup once

backups:
	$(COMPOSE) run --rm --entrypoint ls backup -lh /backups

# Usage: make restore file=arc-20260908T031500Z.sql.gz [db=arc_scratch]
# Without db= this restores over the live database — stop api and worker
# first. With db= it restores into a scratch database, which is how a backup
# is verified without betting the deployment on it (deploy/restore.sh).
restore:
	@test -n "$(file)" || { \
		echo 'usage: make restore file=<name.sql.gz> [db=<database>]' >&2; \
		echo '(make backups lists what is there)' >&2; exit 2; }
	$(COMPOSE) run --rm --entrypoint /bin/bash backup /restore.sh "$(file)" "$(db)"

clean:
	rm -rf server/.pytest_cache server/.mypy_cache server/.ruff_cache
	find server -name '__pycache__' -type d -prune -exec rm -rf {} +
