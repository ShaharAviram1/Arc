# Arc — deployment and operations runbook

Everything Arc needs runs on **one Docker Compose host**
([architecture.md §8](../architecture.md)): `caddy` (TLS + the built client),
`api`, `worker`, `db`, `qbittorrent` behind a `gluetun` VPN sidecar, and a
`backup` sidecar.

The host is a Hetzner Cloud CPX22 (2 vCPU, 4 GB) with a 100 GB volume, in its
own project ([spec.md §9](../spec.md)) — but nothing below assumes it: this is everything
up to and including "run it on the box", for any box that meets §1.

---

## 1. What the host must be

| Requirement | Why |
|---|---|
| **BitTorrent allowed** by the provider's terms | Arc downloads over BitTorrent. This is a terms-of-service question, not a technical one, and it decides the provider. The traffic leaves through a VPN (§5), not the host's own address, but the provider's terms still apply to what the box is for. |
| **`/dev/net/tun` usable by containers** | The VPN sidecar needs it. Every ordinary VPS has it; a few container hosts do not, and those can only run `COMPOSE_PROFILES=novpn` (§5.4). |
| **≥ 2 vCPU** | Software x264 at `veryfast` transcodes a 24-minute 1080p episode in roughly real time on two cores, so on a 2-vCPU host an episode is ready 10–15 minutes after download. Keep `MAX_TRANSCODES` ≤ vCPU/2 (so `1` on two cores) so an encode never starves the API. |
| **≥ 100 GB persistent disk** | See the sizing table below. On a cloud host, a separate volume that can grow later; point `ARC_DATA_DIR` at it (§2.2). |
| **≥ 4 GB RAM** | Postgres, two Python processes, ffmpeg, qBittorrent. On exactly 4 GB add a 2 GB swap file; ffmpeg's peak sits next to everything else. |
| **Ports 80 and 443** reachable | Caddy and ACME. Behind the VPN, BitTorrent arrives through the tunnel and needs no hole in the firewall; in `novpn` mode open 6881 (TCP+UDP) as well. |
| Docker with Compose v2+ | Nothing else — no Node, no Python, no ffmpeg on the host. |

### Disk sizing

Budget **≈ 2 GB per retained episode**: about **1.4 GB** for a 1080p source
and **≈ 0.5 GB** for its HLS rendition. Both are kept, because a rendition can
be redone from the source (FR-P5).

| Episodes retained at once | Disk for media |
|---|---|
| 25 | ~50 GB |
| 50 | ~100 GB |
| 100 | ~200 GB |

What bounds that number is **retention**, not the disk: `look_ahead_n` (N, how
many unwatched episodes ahead Arc keeps), `grace_days_g` (G, days a watched
episode's files survive) and `unwatched_days_d` (D, days a ready-but-unwatched
episode survives before the want is dropped). All three live in the `settings`
table and are admin-editable — they are **not** environment variables. The one
environment knob is `RETENTION_DRY_RUN=true`, which makes the sweep log
everything it would delete and delete nothing; leave it on for the first night
of a new deployment, then turn it off.

---

## 2. First deploy

### 2.1 DNS

Point an `A` (and `AAAA` if you have IPv6) record at the host **before**
starting Caddy. Caddy asks Let's Encrypt for a certificate on first start and
the HTTP-01 challenge needs the name to already resolve.

```
arc.example.com.  A  203.0.113.10
```

### 2.2 The repository and `.env`

```bash
git clone <repo> arc && cd arc
cp .env.example .env
```

Then edit `.env`. These are the keys that **must** be real before `make up`;
everything else in the file has a working default.

