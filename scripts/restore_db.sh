#!/bin/bash
# Restore a backup produced by backup_db.sh.
#
#   ./scripts/restore_db.sh <backup.db.gz> <destination.db>
#
# Writes to the destination path you name and NEVER overwrites an existing
# file — restore to a new path, check it, then swap it in deliberately. The
# restored database is integrity-checked and its row counts printed before the
# script reports success.
set -euo pipefail

SRC="${1:-}"
DEST="${2:-}"
if [ -z "$SRC" ] || [ -z "$DEST" ]; then
  echo "usage: $0 <backup.db.gz> <destination.db>" >&2
  echo "latest backups:" >&2
  ls -1t "${VERAX_BACKUP_DIR:-/Users/stevengarcia/VERAX_2/backups}"/verax-*.db.gz 2>/dev/null | head -5 >&2
  exit 2
fi
[ -f "$SRC" ] || { echo "FATAL: no such backup: $SRC" >&2; exit 1; }
[ -e "$DEST" ] && { echo "FATAL: $DEST already exists — restore to a new path" >&2; exit 1; }

echo "[restore] decompressing $SRC"
gzip -t "$SRC" || { echo "FATAL: archive is corrupt" >&2; exit 1; }
gzip -dc "$SRC" > "$DEST"

echo "[restore] verifying"
CHECK=$(sqlite3 "$DEST" "PRAGMA integrity_check;")
[ "$CHECK" = "ok" ] || { echo "FATAL: integrity_check said: $CHECK" >&2; exit 1; }

for T in statement statement_line writer beneficiary_account contact; do
  printf '[restore]   %-22s %s\n' "$T" "$(sqlite3 "$DEST" "select count(*) from $T;" 2>/dev/null || echo '-')"
done

echo "[restore] OK — restored to $DEST"
echo "[restore] to go live: stop the API and worker, move the current DB aside, then move this into place."
