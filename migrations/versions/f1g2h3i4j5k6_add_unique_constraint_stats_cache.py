"""Add unique constraint to StatsCache for (spotify_track_id, date_added)

This migration adds a unique constraint to prevent duplicate entries
for the same track and date combination. Before running this migration,
you should run the deduplication utility:

    python -m app.utils.stats_cleanup --start-date 2024-12-25 --end-date 2026-01-03

Revision ID: f1g2h3i4j5k6
Revises: e0f1g2h3i4j5
Create Date: 2026-01-03 22:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f1g2h3i4j5k6'
down_revision = 'e0f1g2h3i4j5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # First, deduplicate existing data by keeping only the entry with the highest ID
    # for each (spotify_track_id, date_added) combination
    connection = op.get_bind()

    # For SQLite, we need a different approach since it doesn't support
    # DELETE with subqueries in the same way as PostgreSQL

    # Get the dialect name
    dialect = connection.dialect.name

    if dialect == 'sqlite':
        # SQLite approach: Create a temporary table, copy unique data, drop original, rename
        # First, find the IDs to keep (max ID for each track+date combo)
        connection.execute(sa.text("""
            CREATE TABLE StatsCache_temp AS
            SELECT * FROM StatsCache
            WHERE id IN (
                SELECT MAX(id)
                FROM StatsCache
                GROUP BY spotify_track_id, date_added
            )
        """))

        # Drop the original table
        connection.execute(sa.text("DROP TABLE StatsCache"))

        # Rename temp table to original
        connection.execute(sa.text("ALTER TABLE StatsCache_temp RENAME TO StatsCache"))

        # Recreate indexes
        connection.execute(sa.text(
            "CREATE INDEX ix_StatsCache_spotify_track_id ON StatsCache (spotify_track_id)"
        ))
        connection.execute(sa.text(
            "CREATE INDEX ix_StatsCache_date_added ON StatsCache (date_added)"
        ))

        # Create the unique constraint (SQLite creates a unique index)
        connection.execute(sa.text(
            "CREATE UNIQUE INDEX uq_stats_cache_track_date ON StatsCache (spotify_track_id, date_added)"
        ))

    else:
        # PostgreSQL approach: Delete duplicates keeping highest ID, then add constraint
        connection.execute(sa.text("""
            DELETE FROM "StatsCache" a
            USING "StatsCache" b
            WHERE a.id < b.id
              AND a.spotify_track_id = b.spotify_track_id
              AND a.date_added = b.date_added
        """))

        # Now add the unique constraint
        op.create_unique_constraint(
            'uq_stats_cache_track_date',
            'StatsCache',
            ['spotify_track_id', 'date_added']
        )


def downgrade() -> None:
    connection = op.get_bind()
    dialect = connection.dialect.name

    if dialect == 'sqlite':
        # Drop the unique index
        connection.execute(sa.text("DROP INDEX IF EXISTS uq_stats_cache_track_date"))
    else:
        op.drop_constraint('uq_stats_cache_track_date', 'StatsCache', type_='unique')
