"""add platform to revenue transaction

Revision ID: g2h3i4j5k6l7
Revises: f1g2h3i4j5k6
Create Date: 2026-01-04 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'g2h3i4j5k6l7'
down_revision = 'f1g2h3i4j5k6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add platform column to RevenueTransaction table
    op.add_column('RevenueTransaction', sa.Column('platform', sa.String(), nullable=True))


def downgrade() -> None:
    # Remove platform column from RevenueTransaction table
    op.drop_column('RevenueTransaction', 'platform')
