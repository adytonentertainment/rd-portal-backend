"""Add StatsSyncMetadata table

Revision ID: a1b2c3d4e5f6
Revises: e16c5b8b5f94
Create Date: 2025-01-15 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = 'e16c5b8b5f94'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create StatsSyncMetadata table
    op.create_table(
        'StatsSyncMetadata',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('spotify_track_id', sa.String(), nullable=False),
        sa.Column('last_synced_at', sa.DateTime(), nullable=False),
        sa.Column('last_synced_date', sa.Date(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )

    # Create unique index on spotify_track_id
    op.create_index(
        op.f('ix_StatsSyncMetadata_spotify_track_id'),
        'StatsSyncMetadata',
        ['spotify_track_id'],
        unique=True
    )


def downgrade() -> None:
    # Drop index
    op.drop_index(op.f('ix_StatsSyncMetadata_spotify_track_id'), table_name='StatsSyncMetadata')

    # Drop table
    op.drop_table('StatsSyncMetadata')