| Key | Value | Notes |
|---|---|---|
| `PUBLIC_HOST` | `arc.example.com` | The name Caddy serves and gets a certificate for. |
| `PUBLIC_URL` | `https://arc.example.com` | Invite links and the CSRF origin check are built from it. Compose sets this for the containers itself; the value in `.env` is the host-side one. |
| `SECRET_KEY` | `python3 -c "import secrets; print(secrets.token_urlsafe(32))"` | |
| `FERNET_KEY` | `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` | **Generate once. Never rotate.** Every MAL token is encrypted with it; a new key means every linked account has to link again. Keep a copy somewhere other than the host. |
| `POSTGRES_PASSWORD` | a long random string | Compose refuses to start without it. |
| `MAL_CLIENT_ID`, `MAL_CLIENT_SECRET` | from the MyAnimeList API application | |
| `MAL_REDIRECT_URI` | `https://arc.example.com/api/mal/callback` | Must match **exactly** what is registered on the MAL application — see §2.3. |
| `QBIT_PASS` | a long random string | Also has to be set inside qBittorrent — see §2.5. |
| `WIREGUARD_*` | from the VPN provider's config file | The torrent client's tunnel — see §5.1. Unless you deliberately set `COMPOSE_PROFILES=novpn`, gluetun will not start without them. |
| `BOOTSTRAP_ADMIN_EMAIL` / `_PASSWORD` | your address, ≥ 10 characters | For the first boot only. Blank the password again afterwards. |
| `ANTHROPIC_API_KEY` | phase 2 | Needed only for recommendations (M12) and match suggestions (`LLM_MATCH_SUGGESTIONS=true`). |
| `MAX_TRANSCODES` | ≤ vCPU/2 | Host capacity, not a rule. |
| `ARC_DATA_DIR` | `/mnt/HC_Volume_<id>/arc_data` | Absolute path of an existing directory on the data disk, owned by `PUID:PGID`. When set, `make` adds `deploy/docker-compose.host.yml`, which binds the `arc_data` volume there; unset keeps a plain named volume on the root disk. Fixed once the volume exists (see the note in that file). |

Arc checks this list itself at startup: in production the api and the worker
each log **one ERROR line per key** that is missing or still an example value
(`change-me`, blank, `adminadmin`), and `GET /api/health` reports the count as
`config_warnings`. A healthy deployment answers `0`. See §7.

### 2.3 Register the MAL redirect URI

On the MyAnimeList API application (the same one whose client id you used),
add the **App Redirect URL**:

```
https://arc.example.com/api/mal/callback
```

MAL compares it string-for-string against what Arc sends. A trailing slash, or
`http` instead of `https`, and the link flow dies at the callback with a MAL
error page. This is the single most common first-deploy failure.

### 2.4 Bring it up

```bash
make up
```

That is three steps, in this order:

1. `docker compose build` — builds `arc-server` (the api and worker share one
   image) and `arc-web` (Caddy with the client baked in, built by
   `client/Dockerfile`; the host needs no Node).
2. `docker compose run --rm api alembic upgrade head` — a one-off container
   that starts `db`, waits for it to be healthy, applies the migrations and
   exits with alembic's status. Migrations run **before** anything starts,
   because the api creates the bootstrap admin at startup and the worker
   begins claiming jobs immediately.
3. `docker compose up -d` — the stack.

Then:

```bash
make ps      # every service should reach (healthy)
make logs    # follow everything
curl -s https://arc.example.com/api/health
# {"status":"ok","version":"…","env":"prod","config_warnings":0}
```

Caddy gets its certificate on first request; the first load may take a few
seconds. If it does not, the answer is almost always DNS or port 80.

### 2.5 qBittorrent's first-run password

`linuxserver/qbittorrent` (4.6+) will not take a password from the
environment. On the container's **first** start it generates a temporary one
and prints it to the log. The Web UI is on the internal network only, so this
is done with `docker compose exec` rather than a browser.

The service is `qbittorrent-vpn` in the default VPN mode and `qbittorrent` in
`novpn` mode (§5.4), so name it once:

