"""Revert StatsCache to old schema

Revision ID: e16c5b8b5f94
Revises: efa045b6d0ce
Create Date: 2025-11-05 05:43:52.614295

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e16c5b8b5f94'
down_revision = 'efa045b6d0ce'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Clear the table first since the schema is changing
    op.execute('DELETE FROM "StatsCache"')

    # Drop unique constraint
    op.drop_constraint('_track_service_daterange_uc', 'StatsCache', type_='unique')

    # Drop new schema indexes
    op.drop_index(op.f('ix_StatsCache_date_range_end'), table_name='StatsCache')
    op.drop_index(op.f('ix_StatsCache_date_range_start'), table_name='StatsCache')
    op.drop_index(op.f('ix_StatsCache_service'), table_name='StatsCache')

    # Drop new schema columns
    op.drop_column('StatsCache', 'updated_at')
    op.drop_column('StatsCache', 'created_at')
    op.drop_column('StatsCache', 'data')
    op.drop_column('StatsCache', 'date_range_end')
    op.drop_column('StatsCache', 'date_range_start')
    op.drop_column('StatsCache', 'service')

    # Add old schema columns
    op.add_column('StatsCache', sa.Column('spotify_playcount', sa.Integer(), nullable=True))
    op.add_column('StatsCache', sa.Column('youtube_playcount', sa.Integer(), nullable=True))
    op.add_column('StatsCache', sa.Column('date_added', sa.DateTime(), nullable=False))

    # Create old schema index
    op.create_index(op.f('ix_StatsCache_date_added'), 'StatsCache', ['date_added'], unique=False)


def downgrade() -> None:
    # Clear the table first
    op.execute('DELETE FROM "StatsCache"')

    # Drop old schema index
    op.drop_index(op.f('ix_StatsCache_date_added'), table_name='StatsCache')

    # Drop old schema columns
    op.drop_column('StatsCache', 'date_added')
    op.drop_column('StatsCache', 'youtube_playcount')
    op.drop_column('StatsCache', 'spotify_playcount')

    # Add new schema columns
    op.add_column('StatsCache', sa.Column('service', sa.String(), nullable=False))
    op.add_column('StatsCache', sa.Column('date_range_start', sa.DateTime(), nullable=False))
    op.add_column('StatsCache', sa.Column('date_range_end', sa.DateTime(), nullable=False))
    op.add_column('StatsCache', sa.Column('data', sa.JSON(), nullable=False))
    op.add_column('StatsCache', sa.Column('created_at', sa.DateTime(), nullable=False))
    op.add_column('StatsCache', sa.Column('updated_at', sa.DateTime(), nullable=False))

    # Create new schema indexes
    op.create_index(op.f('ix_StatsCache_service'), 'StatsCache', ['service'], unique=False)
    op.create_index(op.f('ix_StatsCache_date_range_start'), 'StatsCache', ['date_range_start'], unique=False)
    op.create_index(op.f('ix_StatsCache_date_range_end'), 'StatsCache', ['date_range_end'], unique=False)

    # Create unique constraint
    op.create_unique_constraint('_track_service_daterange_uc', 'StatsCache',
                                ['spotify_track_id', 'service', 'date_range_start', 'date_range_end'])
