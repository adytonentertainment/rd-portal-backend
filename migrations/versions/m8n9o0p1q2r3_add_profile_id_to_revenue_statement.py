"""add profile_id to revenue_statement

Revision ID: m8n9o0p1q2r3
Revises: l7m8n9o0p1q2
Create Date: 2026-02-24 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "m8n9o0p1q2r3"
down_revision = "l7m8n9o0p1q2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("RevenueStatement", sa.Column("profile_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("RevenueStatement", "profile_id")
