# Arc

Arc is a multi-user, self-hosted anime server with a browser client: it keeps
track of what you and the people you invite are watching, acquires the next
unwatched episodes on its own, transcodes them to browser-playable HLS with
burned-in subtitles, and streams them back. It syncs progress with
MyAnimeList and can argue a case for what to watch next.

The three documents below are the source of truth and are kept current with
the code: [spec.md](spec.md) (what it does and why),
[architecture.md](architecture.md) (stack, layout, data model, flows), and
[roadmap.md](roadmap.md) (milestones and their definitions of done).

## Prerequisites

- [uv](https://docs.astral.sh/uv/) — manages the Python 3.14 toolchain and
  the server's virtualenv (`uv python install 3.14` if you have no 3.14 yet)
- Node 24 and [pnpm](https://pnpm.io/) — for the client
- Docker with Compose v2+ — for Postgres, qBittorrent, and the production stack
- ffmpeg is **not** needed on the host: it ships inside the server image and
  is only exercised by the transcode pipeline (M7)

## Running locally

```bash
cp .env.example .env      # dev-safe defaults; edit before deploying anywhere
make dev
```

`make dev` starts Postgres and qBittorrent in Docker, waits for the database,
then runs the API, the worker and the Vite dev server on the host with hot
reload. Ctrl-C stops all three; the containers keep running (`make down`).

- Client — <http://localhost:5173>
- API docs — <http://localhost:8000/docs>
- Health — <http://localhost:8000/api/health>
- qBittorrent Web UI — <http://localhost:8080> (dev only; in production it is
  bound to the internal Docker network)

### qBittorrent's first-run password

`linuxserver/qbittorrent` (4.6+) will not take a password from the
environment. On the container's **first** start it generates a temporary one
and prints it to the log; set a permanent one to match `QBIT_PASS` in `.env`
(default `admin` / `adminadmin`) once, and it is kept in the `qbit_config`
volume from then on:

```bash
docker logs arc-qbittorrent-1 | grep -i "temporary password"
TMP=<the password it printed>
curl -c /tmp/qb -H 'Referer: http://localhost:8080' \
     -d "username=admin&password=$TMP" \
     http://localhost:8080/api/v2/auth/login
curl -b /tmp/qb -H 'Referer: http://localhost:8080' \
     --data-urlencode 'json={"web_ui_password":"adminadmin"}' \
     http://localhost:8080/api/v2/app/setPreferences
```

In dev the container's `/data/downloads` is bind-mounted to the repository's
`data/downloads` (`deploy/docker-compose.dev.yml`) so the worker, which runs
on the host, can open the files qBittorrent finished. Point `DATA_DIR` at that
same `data/` directory — from `server/` that means an absolute path, e.g.
`export DATA_DIR="$PWD/../data"` — or the worker will look for the downloads
under `server/data`. Production keeps the `arc_data` named volume, shared
between `api`, `worker` and `qbittorrent`, and needs none of this.

`.env` holds host-side URLs (`localhost`); Docker Compose overrides
`DATABASE_URL` and `QBIT_URL` for the `api` and `worker` containers with the
`db` / `qbittorrent` service hostnames. So the api and worker can also be run
one at a time from `server/`, with the database up (`make dev-db`) and no
exports of any kind:

```bash
cd server
uv run uvicorn arc.main:app --reload --port 8000
uv run python -m arc.worker
```

Database:

```bash
make dev-db               # just Postgres, in Docker, waited for until healthy
make migrate              # alembic upgrade head
make revision m="…"       # autogenerate a migration, then review it by hand
```

## Tests and linting

```bash
make test                 # pytest (server) + vitest (client); starts the db first
make lint                 # ruff check, ruff format --check, mypy, eslint
make fmt                  # ruff format + prettier
```

Server-only, from `server/`:

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy arc
```

The server tests marked `pg` run against a throwaway `arc_test` database
(override with `TEST_DATABASE_URL`), which they create and migrate on first
use. They fail loudly if Postgres is not running — start it with `make
dev-db`, or set `ARC_SKIP_PG_TESTS=1` to skip them on a machine without
Docker.

## Layout

```
server/     FastAPI app (arc.main), worker (arc.worker), CLI (arc.cli), Alembic, pytest
client/     React + TypeScript + Vite SPA, and the Dockerfile that builds it into Caddy
deploy/     docker-compose.yml, docker-compose.dev.yml, Caddyfile, backup/restore, README (the runbook)
scripts/    dev.sh (what `make dev` runs)
```

## Operator commands

`python -m arc.cli` is the handful of things that have to be done on a host
with no browser session yet. Every command is idempotent, so re-running one
after a half-finished deploy is safe.

```bash
cd server
uv run python -m arc.cli status         # users, lists, episodes, jobs, disk, source health
uv run python -m arc.cli invite --email prof@example.edu     # prints the link, once
uv run python -m arc.cli warm-catalogue                       # season cache + refresh sweep
uv run python -m arc.cli import-catalogue                     # offline catalogue, now
uv run python -m arc.cli demo-list --user-email prof@example.edu \
    --add "Sousou no Frieren" --add "Vinland Saga"
```

In production the same commands run inside the api container, which already
knows where the database is:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml \
  run --rm api python -m arc.cli status
```

- `invite` — a seven-day, single-use link (`--expires-in-hours` up to 720).
  Printed once: Arc stores only `sha256(token)`. `--admin` promotes an address
  that already has an account — invites carry no role, so the order is
  invite → accept → `--admin`.
- `warm-catalogue` — queues the season pre-cache (what the schedule renders
  from) and a refresh of every followed show. Without it a fresh deployment
  has an empty schedule until 03:30 UTC.
- `import-catalogue` — downloads and imports the offline catalogue (manami's
  anime database + Fribb's id map, ~14 MB, about half a minute) instead of
  waiting for the weekly job on Monday at 03:30 UTC. Replaces what it imported
  last time, and does nothing at all when neither file has changed. Exits 1 if
  either source failed; the tables of a source that failed are untouched.
- `demo-list` — adds shows to a user's list as *watching*, looked up by title
  through the catalogue, so a new user's first Home page is not empty. It
  prints the title it matched. A list entry is what drives acquisition, so
  this will start Arc looking for episodes unless acquisition is paused.
- `status` — read-only; safe against a live deployment.

## Deploy

Everything runs on a single Docker Compose host: `caddy` (TLS + the built
client), `api`, `worker`, `db`, `qbittorrent` behind a `gluetun` WireGuard
sidecar, and a `backup` sidecar ([architecture.md §8](architecture.md)).

Only the torrent client uses the VPN, and it has no other route out —
`COMPOSE_PROFILES=vpn` in `.env` (the default) is the switch, `novpn` runs
qBittorrent on the host's own address instead. Arc also tells the client not
to seed and caps its upload. Both are in
[deploy/README.md §5](deploy/README.md); `make dev` never uses the VPN.

```bash
cp .env.example .env      # then fill in the production keys — see below
make up                   # build, migrate, start
```

`make up` is three steps in order: `docker compose build`, then the migration
as a one-off `api` container (`… run --rm api alembic upgrade head`), then
`up -d`. The client is built **inside** the image by `client/Dockerfile`, so
the host needs no Node and there is no `client/dist` to keep in step.

Production keys that must be real before `make up` — `PUBLIC_HOST`,
`PUBLIC_URL`, `SECRET_KEY`, `FERNET_KEY` (generate once, **never rotate**),
`POSTGRES_PASSWORD`, `MAL_CLIENT_ID` / `MAL_CLIENT_SECRET`, `MAL_REDIRECT_URI`
(`https://<host>/api/mal/callback`, registered on the MAL application too),
`QBIT_PASS`, the `WIREGUARD_*` values from the VPN provider's config file
(unless `COMPOSE_PROFILES=novpn`), `BOOTSTRAP_ADMIN_*` for the first boot, and
`ANTHROPIC_API_KEY` for phase 2. Arc checks the list itself: in production the api and worker log
one ERROR per key that is missing or still an example value, and
`GET /api/health` reports the count as `config_warnings` (a healthy deployment
answers `0`).

Backups: the `backup` service dumps Postgres nightly into the `backups`
volume and keeps 14 days.

```bash
make backup                                  # take one now
make backups                                 # list them
make restore file=<name>.sql.gz              # over the live database
make restore file=<name>.sql.gz db=arc_check # into a scratch db, to verify one
```

Raw `docker compose` invocations must be run from the repository root with
`--env-file .env`, because `.env` lives at the root rather than in `deploy/`:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml ps
```

**The full runbook is [deploy/README.md](deploy/README.md)**: host
requirements and disk sizing, DNS, the whole `.env` table, registering the MAL
redirect URI, qBittorrent's first-run password in production, the first login
and the professor's invite, seeding a fresh deployment, pausing and resuming
acquisition, the VPN (getting a WireGuard config, verifying the tunnel, running
without it), logs, backups and restore, upgrading, and a troubleshooting table.

The host is a Hetzner CX33 with a 250 GB volume ([spec.md §9](spec.md)); the
runbook assumes nothing about the provider beyond its own requirements
section.
