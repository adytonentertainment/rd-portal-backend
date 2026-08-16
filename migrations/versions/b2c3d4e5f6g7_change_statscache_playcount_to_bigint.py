"""Change StatsCache playcount columns to BigInteger

Revision ID: b2c3d4e5f6g7
Revises: a1b2c3d4e5f6
Create Date: 2025-11-18 21:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b2c3d4e5f6g7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Change youtube_playcount from Integer to BigInteger
    op.alter_column(
        'StatsCache',
        'youtube_playcount',
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False
    )

    # Change spotify_playcount from Integer to BigInteger
    op.alter_column(
        'StatsCache',
        'spotify_playcount',
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=False
    )


def downgrade() -> None:
    # Revert youtube_playcount back to Integer
    op.alter_column(
        'StatsCache',
        'youtube_playcount',
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False
    )

    # Revert spotify_playcount back to Integer
    op.alter_column(
        'StatsCache',
        'spotify_playcount',
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=False
    )
