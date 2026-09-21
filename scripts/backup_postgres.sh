#!/usr/bin/env bash
set -euo pipefail

kind=${1:-daily}
output_dir=${BACKUP_DIR:-backups}
case "$kind" in
  daily|weekly) ;;
  *) echo "backup kind must be daily or weekly" >&2; exit 2 ;;
esac

umask 077
mkdir -p "$output_dir"
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
destination="$output_dir/${kind}-${timestamp}.dump"
partial="${destination}.partial"
trap 'rm -f "$partial"' EXIT

arguments=(
  -U maple_chat
  -d maple_chat
  --data-only
  --format=custom
  --no-owner
  --no-acl
  --exclude-table-data=alembic_version
)
if [[ ${BACKUP_INCLUDE_VECTORS:-false} != true ]]; then
  arguments+=(--exclude-table-data=chunk_embeddings)
fi

docker compose exec -T postgres pg_dump "${arguments[@]}" >"$partial"
docker compose exec -T postgres pg_restore --list <"$partial" >/dev/null
mv "$partial" "$destination"
trap - EXIT

find "$output_dir" -maxdepth 1 -type f -name 'daily-*.dump' -mtime +7 -delete
find "$output_dir" -maxdepth 1 -type f -name 'weekly-*.dump' -mtime +28 -delete
printf '%s\n' "$destination"
