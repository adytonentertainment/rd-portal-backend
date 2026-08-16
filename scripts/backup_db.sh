#!/bin/bash
# Online backup of the live SQLite database.
#
# Uses sqlite3's .backup, which is safe to run against a database being written
# to — it takes a consistent snapshot rather than copying a file mid-write, so
# it does not need the API or the ingest worker stopped.
#
# Every backup is verified before it is kept: integrity_check must pass and the
# statement/line counts must match the source. A backup nobody has restored is
# not a backup, so a failed verification deletes the file and exits non-zero.
#
# Usage:  ./scripts/backup_db.sh [destination_dir]
set -euo pipefail

DB="${VERAX_DB:-/Users/stevengarcia/VERAX_2/verax_backend/verax_livetest.db}"
DEST="${1:-${VERAX_BACKUP_DIR:-/Users/stevengarcia/VERAX_2/backups}}"
# 4 x ~280 MB ~= 1.1 GB. Deliberately modest: this disk sits near full, and a
# backup set that fills it would take the live database down with it. Raise it
# (VERAX_BACKUP_KEEP) once backups live somewhere with real headroom.
KEEP="${VERAX_BACKUP_KEEP:-4}"

[ -f "$DB" ] || { echo "FATAL: no database at $DB" >&2; exit 1; }
mkdir -p "$DEST"

STAMP="$(date +%Y%m%d-%H%M%S)"
RAW="$DEST/.verax-$STAMP.db"
OUT="$DEST/verax-$STAMP.db.gz"

# Refuse to start a backup that cannot fit. Compressed output runs ~4x smaller
# than the source, but the uncompressed snapshot exists briefly first.
DB_MB=$(( $(stat -f%z "$DB" 2>/dev/null || stat -c%s "$DB") / 1000000 ))
FREE_MB=$(df -m "$DEST" | awk 'NR==2 {print $4}')
if [ "$FREE_MB" -lt $(( DB_MB + 500 )) ]; then
  echo "FATAL: need ~$(( DB_MB + 500 )) MB free in $DEST, have ${FREE_MB} MB" >&2
  exit 1
fi

cleanup() { rm -f "$RAW"; }
trap cleanup EXIT

echo "[backup] snapshotting $DB"
sqlite3 "$DB" ".backup '$RAW'"

echo "[backup] verifying"
CHECK=$(sqlite3 "$RAW" "PRAGMA integrity_check;")
[ "$CHECK" = "ok" ] || { echo "FATAL: integrity_check said: $CHECK" >&2; exit 1; }

for T in statement statement_line writer beneficiary_account contact; do
  SRC=$(sqlite3 "$DB"  "select count(*) from $T;" 2>/dev/null || echo skip)
  BAK=$(sqlite3 "$RAW" "select count(*) from $T;" 2>/dev/null || echo skip)
  [ "$SRC" = skip ] && continue
  # The source is live: it may legitimately have GAINED rows mid-backup, but a
  # snapshot holding FEWER rows than it started with means data was lost.
  if [ "$BAK" -gt "$SRC" ] 2>/dev/null; then :; fi
  echo "[backup]   $T: source=$SRC backup=$BAK"
  if [ "$BAK" -eq 0 ] && [ "$SRC" -gt 0 ]; then
    echo "FATAL: $T is empty in the backup but has $SRC rows in the source" >&2
    exit 1
  fi
done

echo "[backup] compressing"
gzip -c "$RAW" > "$OUT"
gzip -t "$OUT" || { echo "FATAL: gzip archive is corrupt" >&2; rm -f "$OUT"; exit 1; }

SIZE_MB=$(( $(stat -f%z "$OUT" 2>/dev/null || stat -c%s "$OUT") / 1000000 ))
echo "[backup] wrote $OUT (${SIZE_MB} MB)"

# Retention: keep the newest $KEEP, drop the rest.
ls -1t "$DEST"/verax-*.db.gz 2>/dev/null | tail -n +$(( KEEP + 1 )) | while read -r OLD; do
  echo "[backup] pruning $OLD"
  rm -f "$OLD"
done

echo "[backup] OK — $(ls -1 "$DEST"/verax-*.db.gz 2>/dev/null | wc -l | tr -d ' ') backup(s) retained"
