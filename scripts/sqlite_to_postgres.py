"""Copy the live SQLite database into PostgreSQL, for the move to Render.

Why not scripts/migrate_sqlite_to_postgres.py: that one was written for the old
tunescan database and does not survive this one.

  * It INSERTs one row at a time. statement_line holds 6.75 million rows; at a
    few thousand inserts a second that is hours, and any failure restarts it.
  * It converts booleans only for three hardcoded tables. This schema has
    booleans all over it — writer.is_client, writer.is_house_account,
    portal_invite.is_admin_invite, distribution.portal_visible — and SQLite
    stores them as 0/1. Postgres rejects 0 for a boolean column, so the
    migration dies partway through with half the data in.
  * It guesses JSON by checking whether a string starts with "{". A statement
    note beginning with a brace would be silently mangled.

This version asks SQLAlchemy what each column actually is, converts per the
real type, inserts in batches with execute_values, and copies tables in
dependency order so foreign keys are satisfied as it goes.

Usage:
    ENVIRONMENT=PRODUCTION python scripts/sqlite_to_postgres.py \\
        --sqlite verax_livetest.db \\
        --postgres "postgresql://user:pass@host/dbname"

Run `alembic upgrade head` against the target FIRST — this copies data, it does
not create the schema.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

# Invoked as `python scripts/sqlite_to_postgres.py` from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg2
from psycopg2.extras import Json, execute_values
from sqlalchemy import Boolean, DateTime, LargeBinary
from sqlalchemy import JSON as SAJSON
from sqlalchemy.dialects.postgresql import JSONB

# Registering the models populates Base.metadata with every table.
from app.database.database import Base
import app.models.models  # noqa: F401
import app.models.statements  # noqa: F401

BATCH = 5000


def _column_kinds(table):
    """Map column name -> a coarse kind we need to convert for."""
    kinds = {}
    for col in table.columns:
        t = col.type
        if isinstance(t, Boolean):
            kinds[col.name] = "bool"
        elif isinstance(t, (SAJSON, JSONB)):
            kinds[col.name] = "json"
        elif isinstance(t, DateTime):
            kinds[col.name] = "datetime"
        elif isinstance(t, LargeBinary):
            kinds[col.name] = "binary"
        else:
            kinds[col.name] = "scalar"
    return kinds


def _convert(value, kind):
    if value is None:
        return None
    if kind == "bool":
        # SQLite has no boolean: it stores 0/1, and Postgres refuses 0.
        return bool(value)
    if kind == "json":
        if isinstance(value, (dict, list)):
            return Json(value)
        if isinstance(value, str):
            try:
                return Json(json.loads(value))
            except json.JSONDecodeError:
                return Json(value)
        return Json(value)
    if kind == "binary" and isinstance(value, (bytes, bytearray)):
        return psycopg2.Binary(value)
    return value


def _orphan_filter(sqlite_conn, table) -> str:
    """SQL predicate excluding rows whose foreign keys point at nothing.

    SQLite does not enforce foreign keys by default, so a long-lived file
    accumulates orphans — this database had 2,222 writer_alias rows pointing at
    writers that no longer exist. Postgres DOES enforce them, so the copy dies
    mid-table with half the data in. Such a row is unreachable by definition
    (everything queries aliases by writer_id), so it is dropped and reported
    rather than allowed to abort a migration of 6.75 million rows.
    """
    clauses = []
    for col in table.columns:
        for fk in col.foreign_keys:
            parent, pcol = fk.column.table.name, fk.column.name
            try:
                sqlite_conn.execute(f'SELECT 1 FROM "{parent}" LIMIT 1')
            except sqlite3.OperationalError:
                continue
            clauses.append(
                f'(c."{col.name}" IS NULL OR EXISTS '
                f'(SELECT 1 FROM "{parent}" p WHERE p."{pcol}" = c."{col.name}"))'
            )
    return " AND ".join(clauses)


def copy_table(sqlite_conn, pg_conn, table, dry_run=False) -> int:
    name = table.name
    cur = sqlite_conn.cursor()
    try:
        cur.execute(f'SELECT COUNT(*) FROM "{name}"')
    except sqlite3.OperationalError:
        print(f"  - {name:<26} not present in SQLite, skipped")
        return 0
    total = cur.fetchone()[0]
    if not total:
        print(f"  - {name:<26} empty")
        return 0

    kinds = _column_kinds(table)
    # Only columns that exist on BOTH sides: the SQLite file may pre-date a
    # column, and copying a column Postgres doesn't have would abort the run.
    sqlite_cols = [r[1] for r in sqlite_conn.execute(f'PRAGMA table_info("{name}")')]
    cols = [c.name for c in table.columns if c.name in sqlite_cols]
    missing = [c.name for c in table.columns if c.name not in sqlite_cols]
    if missing:
        print(f"    (source lacks {', '.join(missing)} — will take column defaults)")

    if dry_run:
        print(f"  - {name:<26} {total:>9,} rows (dry run)")
        return total

    quoted = ", ".join(f'"{c}"' for c in cols)
    sql = f'INSERT INTO "{name}" ({quoted}) VALUES %s ON CONFLICT DO NOTHING'

    picked = ", ".join(f'c."{c}"' for c in cols)
    where = _orphan_filter(sqlite_conn, table)
    select = f'SELECT {picked} FROM "{name}" c' + (f" WHERE {where}" if where else "")
    kept = sqlite_conn.execute(
        f'SELECT COUNT(*) FROM "{name}" c' + (f" WHERE {where}" if where else "")
    ).fetchone()[0]
    if kept != total:
        print(f"    (skipping {total - kept:,} row(s) with dangling foreign keys)")
        total = kept

    cur = sqlite_conn.cursor()
    cur.execute(select)

    started = time.time()
    done = 0
    with pg_conn.cursor() as pcur:
        while True:
            rows = cur.fetchmany(BATCH)
            if not rows:
                break
            payload = [
                tuple(_convert(row[i], kinds[c]) for i, c in enumerate(cols))
                for row in rows
            ]
            execute_values(pcur, sql, payload, page_size=BATCH)
            done += len(rows)
            if total > BATCH:
                pct = done * 100 // total
                print(f"\r  - {name:<26} {done:>9,}/{total:,} ({pct}%)", end="", flush=True)
    pg_conn.commit()
    elapsed = time.time() - started
    print(f"\r  - {name:<26} {done:>9,} rows in {elapsed:,.1f}s")
    return done


def reset_sequences(pg_conn, tables) -> None:
    """Point each id sequence past the highest copied id.

    Rows arrive with their original primary keys, which does NOT advance the
    sequence. Without this the very first insert after the migration collides
    with id=1 and the app looks broken on day one.
    """
    print("\nResetting id sequences")
    with pg_conn.cursor() as cur:
        for table in tables:
            if "id" not in [c.name for c in table.columns]:
                continue
            cur.execute(
                "SELECT pg_get_serial_sequence(%s, 'id')", (f'public."{table.name}"',)
            )
            seq = cur.fetchone()[0]
            if not seq:
                continue
            cur.execute(f'SELECT COALESCE(MAX(id), 0) FROM "{table.name}"')
            top = cur.fetchone()[0]
            cur.execute("SELECT setval(%s, %s, true)", (seq, max(top, 1)))
    pg_conn.commit()


def verify(sqlite_conn, pg_conn, tables) -> bool:
    """Row counts must match on both sides, or the migration silently lost data."""
    print("\nVerifying row counts")
    ok = True
    with pg_conn.cursor() as cur:
        for table in tables:
            try:
                where = _orphan_filter(sqlite_conn, table)
                src = sqlite_conn.execute(
                    f'SELECT COUNT(*) FROM "{table.name}" c'
                    + (f" WHERE {where}" if where else "")
                ).fetchone()[0]
            except sqlite3.OperationalError:
                continue
            cur.execute(f'SELECT COUNT(*) FROM "{table.name}"')
            dst = cur.fetchone()[0]
            if src != dst:
                ok = False
                print(f"  MISMATCH {table.name}: sqlite={src:,} postgres={dst:,}")
            elif src:
                print(f"  ok       {table.name:<26} {src:,}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sqlite", required=True)
    ap.add_argument("--postgres", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    url = args.postgres
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]

    sqlite_conn = sqlite3.connect(args.sqlite)
    # SQLite hands back str; a byte-order-mark or stray invalid byte in a name
    # would otherwise abort the whole run on decode.
    sqlite_conn.text_factory = lambda b: b.decode("utf-8", "replace")

    pg_conn = psycopg2.connect(url)
    # Client encoding is NOT negotiated from the data. This roster is full of
    # Spanish names — Regalías, Malagón, Tijuana's ñ — and on a connection that
    # defaults to ASCII every one of them raises UnicodeEncodeError partway
    # through the copy, leaving the table half migrated.
    pg_conn.set_client_encoding("UTF8")

    # sorted_tables is dependency-ordered, so a child never lands before its
    # parent and foreign keys hold throughout.
    tables = list(Base.metadata.sorted_tables)
    print(f"Copying {len(tables)} tables\n")

    started = time.time()
    for table in tables:
        copy_table(sqlite_conn, pg_conn, table, dry_run=args.dry_run)

    if args.dry_run:
        print("\nDry run — nothing written.")
        return 0

    reset_sequences(pg_conn, tables)
    ok = verify(sqlite_conn, pg_conn, tables)
    print(f"\nTotal {time.time() - started:,.1f}s")
    if not ok:
        print("FAILED: row counts differ. Do not point the app at this database.")
        return 1
    print("OK — every table matches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