```bash
QB=qbittorrent-vpn   # or: QB=qbittorrent

# 1. Find the temporary password.
docker compose --env-file .env -f deploy/docker-compose.yml logs "$QB" \
  | grep -i "temporary password"

# 2. Set the permanent one to match QBIT_PASS in .env.
TMP='<the password it printed>'
NEW='<the value of QBIT_PASS>'
docker compose --env-file .env -f deploy/docker-compose.yml exec "$QB" sh -c "
  curl -s -c /tmp/qb -H 'Referer: http://127.0.0.1:8080' \
       -d \"username=admin&password=\$TMP\" \
       http://127.0.0.1:8080/api/v2/auth/login &&
  curl -s -b /tmp/qb -H 'Referer: http://127.0.0.1:8080' \
       --data-urlencode 'json={\"web_ui_password\":\"'\"\$NEW\"'\"}' \
       http://127.0.0.1:8080/api/v2/app/setPreferences"

# 3. Restart the worker so it authenticates with the new password.
docker compose --env-file .env -f deploy/docker-compose.yml restart worker
```

The password is kept in the `qbit_config` volume from then on, so this is a
once-per-deployment step. `QBIT_USER` stays `admin`.

### 2.6 First login and the professor's invite

The bootstrap admin exists as soon as the api has started once. Sign in at
`https://arc.example.com/login`, then blank `BOOTSTRAP_ADMIN_PASSWORD` in
`.env` (the account is not touched again on restart, but an example file with
a live password in it is a password that leaks).

Issue the invite from the CLI — no browser session needed:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml \
  run --rm api python -m arc.cli invite --email prof@example.edu
# https://arc.example.com/invite/<token>
```

The link is printed **once**: Arc stores only `sha256(token)`. It is valid for
seven days (`--expires-in-hours` up to 720) and single-use. Accepting it
creates a `user`-role account and signs it in.

`--admin` promotes an address that **already has an account** — invites carry
no role, so the order is invite → accept → `--admin`.

### 2.7 Give a fresh deployment something to show

A brand-new deployment has an empty schedule and an empty Home page until the
nightly sweeps run at 03:30 UTC. Two commands fix that:

```bash
C="docker compose --env-file .env -f deploy/docker-compose.yml run --rm api"

# Cache this season and the next (the schedule renders from those rows) and
# queue a refresh of every followed show (covers, episode counts).
$C python -m arc.cli warm-catalogue

# Put shows on somebody's list as "watching", looked up by title.
$C python -m arc.cli demo-list --user-email prof@example.edu \
     --add "Sousou no Frieren" --add "Vinland Saga"
```

Both are idempotent — the sweeps deduplicate on their job type, and a list
entry describes a state rather than an event, so running either twice changes
nothing. `demo-list` needs AniList (or MAL) to be reachable; it prints the
title it actually matched, so a wrong top hit is visible rather than silent.

**Note:** `demo-list` puts shows on a list, and a list is what drives
acquisition (FR-A1). If acquisition is not paused, Arc will start looking for
episodes of whatever you add.

### 2.8 Link MyAnimeList

Each user does this themselves, from the MAL page in the client: *Link
MyAnimeList* → MAL's consent screen → back to `/api/mal/callback`. The import
runs on the link and then every `MAL_IMPORT_INTERVAL_HOURS` (6 by default).

If the consent screen returns an error about the redirect URI, it is §2.3.
If the callback succeeds but the link is not saved, `FERNET_KEY` is unset —
check `config_warnings` on `/api/health`.

---

## 3. Day-to-day operations

### Logs

```bash
make logs                       # everything, following
docker compose --env-file .env -f deploy/docker-compose.yml logs -f worker
docker compose --env-file .env -f deploy/docker-compose.yml logs --since 1h api
```

Production logs are one JSON object per line. Every service rotates at
20 MB × 5 files, so the logs cannot fill the disk the renditions need.

Two deliberate omissions: uvicorn's access log is **off**, and Caddy's access
log **redacts** `/api/invites/<token>`. An invite token is a live credential
that travels in a URL path, and an ordinary access log would be a file full of
them.

### Pausing and resuming acquisition

The kill switch is the `acquisition_paused` setting, and it is the brake for
the day a list import asks for more than the machine or the tracker should be
given at once. It stops `compute_wants` and `search_release`; it deliberately
does **not** stop `poll_qbit` (downloads already in flight finish and reach
the library) and does not stop retention.

From the client: the acquisition panel. From the API:

```bash
curl -X POST https://arc.example.com/api/acquisition/pause  -b cookies -H 'Origin: https://arc.example.com'
curl -X POST https://arc.example.com/api/acquisition/resume -b cookies -H 'Origin: https://arc.example.com'
```

`python -m arc.cli status` prints whether it is paused, among everything else.

### The status summary

```bash
docker compose --env-file .env -f deploy/docker-compose.yml \
  run --rm api python -m arc.cli status
