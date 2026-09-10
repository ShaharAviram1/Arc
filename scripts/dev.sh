#!/usr/bin/env bash
# Arc — local development. Started by `make dev`.
#
# Brings up postgres and qBittorrent in Docker, waits for the database to be
# healthy, then runs the API, the worker and the Vite dev server on the host
# so all three hot-reload. Ctrl-C stops all three; the containers keep
# running (stop them with `make down`).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [ ! -f .env ]; then
	echo "No .env found. Run: cp .env.example .env" >&2
	exit 1
fi

# Export everything in .env. Its URLs are host-side already (Compose
# overrides them with the service hostnames inside the containers), so the
# api and worker started below need nothing further.
set -a
# shellcheck disable=SC1091
. ./.env
# A relative DATA_DIR is meant relative to the repo root, but the api and
# worker start from server/ — make it absolute before they inherit it.
case "${DATA_DIR:-}" in ""|/*) ;; *) export DATA_DIR="$PWD/${DATA_DIR#./}";; esac
set +a

# Dev runs the torrent client on the backend network, not behind the VPN: the
# tunnel is a production mitigation (spec §9) and a WireGuard config is not
# something a checkout should need. Set here rather than left to .env so that
# a .env holding the production `COMPOSE_PROFILES=vpn` still gives `make dev`
# a qBittorrent it can reach on localhost.
export COMPOSE_PROFILES=novpn

COMPOSE=(docker compose --env-file .env
	-f deploy/docker-compose.yml
	-f deploy/docker-compose.dev.yml)

echo "==> starting db and qbittorrent"
"${COMPOSE[@]}" up -d db qbittorrent

echo -n "==> waiting for postgres"
for _ in $(seq 1 60); do
	if "${COMPOSE[@]}" exec -T db pg_isready -q -U "${POSTGRES_USER:-arc}" \
		-d "${POSTGRES_DB:-arc}" >/dev/null 2>&1; then
		echo " ready"
		break
	fi
	echo -n "."
	sleep 1
done

pids=()

cleanup() {
	trap - INT TERM EXIT
	echo
	echo "==> stopping api, worker and client"
	for pid in "${pids[@]:-}"; do
		[ -n "$pid" ] && kill "$pid" 2>/dev/null || true
	done
	wait 2>/dev/null || true
	echo "==> stopped (containers still running; 'make down' stops them)"
}
trap cleanup INT TERM EXIT

echo "==> api      http://localhost:8000  (docs at /docs)"
(cd server && exec uv run uvicorn arc.main:app --reload --port 8000) &
pids+=($!)

echo "==> worker"
(cd server && exec uv run python -m arc.worker) &
pids+=($!)

echo "==> client   http://localhost:5173"
pnpm --dir client dev &
pids+=($!)

wait -n 2>/dev/null || wait
