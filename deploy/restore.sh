#!/usr/bin/env bash
# Arc — restore a Postgres backup (roadmap M11). Run by `make restore`.
#
#   make restore file=arc-20260908T031500Z.sql.gz
#   make restore file=arc-20260908T031500Z.sql.gz db=arc_scratch
#
# Without `db=` this restores **over the live database**, which is what a real
# recovery means: the dump is `--clean --if-exists`, so every object is dropped
# and recreated. With `db=` it restores into another database on the same
# server, creating it if necessary — which is how a backup is *verified*
# without betting the running deployment on it, and is what the M11 checklist
# asks for.
#
# Stop the api and worker before restoring over the live database
# (`docker compose stop api worker`): they hold connections and will happily
# write to a schema that is being dropped underneath them.

set -euo pipefail

: "${POSTGRES_HOST:=db}"
: "${POSTGRES_PORT:=5432}"
: "${POSTGRES_USER:=arc}"
: "${POSTGRES_DB:=arc}"
: "${BACKUP_DIR:=/backups}"

file="${1:-}"
# An empty second argument (which is what `make restore` passes when `db=` is
# omitted) means "the live database", hence `:-` rather than `-`.
target="${2:-}"
target="${target:-$POSTGRES_DB}"

if [ -z "$file" ]; then
	echo "usage: restore.sh <dump-file> [target-database]" >&2
	exit 2
fi

path="$BACKUP_DIR/$file"
if [ ! -f "$path" ]; then
	echo "no such dump: $path" >&2
	echo "available:" >&2
	ls -1 "$BACKUP_DIR" >&2 || true
	exit 1
fi

psql_to() {
	psql --host "$POSTGRES_HOST" --port "$POSTGRES_PORT" \
		--username "$POSTGRES_USER" --dbname "$1" "${@:2}"
}

# Create the target when it is not the live database. Asked of pg_database
# rather than done with `CREATE DATABASE IF NOT EXISTS`, which Postgres does
# not have.
if [ "$target" != "$POSTGRES_DB" ]; then
	if ! psql_to postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$target'" | grep -q 1; then
		echo "creating database $target"
		psql_to postgres -c "CREATE DATABASE \"$target\""
	fi
fi

echo "restoring $file into $target on $POSTGRES_HOST"
# ON_ERROR_STOP so a half-applied restore is an error rather than a database
# that looks restored. The dump is --no-owner --no-privileges, so it replays
# as whatever role is connecting.
gunzip -c "$path" | psql_to "$target" --set ON_ERROR_STOP=1 --quiet

echo "restored $file into $target"
echo "row counts:"
psql_to "$target" -c "
  SELECT relname AS table, n_live_tup AS approx_rows
    FROM pg_stat_user_tables
   ORDER BY n_live_tup DESC, relname
   LIMIT 20;"
