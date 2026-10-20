"""statement_notifications: tell writers when new statements are ready

Publishing makes statements visible in the portal but told nobody. An admin
can now press "Notify writers" for the current reporting period, which emails
each portal contact once, in their language, that new statements are ready.

`distribution.notified_at` records which published statements have been
announced, so a second press only reaches what is new. Existing rows stay null:
nothing is ever sent automatically, and only the newest period can be notified,
so older (or backfilled) periods never produce an email.

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
"""

import sqlalchemy as sa
from alembic import op

revision = "c4d5e6f7a8b9"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("distribution", sa.Column("notified_at", sa.DateTime(), nullable=True))
    op.create_index("ix_distribution_notified_at", "distribution", ["notified_at"])

    op.create_table(
        "statement_notification",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("period_code", sa.String(), nullable=False),
        sa.Column("contact_id", sa.Integer(), sa.ForeignKey("contact.id"), nullable=True),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("language", sa.String(length=2), nullable=False, server_default="en"),
        sa.Column("writer_ids", sa.JSON(), nullable=False),
        sa.Column("distribution_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("error", sa.String(length=500), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("User.id"), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_statement_notification_period_code", "statement_notification", ["period_code"])
    op.create_index("ix_statement_notification_contact_id", "statement_notification", ["contact_id"])
    op.create_index("ix_statement_notification_status", "statement_notification", ["status"])


def downgrade():
    op.drop_index("ix_statement_notification_status", table_name="statement_notification")
    op.drop_index("ix_statement_notification_contact_id", table_name="statement_notification")
    op.drop_index("ix_statement_notification_period_code", table_name="statement_notification")
    op.drop_table("statement_notification")
    op.drop_index("ix_distribution_notified_at", table_name="distribution")
    op.drop_column("distribution", "notified_at")
