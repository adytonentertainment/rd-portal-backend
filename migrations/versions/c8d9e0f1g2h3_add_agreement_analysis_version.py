"""Add analysis_version column to Agreement table

Revision ID: c8d9e0f1g2h3
Revises: 41502fcb74c7
Create Date: 2026-01-02 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c8d9e0f1g2h3'
down_revision = '41502fcb74c7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add analysis_version column to track which analyzer version produced the results
    # This enables future schema migrations and backward compatibility
    op.add_column(
        'Agreement',
        sa.Column('analysis_version', sa.String(), nullable=True, server_default='1.0')
    )


def downgrade() -> None:
    op.drop_column('Agreement', 'analysis_version')
