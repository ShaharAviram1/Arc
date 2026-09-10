#!/usr/bin/env bash
# Arc — Postgres backups (roadmap M11).
#
# Run by the `backup` service in deploy/docker-compose.yml, which is the
# stock postgres:18 image with this script as its entrypoint. Two modes:
#
#   backup.sh loop   dump now, then every BACKUP_INTERVAL_SECONDS (the service)
#   backup.sh once   dump now and exit                            (`make backup`)
#
# Dumps are gzipped plain SQL in the `backups` named volume, named
# arc-<UTC timestamp>.sql.gz, and are restored with `make restore file=…`.
#
# Plain SQL rather than a custom-format archive on purpose: it restores with
# `psql` and nothing else, it can be read with `zless` when the question is
# "was this row in there yesterday", and Arc's database is small enough that
# the size difference does not matter. `--clean --if-exists` makes a restore
# over an existing database work rather than collide.
#
# A dump is written to `<name>.partial` and renamed only once pg_dump has
# exited 0, so a dump interrupted by a host reboot never appears under a name
# that looks like a good backup.

set -euo pipefail
# Dumps are the whole database in plaintext, including password hashes and
# encrypted MAL tokens. Nothing but the owner needs to read them.
umask 077

: "${POSTGRES_HOST:=db}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_USER:=arc}"
: "${POSTGRES_DB:=arc}"
: "${BACKUP_DIR:=/backups}"
#: Nightly. Overridable so that a smoke test can watch a dump happen.
: "${BACKUP_INTERVAL_SECONDS:=86400}"
#: How many days of dumps to keep (architecture.md §8).
: "${BACKUP_KEEP_DAYS:=14}"

log() {
	printf '%s backup: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

prune() {
	# Never delete the newest dump, whatever its age. A stack whose database
	# has been unreachable for a fortnight would otherwise spend that fortnight
	# deleting the last good copy it has.
	local newest
	newest="$(ls -1t "$BACKUP_DIR"/arc-*.sql.gz 2>/dev/null | head -n 1 || true)"
	find "$BACKUP_DIR" -maxdepth 1 -type f -name 'arc-*.sql.gz' \
		-mtime "+$BACKUP_KEEP_DAYS" ! -path "$newest" -print -delete |
		while read -r gone; do log "pruned $gone"; done
}

dump_once() {
	mkdir -p "$BACKUP_DIR"
	local stamp target tmp
	stamp="$(date -u +%Y%m%dT%H%M%SZ)"
	target="$BACKUP_DIR/arc-$stamp.sql.gz"
	tmp="$target.partial"

	log "dumping $POSTGRES_DB from $POSTGRES_HOST:$POSTGRES_PORT"
	if pg_dump \
		--host "$POSTGRES_HOST" --port "$POSTGRES_PORT" \
		--username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
		--clean --if-exists --no-owner --no-privileges |
		gzip -9 >"$tmp"; then
		mv "$tmp" "$target"
		log "wrote $target ($(du -h "$target" | cut -f1))"
	else
		rm -f "$tmp"
		log "FAILED to dump $POSTGRES_DB"
		return 1
	fi

	prune
}

case "${1:-loop}" in
once)
	dump_once
	;;
loop)
	# Dump immediately rather than one interval in: the most valuable backup a
	# fresh deployment can have is the one taken before anybody touches it, and
	# a container that restarts daily would otherwise never dump at all.
	while true; do
		# A failed dump must not stop the loop — the database being down for an
		# hour is not a reason to give up on tonight's backup.
		dump_once || log "continuing after a failed dump"
		sleep "$BACKUP_INTERVAL_SECONDS"
	done
	;;
*)
	echo "usage: backup.sh [once|loop]" >&2
	exit 2
	;;
esac
