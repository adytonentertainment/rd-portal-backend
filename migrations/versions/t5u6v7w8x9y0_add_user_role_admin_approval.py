"""add User.role + User.admin_approved (Verax role-based auth + admin approval)

Revision ID: t5u6v7w8x9y0
Revises: s4t5u6v7w8x9
Create Date: 2026-07-10 00:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "t5u6v7w8x9y0"
down_revision = "s4t5u6v7w8x9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "User",
        sa.Column("role", sa.String(), nullable=False, server_default="writer"),
    )
    op.add_column(
        "User",
        sa.Column(
            "admin_approved", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("User", "admin_approved")
    op.drop_column("User", "role")
