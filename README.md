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

Database migrations:

```bash
make migrate              # alembic upgrade head
```

## Tests and linting

```bash
make test                 # pytest (server) + vitest (client)
make lint                 # ruff check, ruff format --check, mypy, eslint
make fmt                  # ruff format + prettier
```

Server-only, from `server/`:

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check . && uv run mypy arc
```

## Layout

```
server/     FastAPI app (arc.main) and worker (arc.worker), Alembic, pytest
client/     React + TypeScript + Vite SPA
deploy/     docker-compose.yml, docker-compose.dev.yml, Caddyfile
scripts/    dev.sh (what `make dev` runs)
```

## Deploy

Everything runs on a single Docker Compose host: `caddy` (TLS + static
client), `api`, `worker`, `db`, `qbittorrent`
([architecture.md §8](architecture.md)).

```bash
cp .env.example .env      # set PUBLIC_HOST, POSTGRES_PASSWORD, SECRET_KEY, FERNET_KEY…
pnpm --dir client build   # Caddy serves client/dist
make up                   # docker compose up -d --build
```

Raw `docker compose` invocations must be run from the repository root with
`--env-file .env`, because `.env` lives at the root rather than in `deploy/`:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml ps
```

The hosting provider is still undecided, and full deployment and operations
notes land with milestone M11 — treat the above as the shape of the deploy,
not as a production runbook.
