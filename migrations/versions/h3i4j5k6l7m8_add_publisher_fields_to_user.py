"""add publisher fields to user

Revision ID: h3i4j5k6l7m8
Revises: g2h3i4j5k6l7
Create Date: 2025-01-08 03:15:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'h3i4j5k6l7m8'
down_revision = 'g2h3i4j5k6l7'
branch_labels = None
depends_on = None


def upgrade():
    # Add new columns to User table
    op.add_column('User', sa.Column('writer_ipi', sa.String(), nullable=True))
    op.add_column('User', sa.Column('publisher_ipi', sa.String(), nullable=True))
    op.add_column('User', sa.Column('publisher_name', sa.String(), nullable=True))


def downgrade():
    # Remove columns from User table
    op.drop_column('User', 'publisher_name')
    op.drop_column('User', 'publisher_ipi')
    op.drop_column('User', 'writer_ipi')
