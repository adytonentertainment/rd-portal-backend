"""add_billing_interval_to_subscription

Revision ID: k6l7m8n9o0p1
Revises: 418e4298a9e7
Create Date: 2026-02-06 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "k6l7m8n9o0p1"
down_revision = "418e4298a9e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "Subscription",
        sa.Column(
            "billing_interval", sa.String(), nullable=False, server_default="month"
        ),
    )


def downgrade() -> None:
    op.drop_column("Subscription", "billing_interval")