```

```
env             prod  (public_url https://arc.example.com)
users           3 total, 3 active, 1 admin
mal accounts    1 linked
catalogue rows  779 anime
list entries    328 — watching 60, planned 27, on_hold 0, dropped 1, completed 240
episodes        6079 — not_wanted 6078, wanted 0, … ready 1, failed 0, unavailable 0
jobs            1826 — pending 1, running 0, done 1808, failed 1, cancelled 16
retained        2.0 GB on disk (sources + renditions)
acquisition     running
catalogue       active source: anilist
  anilist     closed
  mal         closed
```

Read-only, safe against a live deployment. A catalogue source shown as `open`
is one Arc is currently skipping because it failed (FR-C6).

### Upgrading

```bash
git pull
make up          # rebuild, migrate, restart
make ps
```

`make up` is safe to re-run: the build is cached, the migration is a no-op
when there is nothing to apply, and `up -d` recreates only the containers
whose configuration or image changed. Take a backup first if the pull contains
a migration (`make backup`).

---

## 4. Backups

The `backup` service is `postgres:18` running `deploy/backup.sh`: it dumps
immediately on start and then every `BACKUP_INTERVAL_SECONDS` (86400 by
default), gzipped, into the `backups` volume, and deletes dumps older than
`BACKUP_KEEP_DAYS` (14) — **never the newest one**, whatever its age, so a
fortnight of failing dumps cannot also delete the last good copy.

A dump is written as `<name>.partial` and renamed only after `pg_dump` exits
0, so an interrupted dump never appears under a name that looks trustworthy.
Dumps are `0600`: they contain password hashes and encrypted MAL tokens.

The service's own healthcheck asks the only question worth asking — *is there
a recent dump?* — so a container that is happily sleeping while every dump
fails shows as unhealthy.

```bash
make backup                       # take one now
make backups                      # list what is there
make restore file=arc-20260908T031500Z.sql.gz            # over the live database
make restore file=arc-20260908T031500Z.sql.gz db=arc_check   # into a scratch db
```

**Verify a backup without betting the deployment on it** — do this once after
the first deploy, and after any change to the backup setup:

```bash
make backups
make restore file=<newest> db=arc_check
# prints the row counts of the restored database; compare with:
docker compose --env-file .env -f deploy/docker-compose.yml \
  run --rm api python -m arc.cli status
```

**Restoring for real** (this drops and recreates every object):

```bash
docker compose --env-file .env -f deploy/docker-compose.yml stop api worker
make restore file=arc-20260908T031500Z.sql.gz
docker compose --env-file .env -f deploy/docker-compose.yml start api worker
```

Stopping api and worker first is not optional: they hold connections and will
write to a schema that is being dropped underneath them.

### What is *not* backed up

Only Postgres. The `arc_data` volume — downloads, renditions, fonts — is not,
on purpose: it is hundreds of gigabytes of files that can be fetched or
re-encoded again, and the database is what cannot. A restored database with an
empty media volume is a working Arc that re-acquires what people are watching.

Copy dumps off the host if you want them to survive the host:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml \
  run --rm --entrypoint sh backup -c 'cat /backups/<name>.sql.gz' > ./<name>.sql.gz
```

---

## 5. The VPN (torrent client only)

**qBittorrent, and nothing else in the stack, runs inside a WireGuard
tunnel.** A `gluetun` container holds the tunnel; qBittorrent joins its network
namespace (`network_mode: service:gluetun`), so it has no other route out.
Everything else — the api, the worker, Caddy, Postgres — talks to the internet
as the host, and Arc's own traffic (AniList, MAL, Nyaa's RSS, Anthropic) never
touches the VPN.

