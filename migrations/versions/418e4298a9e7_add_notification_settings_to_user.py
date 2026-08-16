"""add_notification_settings_to_user

Revision ID: 418e4298a9e7
Revises: i4j5k6l7m8n9
Create Date: 2026-01-22 12:45:56.377112

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "418e4298a9e7"
down_revision = "j5k6l7m8n9o0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("User", sa.Column("notification_settings", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("User", "notification_settings")
