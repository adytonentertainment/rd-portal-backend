"""Add RefreshToken table for persistent login sessions

Revision ID: j5k6l7m8n9o0
Revises: i4j5k6l7m8n9
Create Date: 2026-01-11 12:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "j5k6l7m8n9o0"
down_revision = "i4j5k6l7m8n9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create RefreshToken table
    op.create_table(
        "RefreshToken",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("device_info", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["User.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # Create unique index on token_hash for fast lookups
    op.create_index(
        op.f("ix_RefreshToken_token_hash"), "RefreshToken", ["token_hash"], unique=True
    )

    # Create index on user_id for fast lookups by user
    op.create_index(
        op.f("ix_RefreshToken_user_id"), "RefreshToken", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_RefreshToken_user_id"), table_name="RefreshToken")
    op.drop_index(op.f("ix_RefreshToken_token_hash"), table_name="RefreshToken")
    op.drop_table("RefreshToken")
