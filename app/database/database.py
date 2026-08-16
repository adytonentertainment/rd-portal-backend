from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from app.settings.settings import get_settings

settings = get_settings()


def _resolve_database_url() -> str:
    """The database URL, tolerating how managed hosts hand it over.

    Render (and Heroku) inject the connection string as DATABASE_URL, not the
    SQLALCHEMY_DATABASE_URL this app's settings use — and they still emit the
    legacy `postgres://` scheme, which SQLAlchemy 2.x refuses to parse
    ("Can't load plugin: sqlalchemy.dialects:postgres"). Both are handled here
    so a correct Render setup does not fail at import with a cryptic error.

    An explicitly configured SQLALCHEMY_DATABASE_URL still wins, so nothing
    changes locally.
    """
    import os

    url = (settings.sqlalchemy_database_url or "").strip()
    if not url:
        url = (os.getenv("DATABASE_URL") or "").strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if not url:
        raise ValueError(
            "No database configured. Set SQLALCHEMY_DATABASE_URL, or DATABASE_URL "
            "if your host provides it (Render, Heroku)."
        )
    return url


DATABASE_URL = _resolve_database_url()
_is_sqlite = DATABASE_URL.startswith("sqlite")

if _is_sqlite:
    # SQLite (dev/livetest): a single file with one writer at a time. Without
    # WAL + a busy timeout, a concurrent writer — e.g. the ingest worker running
    # alongside an admin upload — makes the loser fail instantly with
    # "database is locked" (a 500 on upload). WAL lets readers run alongside the
    # writer; busy_timeout makes a would-be second writer WAIT instead of erroring.
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        connect_args={"check_same_thread": False, "timeout": 120},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=120000")  # ms: outlast a long sort/parse commit
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

else:
    # PostgreSQL (production) database engine configuration
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,  # Verify connections before using them
        pool_size=10,         # Connection pool size
        max_overflow=20       # Maximum overflow connections
    )

# this will create objects of class Session with every connection to an user
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# class that all database models will inherit from
Base = declarative_base()


def create_tables():
    """Create any missing tables.

    This is a DEV convenience and NOT a migration: create_all only ever adds
    missing tables, never a column added to a table that already exists. On a
    deployed database it would quietly produce a schema that looks fine and is
    missing every ALTER a migration made, so there it is skipped — the release
    command runs `alembic upgrade head` instead.
    """
    import os

    if os.getenv("SKIP_CREATE_ALL") == "1" or not _is_sqlite:
        return
    Base.metadata.create_all(bind=engine)