Why: copyright notices from swarm monitoring reach whoever owns the IP address
that appeared in the swarm ([spec.md §9](../spec.md)). Together with seeding
being off (§5.3 below) that is the mitigation Arc ships. It is not anonymity
and it is not a legal opinion.

**The kill switch is gluetun's firewall, and it is on by default.** The
container drops everything that is not the tunnel, so a dropped tunnel stops
qBittorrent's traffic rather than letting it fall back to the host's address.
The one hole is `FIREWALL_OUTBOUND_SUBNETS=172.29.0.0/16`, the backend network,
so the api and worker can still reach the Web UI. What Arc sees while the
tunnel is down is downloads that stop moving: `poll_qbit` keeps reporting their
progress, nothing is marked unavailable, and they resume when the tunnel does.

### 5.1 Getting a WireGuard config

Any provider that hands out WireGuard configs works. Generate one for a single
device — AirVPN's **Config Generator** (WireGuard, one device, one server) or
Mullvad's **WireGuard configuration** — and download the `.conf`. Every value
`.env` wants is a line in that file:

| `.conf` line | `.env` |
|---|---|
| `[Interface] PrivateKey` | `WIREGUARD_PRIVATE_KEY` |
| `[Interface] Address` | `WIREGUARD_ADDRESSES` |
| `[Peer] PublicKey` (the **server's**) | `WIREGUARD_PUBLIC_KEY` |
| `[Peer] PresharedKey` (AirVPN has one, Mullvad does not) | `WIREGUARD_PRESHARED_KEY` |
| `[Peer] Endpoint`, before the colon | `WIREGUARD_ENDPOINT_IP` |
| `[Peer] Endpoint`, after the colon | `WIREGUARD_ENDPOINT_PORT` |

The endpoint must be an **IP address**: gluetun resolves nothing before the
tunnel is up. If the file gives a hostname, `dig +short <name>` once and pin
the address. Keep `VPN_PROVIDER=custom` for this.

For a provider gluetun knows by name (`mullvad`, `airvpn`, `protonvpn`,
`ivpn`, …) set `VPN_PROVIDER` to it and leave the server's public key and the
endpoint empty — gluetun brings its own server list, and
`VPN_SERVER_COUNTRIES` / `VPN_SERVER_CITIES` choose where to come out. The
private key and addresses are still yours to supply.

These are secrets: they live in `.env` (gitignored) and nowhere else.

### 5.2 Verifying the tunnel

After `make up`, two commands. The first is gluetun's own view, the second is
qBittorrent's — and the point is that they are the same address, because they
are the same network stack:

```bash
docker compose --env-file .env -f deploy/docker-compose.yml \
  exec gluetun wget -qO- https://ipinfo.io/ip
docker compose --env-file .env -f deploy/docker-compose.yml \
  exec qbittorrent-vpn curl -s https://ipinfo.io/ip
```

Both must print the **VPN's** address. Compare it with the host's:

```bash
curl -s https://ipinfo.io/ip     # on the host: your server's own address
```

If those match, the tunnel is not carrying the traffic — stop and fix it
before anything downloads. `docker compose … logs gluetun` says why; the usual
answers are a hostname where an IP was needed, a private key that belongs to a
different account, and a host without `/dev/net/tun`.

`docker compose … ps` shows `gluetun` as `(healthy)` only when the tunnel is
actually passing traffic: gluetun's healthcheck pings *through* it. That is
also what `qbittorrent-vpn` waits for before it starts, so qBittorrent can
never announce itself to a tracker before the tunnel exists.

### 5.3 Seeding is off, upload is capped

Independently of the VPN, Arc tells the client not to seed. At every worker
start-up and once a day the `qbit_apply_policy` job writes:

| Preference | Value | Meaning |
|---|---|---|
| `max_ratio_enabled` / `max_ratio` | `true` / `0` | a share ratio of 0 is reached the moment the download finishes |
| `max_ratio_act` | `0` | **stop** the torrent (1 would delete it, and what happens to the data is retention's decision) |
| `max_seeding_time_enabled` / `max_seeding_time` | `true` / `0` | the same statement in minutes, for a client that disagrees about ratios |
| `up_limit` | `QBIT_UPLOAD_LIMIT_KIB` × 1024 | a global upload cap in bytes/s; it applies while downloading too, where a ratio limit cannot |

`dht` and `pex` are left alone: turning them off would break the swarms Arc
downloads from, and they are not what a notice is about. Belt and braces, every
`poll_qbit` (once a minute) stops any torrent it finds in a seeding state —
that catches anything added before the policy was written. Set `QBIT_SEEDING=true`
to turn all of it off; Arc then leaves the ratio settings as it found them and
applies only the rate cap.

A stopped torrent still has its files, and retention (FR-T1) is what deletes
them. `arc.cli status` and the admin queue view show `qbit_apply_policy` runs
like any other job; a client that is not up yet fails the job and the runner
retries it with backoff.

### 5.4 Running without the VPN

One line in `.env` decides, and Compose reads it from there:

```
COMPOSE_PROFILES=vpn      # qBittorrent inside gluetun (the default)
COMPOSE_PROFILES=novpn    # qBittorrent on the backend network, host's own IP
```

In `novpn` mode there is no `gluetun` container, the service is called
`qbittorrent` rather than `qbittorrent-vpn`, and it publishes 6881/tcp+udp on
the host — which is the port to open in the firewall in that mode and only
that mode. Everything else is identical: the same image, the same
`qbit_config` volume (so the Web UI password and the torrents survive a switch
either way), and the same `QBIT_URL=http://qbittorrent:8080` for the api and
the worker — in VPN mode `gluetun` carries that name as a network alias,
because it *is* qBittorrent's network stack.

Enable exactly one of the two. With both, two containers answer to the same
name. `make` supplies `vpn` when `.env` names no profile at all, so an `.env`
written before this section existed does not silently start a stack with no
torrent client; `make down`, `make ps` and `make logs` look at both.

Switching modes is `docker compose … down` (or `docker compose … stop
qbittorrent-vpn gluetun`), edit `.env`, `make up`. Nothing in the database
refers to either container.

`make dev` always runs `novpn`: the tunnel is a production mitigation, and a
checkout should not need a WireGuard config to run the tests.

---

## 6. What is in the stack, and why

| Service | Image | Network | Healthcheck |
|---|---|---|---|
| `caddy` | built from `client/Dockerfile` (Caddy + the built SPA) | frontend | admin API on `127.0.0.1:2019` |
| `api` | `arc-server` | frontend + backend | `GET /api/health` via the interpreter (the image has no curl) |
| `worker` | `arc-server` | backend | `python -m arc.worker --check` |
| `db` | `postgres:18` | backend | `pg_isready` |
| `backup` | `postgres:18` + `deploy/backup.sh` | backend | a dump newer than 2× the interval |
| `gluetun` (profile `vpn`) | `qmcgaw/gluetun:v3` | backend, aliased `qbittorrent` | the image's own — a ping *through* the tunnel |
| `qbittorrent-vpn` (profile `vpn`) | `lscr.io/linuxserver/qbittorrent` | gluetun's namespace | the Web UI answering on 8080 |
| `qbittorrent` (profile `novpn`) | `lscr.io/linuxserver/qbittorrent` | backend | the Web UI answering on 8080 |

Every service is `restart: unless-stopped` and rotates its logs at 20 MB × 5.
Exactly one of the two qBittorrent services runs (§5.4); they share the same
image, volumes and healthcheck, and differ only in how they reach the network.

**The two networks are a security boundary, not tidiness.** The api trusts
`X-Forwarded-For` only from `172.28.0.0/16` (the frontend subnet), which is
how the per-IP login rate limit counts real visitors instead of counting
Caddy. qBittorrent is on `backend` only, so the service most exposed to the
open internet cannot present itself to the api as the proxy. The subnets are
pinned because a range that cannot be written down is a range that cannot be
trusted.

**The worker's healthcheck is a file, not a query.** The scheduler rewrites
`$DATA_DIR/worker.heartbeat` every 30 s and `--check` exits 0 while that file
is under 90 s old. A database probe would report Postgres's health: a worker
wedged with a dead event loop and a healthy database would pass.

**Caddy** terminates TLS (automatic, Let's Encrypt), compresses with
`zstd`/`gzip`, and sends `X-Content-Type-Options: nosniff`,
`Referrer-Policy: strict-origin-when-cross-origin`, `X-Frame-Options: DENY`
and — only over HTTPS — `Strict-Transport-Security: max-age=31536000`. Hashed
`/assets/*` are cached for a day; `index.html` is `no-cache`, because it is
the one unhashed file and it names the current bundle.

`/media/*` is proxied straight through: `reverse_proxy` forwards `Range` and
returns `206`/`Content-Range` untouched, which is what makes seeking work, and
nothing in the Caddyfile adds cache headers to `/media` — the API marks those
responses `private` precisely because they are per-user authorised. Validate a
change to the Caddyfile before deploying it:

```bash
docker run --rm -e PUBLIC_HOST=arc.example.com \
  -v "$PWD/deploy/Caddyfile":/etc/caddy/Caddyfile:ro \
  caddy:2 caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

---

## 7. Troubleshooting

| Symptom | Look at |
|---|---|
| `config_warnings` > 0 on `/api/health` | `docker compose … logs api \| grep "configuration problem"` — one ERROR line per key, naming the key and what it breaks. |
| Invite links point at `localhost` | `PUBLIC_HOST` is unset or wrong; compose builds `PUBLIC_URL` from it. |
| The client's writes 403 | Same cause: the CSRF check accepts `PUBLIC_URL`'s origin. |
| MAL consent screen errors on the redirect | §2.3 — the URI must match the registered one exactly. |
| MAL callback succeeds, link not saved | `FERNET_KEY` is unset. |
| Nothing ever downloads | `QBIT_PASS` does not match qBittorrent's (§2.5), or acquisition is paused (`arc.cli status`). |
| No qBittorrent container at all | `COMPOSE_PROFILES` is set to something that is neither `vpn` nor `novpn` (§5.4). `make ps` shows what is running. |
| `gluetun` never becomes healthy | `docker compose … logs gluetun`. Usually a hostname where `WIREGUARD_ENDPOINT_IP` needs an address, wrong keys, or no `/dev/net/tun` on the host. |
| Downloads stall at the same percentage | The tunnel is down; gluetun's firewall is holding qBittorrent's traffic (§5). They resume when it comes back — nothing is marked unavailable. |
| Torrents keep seeding | `QBIT_SEEDING=true`, or the `qbit_apply_policy` job is failing — its last error is on the job row (§5.3). |
| `worker` unhealthy, no logs | Its heartbeat file is stale — the scheduler is not running. Check the worker log for an unhandled exception at startup. |
| Caddy has no certificate | DNS does not resolve to this host yet, or port 80 is blocked. |
| Disk filling up | `arc.cli status` for retained bytes; `GET /api/retention/preview` for what the next sweep would delete and why. |
| A queue that is not moving | `arc.cli status` for jobs by status; the admin queue view for the detail. |

All compose commands must be run **from the repository root** with
`--env-file .env` — `.env` lives at the root, not in `deploy/`. The `make`
targets do it for you.
