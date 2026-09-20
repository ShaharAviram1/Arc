#!/bin/sh
# Restart the torrent client when the VPN tunnel is newer than it is.
#
# gluetun restarts its VPN by itself when its health check fails, and that
# rebuilds the tunnel interface. qBittorrent shares gluetun's network
# namespace and keeps the sockets it opened on the old interface: from that
# moment every tracker fails, DHT falls to zero nodes and every magnet sits in
# "fetching metadata" — while both containers still report healthy, because
# gluetun's tunnel is fine and qBittorrent's Web UI still answers. It stayed
# like that for 19 hours on 2026-09-19/20 until the client was restarted by
# hand.
#
# The test is deliberately not "does the client look stuck" (a dead swarm looks
# the same): it is "has gluetun finished setting up a tunnel since the client
# started". gluetun logs one line per tunnel it brings up, and `docker logs
# --since` takes the client's own start time, so a count above zero means the
# client predates its tunnel. After the restart the client is newer than the
# tunnel and the count is zero again — the loop cannot flap.
set -eu

PROJECT="${COMPOSE_PROJECT:-arc}"
INTERVAL="${WATCHDOG_INTERVAL_SECONDS:-60}"
MARKER="${WATCHDOG_TUNNEL_MARKER:-Wireguard setup is complete}"
DRY_RUN="${WATCHDOG_DRY_RUN:-0}"

container() {
  docker ps -q \
    -f "label=com.docker.compose.project=$PROJECT" \
    -f "label=com.docker.compose.service=$1" | head -n 1
}

check() {
  vpn="$(container gluetun)"
  client="$(container qbittorrent-vpn)"
  if [ -z "$vpn" ] || [ -z "$client" ]; then
    return 0 # no-VPN mode, or the stack is coming up: nothing to guard
  fi
  # Only once the new tunnel passes gluetun's own check: restarting the client
  # into a tunnel that is still down would announce nothing and fix nothing.
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' "$vpn")"
  [ "$health" = "healthy" ] || return 0

  since="${WATCHDOG_SINCE_OVERRIDE:-$(docker inspect -f '{{.State.StartedAt}}' "$client")}"
  rebuilt="$(docker logs --since "$since" "$vpn" 2>&1 | grep -c "$MARKER" || true)"
  if [ "${rebuilt:-0}" -gt 0 ]; then
    if [ "$DRY_RUN" = "1" ]; then
      echo "$(date -u +%FT%TZ) tunnel rebuilt $rebuilt time(s) since $since — would restart the torrent client (dry run)"
    else
      echo "$(date -u +%FT%TZ) tunnel rebuilt $rebuilt time(s) since the torrent client started ($since) — restarting it"
      docker restart "$client" >/dev/null
    fi
  fi
}

if [ "${1:-loop}" = "once" ]; then
  check
  exit 0
fi

echo "$(date -u +%FT%TZ) vpn-watchdog: project=$PROJECT interval=${INTERVAL}s"
while true; do
  check || echo "$(date -u +%FT%TZ) check failed; trying again"
  sleep "$INTERVAL"
done
