"""Bring the database up to the current schema. Safe to run on every deploy.

`alembic upgrade head` alone does NOT work on a fresh database here, and the
reason is worth stating so nobody "fixes" this by deleting it:

The baseline revision (efa045b6d0ce) has an empty `upgrade()`. It was created
by stamping a database that already existed, so the chain has never been able
to build a schema from nothing — the second revision immediately does
`DELETE FROM "StatsCache"` on a table no migration ever created, and the deploy
dies with UndefinedTable.

So there are two cases, and this picks the right one:

  * Empty database  -> create the schema from the models (which ARE the current
                       truth) and `stamp head`, recording that every revision is
                       accounted for. Future migrations then apply normally.
  * Existing database -> `upgrade head`, the normal incremental path.

Idempotent either way, which is what a preDeployCommand has to be: Render runs
it before every single deploy, not just the first.

Usage:
    ENVIRONMENT=PRODUCTION python scripts/init_db.py
"""

from __future__ import annotations

import os
import sys

# Run as `python scripts/init_db.py` from the repo root, so the repo itself has
# to be importable — Render's preDeployCommand does exactly that.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.database.database import Base, DATABASE_URL, engine
import app.models.models  # noqa: F401 — registers tables on Base
import app.models.statements  # noqa: F401


def _alembic_config() -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("script_location", "migrations")
    # Alembic's env.py reads the app settings, but be explicit: the deploy may
    # only have DATABASE_URL set, and this is the URL we just resolved from it.
    cfg.set_main_option("sqlalchemy.url", DATABASE_URL.replace("%", "%%"))
    return cfg


def main() -> int:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    cfg = _alembic_config()

    if "alembic_version" in tables:
        print(f"[init_db] existing database ({len(tables)} tables) — upgrading")
        command.upgrade(cfg, "head")
        print("[init_db] at head")
        return 0

    if tables:
        # Schema present but never stamped: upgrading would replay migrations
        # against objects that already exist. Stamp instead, then future
        # revisions apply cleanly.
        print(f"[init_db] {len(tables)} tables present but unstamped — stamping head")
        command.stamp(cfg, "head")
        return 0

    print("[init_db] empty database — creating schema from models")
    Base.metadata.create_all(bind=engine)
    created = len(inspect(engine).get_table_names())
    command.stamp(cfg, "head")
    print(f"[init_db] created {created} tables and stamped head")
    return 0


if __name__ == "__main__":
    sys.exit(main())
