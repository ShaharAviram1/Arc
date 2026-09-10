# syntax=docker/dockerfile:1
#
# The client, built and then baked into the Caddy image that serves it
# (architecture.md §8).
#
# Before M11 the `caddy` service bind-mounted ../client/dist, which meant
# `make up` only worked on a host that had Node, pnpm and a fresh `pnpm build`
# — three things a production host has no other reason to carry, and a silent
# failure mode besides: an empty or stale dist directory mounts perfectly
# happily and serves last week's app. Building here makes the image the unit
# of deployment, and `docker compose build` the only thing the host needs.
#
# Dev is untouched: `make dev` runs Vite on the host and never builds this.

# ---- build: install dependencies and produce /app/dist ---------------------
FROM node:24-alpine AS build

WORKDIR /app

# pnpm through corepack, pinned to the version that wrote the lockfile
# (lockfileVersion 9.0). Unpinned, a future pnpm could resolve differently
# from what `make lint` checked on a developer's machine.
RUN corepack enable && corepack prepare pnpm@10.33.2 --activate

# Manifest first, so the dependency layer is reused when only source changes.
COPY package.json pnpm-lock.yaml ./
RUN --mount=type=cache,target=/root/.local/share/pnpm/store \
    pnpm install --frozen-lockfile

COPY . .
# `pnpm build` is `tsc -b && vite build`: a type error fails the image rather
# than shipping a bundle nobody type-checked.
RUN pnpm build

# ---- web: Caddy with the built client inside it ----------------------------
FROM caddy:2 AS web

# Where the Caddyfile's `root * /srv` points. Copied, not mounted: the served
# files and the image that serves them version together.
COPY --from=build /app/dist /srv
