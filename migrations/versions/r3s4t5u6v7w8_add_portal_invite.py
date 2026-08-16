"""add portal_invite (infra PRD §7.2 — Dropbox-style access sharing)

Revision ID: r3s4t5u6v7w8
Revises: q2r3s4t5u6v7
Create Date: 2026-07-06 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "r3s4t5u6v7w8"
down_revision = "q2r3s4t5u6v7"
branch_labels = None
depends_on = None

# contactrole enum already exists (created in q2r3s4t5u6v7); reference only.
contact_role_enum = postgresql.ENUM(
    "PRIMARY", "MANAGER", "LEGAL", "OTHER", name="contactrole", create_type=False
)


def upgrade() -> None:
    op.create_table(
        "portal_invite",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("writer_id", sa.Integer(), sa.ForeignKey("writer.id"), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("role", contact_role_enum, nullable=False),
        sa.Column("invited_by_user_id", sa.Integer(), sa.ForeignKey("User.id"), nullable=True),
        sa.Column("is_admin_invite", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("accepted_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_portal_invite_token_hash"),
    )
    op.create_index("ix_portal_invite_writer_id", "portal_invite", ["writer_id"])
    op.create_index("ix_portal_invite_email", "portal_invite", ["email"])
    op.create_index("ix_portal_invite_token_hash", "portal_invite", ["token_hash"])


def downgrade() -> None:
    op.drop_index("ix_portal_invite_token_hash", table_name="portal_invite")
    op.drop_index("ix_portal_invite_email", table_name="portal_invite")
    op.drop_index("ix_portal_invite_writer_id", table_name="portal_invite")
    op.drop_table("portal_invite")
